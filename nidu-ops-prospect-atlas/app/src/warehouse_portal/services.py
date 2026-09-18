import csv
import re
from contextlib import contextmanager, nullcontext
from io import StringIO
from urllib.parse import urlparse

from agents import custom_span, get_current_trace, trace
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from warehouse_portal.config import Settings, get_settings
from warehouse_portal.models import (
    Account,
    AgentRun,
    Approval,
    AuditEvent,
    Evidence,
    FitScore,
    Location,
    NextAction,
    Opportunity,
    ResearchJob,
    Source,
    now,
)
from warehouse_portal.providers import (
    CsvProvider,
    MockDiscoveryProvider,
    MockResearchProvider,
    PlacesTextSearchProvider,
    ResponsesWebResearchProvider,
)
from warehouse_portal.schemas import (
    AuditOutput,
    Candidate,
    ClassificationOutput,
    DiscoveryOutput,
    EvidenceInput,
    EvidenceLevel,
    OrchestrationOutput,
    ResearchOutput,
    ScoreOutput,
)
from warehouse_portal.territory import classify_territory

COMPONENT_MAX = {
    "operational_evidence": 20,
    "current_product_fit": 20,
    "buyer_accessibility": 10,
    "likely_deal_value": 10,
    "integration_simplicity": 10,
    "implementation_simplicity": 10,
    "repeatability": 10,
    "geographic_accessibility": 5,
    "evidence_quality": 5,
}
PENALTIES = {
    "mature_wms": 20,
    "warehouse_absent": 15,
    "complex_3pl_billing": 15,
    "enterprise_required": 15,
    "transfers_required": 10,
    "high_support": 10,
}
LIVE_STAGES = {
    "discovered",
    "deduplicated",
    "researching",
    "researched",
    "scored",
    "needs_review",
    "qualified",
    "disqualified",
    "contact_found",
    "draft_ready",
    "approved_to_contact",
    "contacted",
    "replied",
    "discovery_scheduled",
    "demo_scheduled",
    "proposal",
    "nurture",
    "won",
    "lost",
    "suppressed",
}


def normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def normalized_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").hostname
    normalized = host.removeprefix("www.").casefold() if host else None
    return None if normalized and normalized.endswith(".invalid") else normalized


def audit_event(
    db: Session, account_id: str | None, actor: str, action: str, details: dict | None = None
):
    db.add(AuditEvent(account_id=account_id, actor=actor, action=action, details=details or {}))


@contextmanager
def recorded_run(
    db: Session, workflow: str, account_id: str | None, agent: str, settings: Settings
):
    run = AgentRun(workflow_name=workflow, account_id=account_id, agent_name=agent)
    db.add(run)
    db.flush()
    try:
        trace_context = (
            trace(
                workflow_name=workflow,
                group_id=account_id,
                metadata={"account_id": account_id or "", "agent_name": agent},
                disabled=not bool(settings.openai_api_key),
            )
            if get_current_trace() is None
            else nullcontext()
        )
        with trace_context:
            with custom_span(agent):
                yield run
        run.status = "completed"
    except Exception as exc:
        run_id, started = run.id, run.started_at
        db.rollback()
        db.add(
            AgentRun(
                id=run_id,
                workflow_name=workflow,
                account_id=account_id,
                agent_name=agent,
                started_at=started,
                completed_at=now(),
                status="failed",
                source_count=0,
                error=type(exc).__name__,
            )
        )
        db.commit()
        raise
    else:
        run.completed_at = now()
        db.flush()


class TerritoryDiscoveryAgent:
    name = "TerritoryDiscoveryAgent"

    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()

    def run(
        self,
        provider: str = "mock",
        city: str = "Savannah",
        industry: str = "industrial supply distributor",
    ) -> DiscoveryOutput:
        run_name = self.name
        if provider == "google":
            used = google_discovery_usage(self.db)
            if used >= self.settings.google_discovery_monthly_limit:
                raise ValueError("Google Places monthly discovery limit reached")
            run_name = "GooglePlacesDiscoveryAgent"
        with recorded_run(self.db, "company_discovery", None, run_name, self.settings) as run:
            if provider == "google":
                candidates = PlacesTextSearchProvider(self.settings).discover(city, industry)
            elif provider == "mock":
                candidates = MockDiscoveryProvider().discover(city, industry)
            else:
                raise ValueError("Provider must be mock or google")
            run.source_count = len(candidates)
            return DiscoveryOutput(candidates=candidates)


def google_discovery_usage(db: Session) -> int:
    month_start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        db.scalar(
            select(func.count(AgentRun.id)).where(
                AgentRun.agent_name == "GooglePlacesDiscoveryAgent",
                AgentRun.started_at >= month_start,
            )
        )
        or 0
    )


class CompanyResearchAgent:
    name = "CompanyResearchAgent"

    def __init__(self, settings: Settings | None = None, provider=None):
        self.settings = settings or get_settings()
        self.provider = provider

    def run(self, account: Account, provider: str = "auto") -> tuple[ResearchOutput, dict]:
        candidate = Candidate.model_validate(
            {
                "name": account.name,
                "website": account.website,
                "industry": account.industry,
                "description": account.description,
                "city": account.city,
                "state": account.state,
                "is_demo": account.is_demo,
                "source_url": account.sources[0].url if account.sources else None,
            }
        )
        chosen = self.provider
        if chosen is None:
            live = provider == "openai" or (
                provider == "auto"
                and bool(self.settings.openai_api_key and self.settings.openai_research_model)
            )
            chosen = ResponsesWebResearchProvider(self.settings) if live else MockResearchProvider()
        return chosen.research(candidate)


class EvidenceAuditorAgent:
    name = "EvidenceAuditorAgent"

    def run(
        self,
        evidence: list[EvidenceInput],
        *,
        citation_urls: set[str] | None = None,
        require_citations: bool = False,
    ) -> AuditOutput:
        accepted = []
        rejected = 0
        for item in evidence:
            parsed = urlparse(str(item.source_url))
            cited = citation_urls is not None and str(item.source_url) in citation_urls
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or item.retrieved_at > now()
                or (require_citations and not cited)
                or (
                    item.evidence_level == EvidenceLevel.VERIFIED_FACT
                    and parsed.hostname.endswith(".invalid")
                )
            ):
                rejected += 1
                continue
            accepted.append(item)
        return AuditOutput(accepted=accepted, rejected_count=rejected)


def _strength(item: Evidence) -> float:
    return {
        "VERIFIED_FACT": 1.0,
        "STRONG_INFERENCE": 0.7,
        "WEAK_INFERENCE": 0.3,
        "UNKNOWN": 0.0,
    }.get(item.evidence_level, 0.0)


def score_account(account: Account, evidence: list[Evidence]) -> ScoreOutput:
    """Pure deterministic arithmetic. Unknown claims contribute zero."""
    strength: dict[str, float] = {}
    for item in evidence:
        strength[item.claim_type] = max(strength.get(item.claim_type, 0), _strength(item))

    def s(name: str) -> float:
        return strength.get(name, 0.0)

    geography = {"TIER_1": 5, "TIER_2": 3, "TIER_3": 1}.get(account.geographic_tier, 0)
    verified = [e for e in evidence if e.evidence_level == "VERIFIED_FACT"]
    verified_sources = {e.source_id for e in verified}
    components = {
        "operational_evidence": round(12 * s("warehouse") + 8 * s("distribution")),
        "current_product_fit": round(7 * s("receiving") + 7 * s("orders") + 6 * s("inventory")),
        "buyer_accessibility": round(10 * s("buyer_access")),
        "likely_deal_value": round(10 * s("deal_value")),
        "integration_simplicity": round(10 * s("integration_simple")),
        "implementation_simplicity": round(10 * s("single_site")),
        "repeatability": round(10 * s("repeatable")),
        "geographic_accessibility": geography,
        "evidence_quality": min(5, len(verified) + len(verified_sources)),
    }
    penalties = {
        "mature_wms": 20 if s("mature_wms") == 1 else 0,
        "warehouse_absent": 15 if s("warehouse_absent") == 1 else 0,
        "complex_3pl_billing": 15 if s("complex_3pl_billing") == 1 else 0,
        "enterprise_required": 15 if s("enterprise_required") == 1 else 0,
        "transfers_required": 10 if s("transfers_required") == 1 else 0,
        "high_support": round(10 * s("high_support")),
    }
    total = max(0, min(100, sum(components.values()) - sum(penalties.values())))
    grade = "A" if total >= 75 else "B" if total >= 60 else "C" if total >= 45 else "D"
    substantive = {e.claim_type for e in evidence if _strength(e) > 0 and e.claim_type != "general"}
    source_diversity = len({e.source_id for e in evidence})
    confidence = min(
        100,
        round(
            40 * min(1, len(substantive) / 7)
            + 30 * min(1, len(verified) / 4)
            + 20 * min(1, source_diversity / 3)
            + 10 * (1 if account.geographic_tier != "OUTSIDE_TERRITORY" else 0)
        ),
    )
    uncertainties = []
    if not verified:
        uncertainties.append("No independently verified facts are stored.")
    if s("warehouse") == 0 and s("warehouse_absent") == 0:
        uncertainties.append("Physical warehouse operation is unconfirmed.")
    if s("integration_simple") == 0:
        uncertainties.append("Accounting or ERP integration requirements are unknown.")
    if account.is_demo:
        uncertainties.append("Fictional demo account: all research is synthetic.")
    questions = [
        "Is there an existing mature WMS or mandatory workflow it must replace?",
        "Are automated QuickBooks integration or multi-warehouse transfers required on day one?",
    ]
    if account.geographic_tier == "OUTSIDE_TERRITORY":
        questions.append("Has an authorized reviewer approved the territory exception?")
    complexity = (
        "high"
        if sum(penalties.values()) >= 20
        else "medium"
        if s("integration_simple") == 0
        else "low"
    )
    action = (
        "Verify warehouse operations and integration needs"
        if confidence < 50
        else "Human review of sourced fit and discovery questions"
    )
    return ScoreOutput(
        score=total,
        grade=grade,
        confidence_score=confidence,
        component_scores=components,
        penalties=penalties,
        evidence_refs=[e.id for e in evidence if _strength(e) > 0],
        rationale=f"Deterministic fit: {sum(components.values())} component points minus {sum(penalties.values())} penalties.",
        uncertainties=uncertainties,
        disqualifying_questions=questions,
        sales_cycle_category="unknown" if confidence < 50 else "consultative",
        implementation_complexity=complexity,
        support_burden="high" if penalties["high_support"] >= 7 else "unknown",
        recommended_next_action=action,
    )


class FitScoringAgent:
    name = "FitScoringAgent"

    def run(self, account: Account, evidence: list[Evidence]) -> ScoreOutput:
        return score_account(account, evidence)


class OpportunityClassifierAgent:
    name = "OpportunityClassifierAgent"

    def run(
        self, account: Account, score: ScoreOutput, evidence: list[Evidence]
    ) -> ClassificationOutput:
        types = {e.claim_type for e in evidence if _strength(e) >= 0.7}
        if account.geographic_tier == "OUTSIDE_TERRITORY" or "warehouse_absent" in types:
            label = "POOR_FIT"
        elif score.confidence_score < 40:
            label = "INSUFFICIENT_EVIDENCE"
        elif score.score < 45:
            label = (
                "DISCOVERY_CANDIDATE"
                if "warehouse" in types or "distribution" in types
                else "INSUFFICIENT_EVIDENCE"
            )
        elif "enterprise_required" in types or "transfers_required" in types:
            label = "VALUABLE_CUSTOM_SOFTWARE"
        elif "integration_required" in types:
            label = "INTEGRATION_REQUIRED"
        elif "integration_simple" not in types:
            label = "INSUFFICIENT_EVIDENCE"
        elif score.score >= 75:
            label = "REUSABLE_PRODUCT_FIT"
        else:
            label = "CONFIGURATION_FIT"
        return ClassificationOutput(
            classification=label,
            rationale=f"Rule-based classification from score {score.score}, confidence {score.confidence_score}, and sourced requirements.",
        )


class OrchestratorAgent:
    name = "OrchestratorAgent"

    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()

    def add_candidate(self, candidate: Candidate, actor: str = "system") -> tuple[Account, bool]:
        domain = normalized_domain(candidate.website)
        key = normalized_name(candidate.name)
        city = (candidate.city or "").casefold()
        existing = (
            self.db.scalar(select(Account).where(Account.domain == domain)) if domain else None
        )
        if existing is None:
            for row in self.db.scalars(select(Account).where(Account.normalized_name == key)):
                if (row.city or "").casefold() == city:
                    existing = row
                    break
        if existing:
            if existing.pipeline_stage == "discovered":
                existing.pipeline_stage = "deduplicated"
            audit_event(self.db, existing.id, actor, "duplicate_detected", {"name": candidate.name})
            self.db.flush()
            return existing, False
        tier, distance = classify_territory(candidate.city, candidate.latitude, candidate.longitude)
        account = Account(
            name=candidate.name,
            normalized_name=key,
            website=candidate.website,
            domain=domain,
            industry=candidate.industry,
            description=candidate.description if candidate.is_demo else None,
            headquarters=candidate.headquarters,
            local_address=candidate.local_address,
            city=candidate.city,
            state=candidate.state,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            distance_miles=distance,
            geographic_tier=tier,
            is_demo=candidate.is_demo,
        )
        self.db.add(account)
        self.db.flush()
        if candidate.local_address and candidate.city and candidate.state:
            self.db.add(
                Location(
                    account_id=account.id,
                    address=candidate.local_address,
                    city=candidate.city,
                    state=candidate.state,
                    latitude=candidate.latitude,
                    longitude=candidate.longitude,
                )
            )
        if candidate.source_url:
            self.db.add(
                Source(
                    account_id=account.id,
                    url=candidate.source_url,
                    title=candidate.source_title or candidate.name,
                    source_type=candidate.source_type or "discovery",
                )
            )
        audit_event(
            self.db, account.id, actor, "account_created", {"demo": account.is_demo, "tier": tier}
        )
        self.db.flush()
        return account, True

    def discover(
        self,
        provider: str = "mock",
        city: str = "Savannah",
        industry: str = "industrial supply distributor",
    ) -> list[Account]:
        output = TerritoryDiscoveryAgent(self.db, self.settings).run(provider, city, industry)
        accounts = [
            self.add_candidate(candidate, f"{provider}_discovery")[0]
            for candidate in output.candidates
        ]
        self.db.commit()
        return accounts

    def import_csv(self, content: str) -> tuple[int, int]:
        candidates = CsvProvider().parse(content)
        created = 0
        for candidate in candidates:
            _, new = self.add_candidate(candidate, "csv_import")
            created += int(new)
        self.db.commit()
        return created, len(candidates) - created

    def research(
        self, account_id: str, provider: str = "auto", research_provider=None
    ) -> OrchestrationOutput:
        account = self.db.get(Account, account_id)
        if account is None:
            raise LookupError("Account not found")
        if account.pipeline_stage in {"qualified", "disqualified"}:
            raise ValueError("Reviewed accounts require a new review cycle")
        account.pipeline_stage = "researching"
        account.research_status = "running"
        self.db.flush()
        with recorded_run(
            self.db, "company_research", account_id, CompanyResearchAgent.name, self.settings
        ) as run:
            output, metadata = CompanyResearchAgent(self.settings, research_provider).run(
                account, provider
            )
            citations = set(metadata.pop("_citation_urls", []))
            with recorded_run(
                self.db, "company_research", account_id, EvidenceAuditorAgent.name, self.settings
            ) as audit_run:
                audited = EvidenceAuditorAgent().run(
                    output.evidence,
                    citation_urls=citations,
                    require_citations=(
                        provider == "openai"
                        or (
                            provider == "auto"
                            and bool(
                                self.settings.openai_api_key and self.settings.openai_research_model
                            )
                        )
                    ),
                )
                audit_run.source_count = len({str(e.source_url) for e in audited.accepted})
            run.usage_metadata = metadata
            run.source_count = len({str(e.source_url) for e in audited.accepted})
            for item in audited.accepted:
                self._store_evidence(account, item)
            if account.is_demo:
                account.description = output.account_summary
            elif audited.accepted:
                account.description = " ".join(item.summary for item in audited.accepted[:3])
            else:
                account.description = "No attributable operational evidence found."
            account.research_status = "completed"
            account.pipeline_stage = "researched"
            account.warehouse_evidence_status = self._evidence_status(account, "warehouse")
            account.distribution_evidence_status = self._evidence_status(account, "distribution")
            audit_event(
                self.db,
                account.id,
                "research_agent",
                "research_completed",
                {"accepted": len(audited.accepted), "rejected": audited.rejected_count},
            )
        self.db.commit()
        return OrchestrationOutput(
            account_id=account.id, stage=account.pipeline_stage, source_count=run.source_count
        )

    def _store_evidence(self, account: Account, item: EvidenceInput):
        url = str(item.source_url)
        source = self.db.scalar(
            select(Source).where(Source.account_id == account.id, Source.url == url)
        )
        if source is None:
            source = Source(
                account_id=account.id,
                url=url,
                title=item.source_title,
                source_type=item.source_type,
                retrieved_at=item.retrieved_at,
            )
            self.db.add(source)
            self.db.flush()
        exists = self.db.scalar(
            select(Evidence).where(
                Evidence.account_id == account.id,
                Evidence.source_id == source.id,
                Evidence.supported_claim == item.supported_claim,
            )
        )
        if exists:
            return
        self.db.add(
            Evidence(
                account_id=account.id,
                source_id=source.id,
                source_url=url,
                source_title=item.source_title,
                retrieved_at=item.retrieved_at,
                source_type=item.source_type,
                supported_claim=item.supported_claim,
                summary=item.summary,
                evidence_level=item.evidence_level.value,
                freshness_status=item.freshness_status,
                claim_type=item.claim_type,
            )
        )
        self.db.flush()

    def _evidence_status(self, account: Account, claim_type: str) -> str:
        matches = [e.evidence_level for e in account.evidence if e.claim_type == claim_type]
        if "VERIFIED_FACT" in matches:
            return "VERIFIED_FACT"
        if "STRONG_INFERENCE" in matches:
            return "STRONG_INFERENCE"
        if "WEAK_INFERENCE" in matches:
            return "WEAK_INFERENCE"
        return "UNKNOWN"

    def score(self, account_id: str) -> OrchestrationOutput:
        account = self.db.get(Account, account_id)
        if account is None:
            raise LookupError("Account not found")
        if account.pipeline_stage in {"qualified", "disqualified"}:
            raise ValueError("Reviewed accounts require a new review cycle")
        if account.research_status != "completed":
            raise ValueError("Research must complete before scoring")
        with recorded_run(
            self.db, "fit_scoring", account_id, FitScoringAgent.name, self.settings
        ) as run:
            output = FitScoringAgent().run(account, list(account.evidence))
            account.pipeline_stage = "scored"
            previous = self.db.scalar(
                select(FitScore)
                .where(FitScore.account_id == account.id)
                .order_by(FitScore.created_at.desc())
            )
            if (
                previous
                and previous.evidence_refs == output.evidence_refs
                and previous.score == output.score
                and previous.component_scores == output.component_scores
                and previous.penalties == output.penalties
                and previous.confidence_score == output.confidence_score
            ):
                score_row = previous
            else:
                score_row = FitScore(
                    account_id=account.id,
                    score=output.score,
                    grade=output.grade,
                    confidence_score=output.confidence_score,
                    component_scores=output.component_scores,
                    penalties=output.penalties,
                    evidence_refs=output.evidence_refs,
                    rationale=output.rationale,
                    uncertainties=output.uncertainties,
                    disqualifying_questions=output.disqualifying_questions,
                    sales_cycle_category=output.sales_cycle_category,
                    implementation_complexity=output.implementation_complexity,
                    support_burden=output.support_burden,
                    recommended_next_action=output.recommended_next_action,
                )
                self.db.add(score_row)
                self.db.flush()
                for action in self.db.scalars(
                    select(NextAction).where(
                        NextAction.account_id == account.id,
                        NextAction.action_type == "human_review",
                        NextAction.status == "open",
                    )
                ):
                    action.status = "completed"
                self.db.add(
                    NextAction(
                        account_id=account.id,
                        action_type="human_review",
                        description=output.recommended_next_action,
                    )
                )
            with recorded_run(
                self.db,
                "fit_scoring",
                account_id,
                OpportunityClassifierAgent.name,
                self.settings,
            ) as class_run:
                classified = OpportunityClassifierAgent().run(
                    account, output, list(account.evidence)
                )
                class_run.source_count = len(account.sources)
            previous_opportunity = self.db.scalar(
                select(Opportunity)
                .where(Opportunity.account_id == account.id)
                .order_by(Opportunity.created_at.desc(), Opportunity.id.desc())
            )
            if (
                previous_opportunity is None
                or previous_opportunity.fit_score_id != score_row.id
                or previous_opportunity.classification != classified.classification
                or previous_opportunity.rationale != classified.rationale
            ):
                self.db.add(
                    Opportunity(
                        account_id=account.id,
                        fit_score_id=score_row.id,
                        classification=classified.classification,
                        rationale=classified.rationale,
                    )
                )
            account.pipeline_stage = "needs_review"
            run.source_count = len(account.sources)
            audit_event(
                self.db,
                account.id,
                "scoring_agent",
                "scored",
                {"score": score_row.score, "grade": score_row.grade},
            )
        self.db.commit()
        return OrchestrationOutput(
            account_id=account.id,
            stage=account.pipeline_stage,
            source_count=run.source_count,
            score=score_row.score,
        )


def review_queue(db: Session) -> list[Account]:
    return list(
        db.scalars(
            select(Account)
            .where(Account.pipeline_stage == "needs_review")
            .order_by(Account.updated_at.desc())
        )
    )


def decide(
    db: Session, account_id: str, reviewer: str, decision: str, reason: str | None = None
) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise LookupError("Account not found")
    if decision not in {"approved", "disqualified"}:
        raise ValueError("Invalid decision")
    target = "qualified" if decision == "approved" else "disqualified"
    if account.pipeline_stage == target:
        return account
    reason = reason.strip() if reason else None
    if decision == "approved" and account.pipeline_stage != "needs_review":
        raise ValueError("Only accounts in review queue can be approved")
    if decision == "disqualified" and account.pipeline_stage not in {
        "discovered",
        "deduplicated",
        "researched",
        "scored",
        "needs_review",
    }:
        raise ValueError("This account cannot be disqualified at its current stage")
    if decision == "disqualified" and not reason:
        raise ValueError("Disqualification requires a reason")
    if account.geographic_tier == "OUTSIDE_TERRITORY" and decision == "approved" and not reason:
        raise ValueError("Outside-territory approval requires an explicit reason")
    if decision == "disqualified":
        job = db.scalar(select(ResearchJob).where(ResearchJob.active_account_id == account.id))
        if job and job.status == "running":
            raise ValueError("Research is running; try disqualifying after it finishes")
        if job and job.status == "queued":
            job.status = "cancelled"
            job.active_account_id = None
            job.finished_at = now()
    db.add(Approval(account_id=account.id, reviewer=reviewer, decision=decision, reason=reason))
    account.pipeline_stage = target
    for action in db.scalars(
        select(NextAction).where(NextAction.account_id == account.id, NextAction.status == "open")
    ):
        action.status = "completed"
    audit_event(db, account.id, reviewer, decision, {"reason": reason or ""})
    db.commit()
    return account


def qualified_csv(db: Session) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "name",
            "website",
            "industry",
            "city",
            "state",
            "score",
            "grade",
            "confidence",
            "is_demo",
        ]
    )
    accounts = db.scalars(
        select(Account).where(Account.pipeline_stage == "qualified").order_by(Account.name)
    ).all()
    for account in accounts:
        approval = db.scalar(
            select(Approval).where(
                Approval.account_id == account.id, Approval.decision == "approved"
            )
        )
        if approval is None:
            continue
        score = db.scalar(
            select(FitScore)
            .where(FitScore.account_id == account.id)
            .order_by(FitScore.created_at.desc())
        )
        writer.writerow(
            [
                account.id,
                account.name,
                account.website or "",
                account.industry or "",
                account.city or "",
                account.state or "",
                score.score if score else "",
                score.grade if score else "",
                score.confidence_score if score else "",
                "true" if account.is_demo else "false",
            ]
        )
    return output.getvalue()


def dashboard_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Account.pipeline_stage, func.count(Account.id)).group_by(Account.pipeline_stage)
    )
    return {stage: count for stage, count in rows.all()}
