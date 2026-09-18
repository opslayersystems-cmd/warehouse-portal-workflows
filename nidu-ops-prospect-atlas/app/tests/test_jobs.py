from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from warehouse_portal import cli
from warehouse_portal.auth import create_operator
from warehouse_portal.config import Settings
from warehouse_portal.db import get_db
from warehouse_portal.jobs import _finish_job, claim_next_job, enqueue_research, run_next_job
from warehouse_portal.main import app
from warehouse_portal.models import Account, Evidence, ProviderThrottle, ResearchJob, now
from warehouse_portal.schemas import Candidate, ResearchOutput
from warehouse_portal.services import OrchestratorAgent


def factory(db):
    return sessionmaker(bind=db.bind, expire_on_commit=False)


def test_demo_job_is_idempotent_and_completes(db):
    account = OrchestratorAgent(db).discover("mock")[0]
    first = enqueue_research(db, account.id)
    assert first.provider == "mock"
    assert enqueue_research(db, account.id).id == first.id

    result = run_next_job(factory(db))
    assert result.status == "completed"
    db.expire_all()
    assert db.get(ResearchJob, first.id).active_account_id is None
    assert db.get(Account, account.id).pipeline_stage == "researched"
    evidence_count = db.query(Evidence).filter_by(account_id=account.id).count()
    assert evidence_count > 0

    second = enqueue_research(db, account.id)
    assert second.id != first.id
    assert run_next_job(factory(db)).status == "completed"
    assert db.query(Evidence).filter_by(account_id=account.id).count() == evidence_count


def test_real_job_requires_live_configuration_and_provider_rate_slot(db):
    manager = OrchestratorAgent(db)
    first, _ = manager.add_candidate(Candidate(name="First Real Co", city="Savannah"))
    second, _ = manager.add_candidate(Candidate(name="Second Real Co", city="Savannah"))
    db.commit()
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        enqueue_research(db, first.id)
    with pytest.raises(ValueError, match="Mock jobs"):
        enqueue_research(db, first.id, "mock")

    at = now()
    db.add(ProviderThrottle(provider="openai", next_allowed_at=at))
    db.commit()
    settings = Settings(openai_api_key="test", openai_research_model="test-model")
    enqueue_research(db, first.id, settings=settings)
    enqueue_research(db, second.id, settings=settings)

    class FakeProvider:
        def research(self, account):
            return ResearchOutput(account_summary="No supported claims", evidence=[]), {}

    current = [now()]
    options = {
        "research_provider": FakeProvider(),
        "settings": settings,
        "clock": lambda: current[0],
    }
    assert run_next_job(factory(db), **options).status == "completed"
    assert run_next_job(factory(db), **options) is None
    current[0] += timedelta(seconds=10)
    assert run_next_job(factory(db), **options).status == "completed"
    db.expire_all()
    assert db.get(Account, first.id).pipeline_stage == "researched"
    assert db.get(Account, second.id).pipeline_stage == "researched"


def test_transient_failure_retries_without_persisting_error_text(db):
    account = OrchestratorAgent(db).discover("mock")[0]
    job = enqueue_research(db, account.id)

    class FlakyProvider:
        calls = 0

        def research(self, account):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("credential should never be stored")
            return ResearchOutput(account_summary="No claims", evidence=[]), {}

    provider = FlakyProvider()
    current = [now()]
    options = {"research_provider": provider, "clock": lambda: current[0]}
    assert run_next_job(factory(db), **options).status == "queued"
    db.expire_all()
    assert db.get(ResearchJob, job.id).last_error_class == "ReadTimeout"
    assert run_next_job(factory(db), **options) is None
    current[0] += timedelta(seconds=31)
    assert run_next_job(factory(db), **options).status == "completed"
    assert provider.calls == 2


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finish(db):
    account = OrchestratorAgent(db).discover("mock")[0]
    enqueue_research(db, account.id)
    at = now()
    first = claim_next_job(db, at=at)
    assert first.attempts == 1
    second = claim_next_job(db, at=at + timedelta(seconds=601))
    assert second.id == first.id and second.attempts == 2
    assert _finish_job(db, first, None, at=at + timedelta(seconds=602)) == "reclaimed"
    third = claim_next_job(db, at=at + timedelta(seconds=1202))
    assert third.attempts == 3
    assert claim_next_job(db, at=at + timedelta(seconds=1803)) is None
    db.expire_all()
    job = db.get(ResearchJob, first.id)
    assert job.status == "failed"
    assert job.last_error_class == "LeaseExpired"
    assert job.active_account_id is None


def test_job_routes_require_auth_and_show_queued_state(db):
    create_operator(db, "job.operator", "a long test password", "operator")
    account = OrchestratorAgent(db).discover("mock")[0]

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        client = TestClient(app)
        path = f"/api/accounts/{account.id}/research-jobs"
        assert client.post(path).status_code == 401
        assert client.get("/api/research-jobs").status_code == 401
        csrf = client.post(
            "/api/login", json={"username": "job.operator", "password": "a long test password"}
        ).json()["csrf_token"]
        assert client.post(path).status_code == 403
        queued = client.post(path, headers={"X-CSRF-Token": csrf})
        assert queued.status_code == 200
        assert queued.json()["status"] == "queued"
        assert client.post(path, headers={"X-CSRF-Token": csrf}).json()["id"] == queued.json()["id"]
        assert client.get("/api/research-jobs").json()[0]["status"] == "queued"
        assert "Queued" in client.get("/research-queue").text
        assert "Latest research job" in client.get(f"/accounts/{account.id}").text
    finally:
        app.dependency_overrides.clear()


def test_cli_batch_advances_past_active_jobs(db, monkeypatch):
    manager = OrchestratorAgent(db)
    manager.add_candidate(Candidate(name="Demo Alpha", city="Savannah", is_demo=True))
    manager.add_candidate(Candidate(name="Demo Beta", city="Savannah", is_demo=True))
    db.commit()
    monkeypatch.setattr(cli, "SessionLocal", factory(db))
    runner = CliRunner()
    first = runner.invoke(cli.app, ["enqueue-research", "--all", "--limit", "1"])
    second = runner.invoke(cli.app, ["enqueue-research", "--all", "--limit", "1"])
    assert first.exit_code == second.exit_code == 0
    assert db.query(ResearchJob).count() == 2
    assert runner.invoke(cli.app, ["enqueue-research", "--all"]).exit_code != 0
