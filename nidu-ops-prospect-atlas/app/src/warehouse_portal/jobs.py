"""Durable, bounded research jobs run by a separate local worker process."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx
from openai import APIConnectionError, APIStatusError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from warehouse_portal.config import Settings, get_settings
from warehouse_portal.models import Account, ProviderThrottle, ResearchJob, now
from warehouse_portal.services import OrchestratorAgent

MAX_ENQUEUE_BATCH = 20
MAX_WORKER_BATCH = 20
LEASE_SECONDS = 600
OPENAI_INTERVAL_SECONDS = 10
RESEARCHABLE_STAGES = {"discovered", "deduplicated", "researched"}


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    account_id: str
    provider: str
    attempts: int


@dataclass(frozen=True)
class JobResult:
    id: str
    status: str


def enqueue_research(
    db: Session, account_id: str, provider: str = "auto", settings: Settings | None = None
) -> ResearchJob:
    settings = settings or get_settings()
    account = db.get(Account, account_id)
    if account is None:
        raise LookupError("Account not found")
    if account.pipeline_stage not in RESEARCHABLE_STAGES:
        raise ValueError("Account is not ready for research")
    if provider not in {"auto", "mock", "openai"}:
        raise ValueError("Provider must be auto, mock, or openai")
    chosen = "mock" if account.is_demo and provider == "auto" else provider
    if chosen == "auto":
        chosen = "openai"
    if chosen == "mock" and not account.is_demo:
        raise ValueError("Mock jobs are limited to fictional demo accounts")
    if chosen == "openai" and not (settings.openai_api_key and settings.openai_research_model):
        raise ValueError("OPENAI_API_KEY and OPENAI_RESEARCH_MODEL are required")
    existing = db.scalar(select(ResearchJob).where(ResearchJob.active_account_id == account_id))
    if existing is not None:
        return existing
    job = ResearchJob(account_id=account_id, active_account_id=account_id, provider=chosen)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ResearchJob).where(ResearchJob.active_account_id == account_id))
        if existing is None:
            raise
        return existing
    db.refresh(job)
    return job


def _eligible(at):
    return or_(
        and_(ResearchJob.status == "queued", ResearchJob.available_at <= at),
        and_(ResearchJob.status == "running", ResearchJob.lease_until <= at),
    )


def claim_next_job(db: Session, *, at=None, openai_interval_seconds: int = OPENAI_INTERVAL_SECONDS):
    at = at or now()
    db.execute(
        update(ResearchJob)
        .where(
            ResearchJob.status == "running",
            ResearchJob.lease_until <= at,
            ResearchJob.attempts >= ResearchJob.max_attempts,
        )
        .values(
            status="failed",
            active_account_id=None,
            finished_at=at,
            lease_until=None,
            last_error_class="LeaseExpired",
        )
        .execution_options(synchronize_session=False)
    )
    candidates = db.scalars(
        select(ResearchJob)
        .where(_eligible(at), ResearchJob.attempts < ResearchJob.max_attempts)
        .order_by(ResearchJob.available_at, ResearchJob.created_at)
        .limit(100)
    ).all()
    for candidate in candidates:
        if candidate.provider == "openai":
            if db.get(ProviderThrottle, "openai") is None:
                raise RuntimeError("OpenAI provider throttle is missing; run migrations")
            slot = db.execute(
                update(ProviderThrottle)
                .where(
                    ProviderThrottle.provider == "openai",
                    ProviderThrottle.next_allowed_at <= at,
                )
                .values(next_allowed_at=at + timedelta(seconds=openai_interval_seconds))
                .returning(ProviderThrottle.provider)
                .execution_options(synchronize_session=False)
            ).first()
            if slot is None:
                continue
        row = db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == candidate.id,
                _eligible(at),
                ResearchJob.attempts < ResearchJob.max_attempts,
            )
            .values(
                status="running",
                attempts=ResearchJob.attempts + 1,
                started_at=at,
                lease_until=at + timedelta(seconds=LEASE_SECONDS),
            )
            .returning(
                ResearchJob.id,
                ResearchJob.account_id,
                ResearchJob.provider,
                ResearchJob.attempts,
            )
            .execution_options(synchronize_session=False)
        ).first()
        if row is not None:
            db.commit()
            return ClaimedJob(*row)
        db.rollback()  # Also releases a provider slot if another worker won the claim.
    db.commit()
    return None


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, APIConnectionError)):
        return True
    if isinstance(exc, (httpx.HTTPStatusError, APIStatusError)):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False


def _finish_job(db: Session, claim: ClaimedJob, error: Exception | None, *, at=None) -> str:
    at = at or now()
    job = db.get(ResearchJob, claim.id)
    if job is None or job.status != "running" or job.attempts != claim.attempts:
        return "reclaimed"
    if error is None:
        job.status = "completed"
        job.active_account_id = None
        job.finished_at = at
        job.lease_until = None
        job.last_error_class = None
    elif _retryable(error) and job.attempts < job.max_attempts:
        job.status = "queued"
        job.available_at = at + timedelta(seconds=min(30 * 2 ** (job.attempts - 1), 300))
        job.lease_until = None
        job.last_error_class = type(error).__name__
    else:
        job.status = "failed"
        job.active_account_id = None
        job.finished_at = at
        job.lease_until = None
        job.last_error_class = type(error).__name__
    db.commit()
    return job.status


def run_next_job(
    session_factory: sessionmaker[Session],
    *,
    research_provider=None,
    settings: Settings | None = None,
    clock: Callable = now,
    openai_interval_seconds: int = OPENAI_INTERVAL_SECONDS,
) -> JobResult | None:
    with session_factory() as db:
        claim = claim_next_job(db, at=clock(), openai_interval_seconds=openai_interval_seconds)
    if claim is None:
        return None
    error = None
    try:
        with session_factory() as db:
            OrchestratorAgent(db, settings).research(
                claim.account_id, claim.provider, research_provider=research_provider
            )
    except Exception as exc:
        error = exc
    with session_factory() as db:
        status = _finish_job(db, claim, error, at=clock())
    return JobResult(claim.id, status)
