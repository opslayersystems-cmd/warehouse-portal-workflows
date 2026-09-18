from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from warehouse_portal.auth import create_operator
from warehouse_portal.config import Settings, get_settings
from warehouse_portal.db import get_db, make_engine
from warehouse_portal.jobs import enqueue_research
from warehouse_portal.main import app
from warehouse_portal.models import Account, AgentRun, Approval, Evidence, FitScore, Source
from warehouse_portal.providers import (
    MockResearchProvider,
    PlacesTextSearchProvider,
    ResponsesWebResearchProvider,
    city_from_address,
    retry_request,
)
from warehouse_portal.schemas import Candidate, EvidenceInput, ResearchOutput
from warehouse_portal.services import (
    EvidenceAuditorAgent,
    OpportunityClassifierAgent,
    OrchestratorAgent,
    TerritoryDiscoveryAgent,
    decide,
    qualified_csv,
    review_queue,
    score_account,
)
from warehouse_portal.territory import classify_territory, geodesic_miles


def add_evidence(db, account, kind, level="VERIFIED_FACT"):
    source = Source(
        account_id=account.id,
        url=f"https://example.com/{kind}",
        title=f"Official {kind}",
        source_type="official_company",
    )
    db.add(source)
    db.flush()
    row = Evidence(
        account_id=account.id,
        source_id=source.id,
        source_url=source.url,
        source_title=source.title,
        retrieved_at=datetime.now(UTC),
        source_type=source.source_type,
        supported_claim=f"Claim about {kind}",
        summary=f"Documented {kind}",
        evidence_level=level,
        freshness_status="CURRENT",
        claim_type=kind,
    )
    db.add(row)
    db.flush()
    return row


def test_scoring_arithmetic_and_penalties(db):
    account, _ = OrchestratorAgent(db).add_candidate(
        Candidate(name="Test Distributor", city="Savannah")
    )
    evidence = [
        add_evidence(db, account, kind)
        for kind in (
            "warehouse",
            "distribution",
            "receiving",
            "orders",
            "inventory",
            "buyer_access",
            "deal_value",
            "integration_simple",
            "single_site",
            "repeatable",
        )
    ]
    score = score_account(account, evidence)
    assert sum(score.component_scores.values()) == 100
    assert score.score == 100
    assert score.grade == "A"
    assert score.confidence_score == 100
    evidence.append(add_evidence(db, account, "mature_wms"))
    penalized = score_account(account, evidence)
    assert penalized.penalties["mature_wms"] == 20
    assert penalized.score == 80
    assert score_account(account, []).penalties["warehouse_absent"] == 0
    assert score_account(account, []).score == 5
    assert (
        score_account(account, [add_evidence(db, account, "warehouse_absent")]).penalties[
            "warehouse_absent"
        ]
        == 15
    )


def test_confidence_independent_of_fit(db):
    account, _ = OrchestratorAgent(db).add_candidate(
        Candidate(name="Confidence Co", city="Savannah")
    )
    weak = [add_evidence(db, account, "warehouse", "WEAK_INFERENCE")]
    first = score_account(account, weak)
    strong = [add_evidence(db, account, "distribution"), add_evidence(db, account, "orders")]
    second = score_account(account, weak + strong)
    assert second.confidence_score > first.confidence_score
    assert second.score > first.score


def test_low_score_with_verified_operations_stays_discovery_candidate(db):
    account, _ = OrchestratorAgent(db).add_candidate(
        Candidate(name="Operations Candidate", city="Savannah")
    )
    evidence = [add_evidence(db, account, "warehouse"), add_evidence(db, account, "distribution")]
    result = OpportunityClassifierAgent().run(account, score_account(account, evidence), evidence)
    assert result.classification == "DISCOVERY_CANDIDATE"


def test_disqualify_discovered_account_requires_reason(db):
    account, _ = OrchestratorAgent(db).add_candidate(Candidate(name="Retail Only", city="Savannah"))
    db.commit()
    with pytest.raises(ValueError, match="requires a reason"):
        decide(db, account.id, "Reviewer", "disqualified", "   ")
    with pytest.raises(ValueError, match="review queue"):
        decide(db, account.id, "Reviewer", "approved")
    decided = decide(db, account.id, "Reviewer", "disqualified", "Retail only")
    assert decided.pipeline_stage == "disqualified"
    approval = db.scalar(select(Approval).where(Approval.account_id == account.id))
    assert approval.reason == "Retail only"


def test_disqualifying_cancels_queued_research(db):
    account = OrchestratorAgent(db).discover("mock")[0]
    job = enqueue_research(db, account.id)
    assert job.status == "queued"
    decide(db, account.id, "Reviewer", "disqualified", "Out of scope")
    assert job.status == "cancelled"
    assert job.active_account_id is None


@pytest.mark.parametrize(
    "city,tier",
    [
        ("Savannah", "TIER_1"),
        ("Thunderbolt", "TIER_1"),
        ("Pooler", "TIER_1"),
        ("Pembroke", "TIER_1"),
        ("Bluffton", "TIER_1"),
        ("North Charleston", "TIER_2"),
        ("Charleston", "TIER_3"),
        ("Jacksonville", "TIER_3"),
        ("Atlanta", "OUTSIDE_TERRITORY"),
    ],
)
def test_geography(city, tier):
    found, distance = classify_territory(city, None, None)
    assert found == tier
    if tier != "OUTSIDE_TERRITORY":
        assert distance is not None
    assert geodesic_miles((32.0809, -81.0912), (32.0809, -81.0912)) == 0


def test_whitelisted_city_with_far_away_coordinates_is_outside():
    assert classify_territory("Savannah", 33.7490, -84.3880)[0] == "OUTSIDE_TERRITORY"


def test_duplicate_detection_and_csv_import(db):
    manager = OrchestratorAgent(db)
    content = "name,website,city,state\nAcme Supply,https://acme.example,Pooler,GA\nACME SUPPLY,https://www.acme.example,Pooler,GA\nOther Supply,,Rincon,GA\n"
    created, duplicates = manager.import_csv(content)
    assert (created, duplicates) == (2, 1)
    assert db.query(Account).count() == 2
    assert manager.import_csv(content) == (0, 3)


def test_distinct_fictional_demo_urls_do_not_collapse(db):
    accounts = OrchestratorAgent(db).discover("mock")
    assert len({account.id for account in accounts}) == 2


def test_evidence_classification_and_unsupported_fact_rejection():
    with pytest.raises(ValueError):
        EvidenceInput(
            source_url="https://example.com/x",
            source_title="Example source",
            retrieved_at=datetime.now(UTC),
            source_type="llm_unsourced",
            supported_claim="Has a warehouse",
            summary="No actual citation",
            evidence_level="VERIFIED_FACT",
        )
    item = EvidenceInput(
        source_url="https://example.com/x",
        source_title="Example source",
        retrieved_at=datetime.now(UTC),
        source_type="official_company",
        supported_claim="Has a warehouse",
        summary="Source says so",
        evidence_level="VERIFIED_FACT",
    )
    assert EvidenceAuditorAgent().run([item], require_citations=True).rejected_count == 1
    assert EvidenceAuditorAgent().run(
        [item], citation_urls={"https://example.com/x"}, require_citations=True
    ).accepted == [item]


def test_mock_fallback_never_invents_real_company_evidence():
    output, usage = MockResearchProvider().research(Candidate(name="Real Name", city="Savannah"))
    assert output.evidence == []
    assert usage == {}
    assert output.uncertainties


def test_research_score_approval_and_export(db):
    manager = OrchestratorAgent(db)
    account = manager.discover("mock")[0]
    with pytest.raises(ValueError):
        decide(db, account.id, "Reviewer", "approved")
    manager.research(account.id, "mock")
    first_count = db.query(Evidence).filter_by(account_id=account.id).count()
    manager.research(account.id, "mock")
    assert db.query(Evidence).filter_by(account_id=account.id).count() == first_count
    score = manager.score(account.id)
    assert score.stage == "needs_review"
    assert db.query(FitScore).filter_by(account_id=account.id).count() == 1
    run_names = {run.agent_name for run in db.scalars(select(AgentRun))}
    assert {
        "TerritoryDiscoveryAgent",
        "CompanyResearchAgent",
        "EvidenceAuditorAgent",
        "FitScoringAgent",
        "OpportunityClassifierAgent",
    }.issubset(run_names)
    assert account.id in [a.id for a in review_queue(db)]
    approved = decide(db, account.id, "Reviewer", "approved")
    assert approved.pipeline_stage == "qualified"
    assert db.query(Approval).filter_by(account_id=account.id).count() == 1
    assert decide(db, account.id, "Reviewer", "approved").pipeline_stage == "qualified"
    assert db.query(Approval).filter_by(account_id=account.id).count() == 1
    exported = qualified_csv(db)
    assert "Demo Savannah Industrial Supply" in exported
    assert "is_demo" in exported and ",true" in exported


def test_outside_territory_needs_explicit_exception(db):
    manager = OrchestratorAgent(db)
    account, _ = manager.add_candidate(Candidate(name="Far Away Co", city="Atlanta"))
    manager.research(account.id, "mock")
    manager.score(account.id)
    with pytest.raises(ValueError):
        decide(db, account.id, "Reviewer", "approved")
    assert (
        decide(
            db, account.id, "Reviewer", "approved", "Approved territory exception"
        ).pipeline_stage
        == "qualified"
    )


def test_retry_transient_failure_and_no_retry_on_400():
    attempts = 0

    def transient():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            request = httpx.Request("GET", "https://example.com")
            raise httpx.HTTPStatusError(
                "temporary", request=request, response=httpx.Response(503, request=request)
            )
        return "ok"

    assert retry_request(transient) == "ok"
    assert attempts == 3
    request = httpx.Request("GET", "https://example.com")
    with pytest.raises(httpx.HTTPStatusError):
        retry_request(
            lambda: (_ for _ in ()).throw(
                httpx.HTTPStatusError(
                    "bad", request=request, response=httpx.Response(400, request=request)
                )
            )
        )


def test_google_places_provider_mock_transport_and_retry():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        assert "websiteUri" not in request.headers["x-goog-fieldmask"]
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "displayName": {"text": "Sample Supply"},
                        "formattedAddress": "Savannah, GA",
                        "googleMapsUri": "https://maps.google.com/sample",
                        "location": {"latitude": 32.08, "longitude": -81.09},
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    provider = PlacesTextSearchProvider(Settings(google_maps_api_key="test"), client)
    found = provider.discover("Savannah", "industrial supply")
    assert calls == 2
    assert found[0].name == "Sample Supply"
    assert found[0].website is None
    assert found[0].source_type == "google_places"
    assert city_from_address("123 Main St, North Charleston, SC 29405") == (
        "North Charleston",
        "SC",
    )


def test_google_discovery_monthly_limit(db, monkeypatch):
    calls = 0

    class FakePlacesProvider:
        def __init__(self, settings):
            pass

        def discover(self, city, industry):
            nonlocal calls
            calls += 1
            return []

    monkeypatch.setattr("warehouse_portal.services.PlacesTextSearchProvider", FakePlacesProvider)
    settings = Settings(google_maps_api_key="test", google_discovery_monthly_limit=2)
    agent = TerritoryDiscoveryAgent(db, settings)
    agent.run("google")
    db.commit()
    agent.run("google")
    db.commit()
    with pytest.raises(ValueError, match="monthly discovery limit reached"):
        agent.run("google")
    assert calls == 2


def test_openai_provider_uses_current_web_search_and_collects_sources():
    captured = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            source = SimpleNamespace(url="https://example.com/about")
            action = SimpleNamespace(sources=[source], url=None)
            block = SimpleNamespace(type="web_search_call", action=action, content=[])
            return SimpleNamespace(
                output_parsed=ResearchOutput(account_summary="No claims", evidence=[]),
                output=[block],
                usage=None,
            )

    client = SimpleNamespace(responses=FakeResponses())
    provider = ResponsesWebResearchProvider(
        Settings(openai_api_key="test", openai_research_model="account-selected-model"), client
    )
    output, metadata = provider.research(Candidate(name="Example Company", city="Savannah"))
    assert output.evidence == []
    assert captured["tools"] == [{"type": "web_search"}]
    assert captured["include"] == ["web_search_call.action.sources"]
    assert captured["model"] == "account-selected-model"
    assert metadata["_citation_urls"] == ["https://example.com/about"]


def test_failure_run_is_recorded_without_secret(db):
    class FailingProvider:
        def research(self, account):
            raise RuntimeError("provider failed with credential test-secret")

    manager = OrchestratorAgent(db)
    account, _ = manager.add_candidate(Candidate(name="Failure Example", city="Savannah"))
    db.commit()
    with pytest.raises(RuntimeError):
        manager.research(account.id, research_provider=FailingProvider())
    run = db.scalar(select(AgentRun).where(AgentRun.account_id == account.id))
    assert run.status == "failed"
    assert run.error == "RuntimeError"
    assert db.get(Account, account.id).pipeline_stage == "discovered"


def test_unknown_integration_is_not_inferred_required(db):
    manager = OrchestratorAgent(db)
    account, _ = manager.add_candidate(Candidate(name="Unknown Integration", city="Savannah"))
    evidence = [
        add_evidence(db, account, kind)
        for kind in (
            "warehouse",
            "distribution",
            "receiving",
            "orders",
            "inventory",
            "buyer_access",
            "deal_value",
            "single_site",
            "repeatable",
        )
    ]
    classification = OpportunityClassifierAgent().run(
        account, score_account(account, evidence), evidence
    )
    assert classification.classification == "INSUFFICIENT_EVIDENCE"


def test_unsafe_candidate_url_rejected():
    with pytest.raises(ValueError):
        Candidate(name="Unsafe URL", website="javascript:alert(1)")


def test_migration_creates_all_tables(tmp_path, monkeypatch):
    target = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
    get_settings.cache_clear()
    try:
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        inspector = inspect(make_engine(f"sqlite:///{target}"))
        tables = set(inspector.get_table_names())
        assert {
            "accounts",
            "locations",
            "sources",
            "evidence",
            "fit_scores",
            "opportunities",
            "approvals",
            "next_actions",
            "agent_runs",
            "audit_events",
            "operators",
            "research_jobs",
            "provider_throttles",
            "contacts",
            "suppressions",
        }.issubset(tables)
        assert "google_subject" in {column["name"] for column in inspector.get_columns("operators")}
    finally:
        get_settings.cache_clear()


def test_api_review_queue_and_ui(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        create_operator(db, "tester", "a long test password", "admin")
        client = TestClient(app)
        csrf = client.post(
            "/api/login", json={"username": "tester", "password": "a long test password"}
        ).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        created = client.post("/api/discover", json={"provider": "mock"}, headers=headers)
        assert created.status_code == 200
        account_id = created.json()["account_ids"][0]
        assert client.get("/").status_code == 200
        assert client.get(f"/accounts/{account_id}").status_code == 200
        unscored_row = next(
            row for row in client.get("/api/accounts").json() if row["id"] == account_id
        )
        assert unscored_row["score"] is None
        assert "Pending research" in client.get("/accounts?stage=discovered").text
        assert (
            client.post(f"/api/accounts/{account_id}/approve", json={}, headers=headers).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/accounts/{account_id}/research?provider=mock", headers=headers
            ).status_code
            == 200
        )
        assert client.post(f"/api/accounts/{account_id}/score", headers=headers).status_code == 200
        scored_row = next(
            row for row in client.get("/api/accounts").json() if row["id"] == account_id
        )
        assert scored_row["score"] is not None
        assert f"{scored_row['score']}/100" in client.get("/accounts?stage=needs_review").text
        assert any(row["id"] == account_id for row in client.get("/api/review-queue").json())
        assert "Demo Savannah Industrial Supply" in client.get("/approval-queue").text
        detail = client.get(f"/accounts/{account_id}").text
        assert "Verified facts" in detail and "Inferences and unknowns" in detail
        result = client.post(f"/api/accounts/{account_id}/approve", json={}, headers=headers)
        assert result.status_code == 200
        assert (
            db.scalar(select(Approval).where(Approval.account_id == account_id)).reviewer
            == "tester"
        )
        assert "Demo Savannah Industrial Supply" in client.get("/api/export-qualified").text
    finally:
        app.dependency_overrides.clear()


def test_ui_disqualifies_discovered_account(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        create_operator(db, "reviewer", "a long test password", "reviewer")
        account, _ = OrchestratorAgent(db).add_candidate(
            Candidate(name="Out of Scope", city="Savannah")
        )
        db.commit()
        client = TestClient(app)
        csrf = client.post(
            "/api/login", json={"username": "reviewer", "password": "a long test password"}
        ).json()["csrf_token"]
        assert "Disqualify" in client.get("/accounts?stage=discovered").text
        response = client.post(
            f"/ui/accounts/{account.id}/decision",
            data={"csrf_token": csrf, "decision": "disqualified", "reason": "Retail only"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert db.get(Account, account.id).pipeline_stage == "disqualified"
        assert "Out of Scope" in client.get("/accounts?stage=disqualified").text
    finally:
        app.dependency_overrides.clear()


def test_api_csv_import(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        create_operator(db, "importer", "a long test password", "operator")
        client = TestClient(app)
        csrf = client.post(
            "/api/login", json={"username": "importer", "password": "a long test password"}
        ).json()["csrf_token"]
        content = StringIO("name,city,state\nCSV Example,Pooler,GA\n").getvalue()
        result = client.post(
            "/api/import-csv",
            files={"file": ("accounts.csv", content, "text/csv")},
            headers={"X-CSRF-Token": csrf},
        )
        assert result.status_code == 200
        assert result.json() == {"created": 1, "duplicates": 0}
    finally:
        app.dependency_overrides.clear()
