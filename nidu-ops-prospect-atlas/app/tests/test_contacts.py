from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from warehouse_portal import contacts as contacts_module
from warehouse_portal.auth import create_operator
from warehouse_portal.config import Settings
from warehouse_portal.contact_providers import (
    APOLLO_SEARCH_URL,
    ApolloPeopleSearchProvider,
    PublicContactSearchProvider,
)
from warehouse_portal.contacts import (
    _reserve_openai_slot,
    add_suppression,
    is_suppressed,
    research_contacts,
)
from warehouse_portal.db import get_db
from warehouse_portal.main import app
from warehouse_portal.models import Contact, ProviderThrottle, Suppression, now
from warehouse_portal.schemas import (
    Candidate,
    ContactCandidate,
    ContactResearchOutput,
    SuppressionInput,
)
from warehouse_portal.services import OrchestratorAgent, decide


def qualified_demo(db):
    manager = OrchestratorAgent(db)
    account = manager.discover("mock")[0]
    manager.research(account.id, "mock")
    manager.score(account.id)
    decide(db, account.id, "reviewer", "approved")
    return account


def real_account(db, name="Public Example"):
    account, _ = OrchestratorAgent(db).add_candidate(
        Candidate(name=name, website="https://company.example", city="Savannah")
    )
    account.pipeline_stage = "qualified"
    db.commit()
    return account


def test_qualified_demo_contact_is_sourced_and_suppression_blocks_research(db):
    account = qualified_demo(db)
    contacts = research_contacts(db, account.id)
    assert len(contacts) == 1
    assert contacts[0].email == "operations@example.invalid"
    assert contacts[0].source_type == "demo_fixture"
    assert len(research_contacts(db, account.id)) == 1
    assert db.query(Contact).filter_by(account_id=account.id).count() == 1

    entry = SuppressionInput(
        kind="email", value="OPERATIONS@example.invalid", reason="Do not contact"
    )
    suppression = add_suppression(db, entry, "reviewer")
    assert suppression.value == "operations@example.invalid"
    assert add_suppression(db, entry, "reviewer").id == suppression.id
    assert is_suppressed(db, account, contacts[0].email)

    add_suppression(
        db, SuppressionInput(kind="account", value=account.id, reason="Do not contact"), "reviewer"
    )
    with pytest.raises(ValueError, match="suppressed"):
        research_contacts(db, account.id)


def test_contact_research_requires_human_approval(db):
    account = OrchestratorAgent(db).discover("mock")[0]
    with pytest.raises(ValueError, match="Approve"):
        research_contacts(db, account.id)


def test_public_citation_gate_then_apollo_fallback(db):
    account = real_account(db)
    settings = Settings(
        openai_api_key="test", openai_research_model="test-model", apollo_api_key="test"
    )

    class UncitedPublic:
        def search(self, account):
            item = ContactCandidate(
                name="Wrong Source",
                title="Manager",
                email="manager@company.example",
                source_url="https://company.example/team",
                source_title="Team page",
                source_type="official_company",
                retrieved_at=datetime.now(UTC),
            )
            return ContactResearchOutput(contacts=[item]), set(), {}

    class ApolloFallback:
        def search(self, account):
            return [
                ContactCandidate(
                    name="Vendor Candidate",
                    title="Operations Director",
                    email=None,
                    source_url=APOLLO_SEARCH_URL,
                    source_title="Apollo People API Search",
                    source_type="apollo_search",
                    retrieved_at=datetime.now(UTC),
                )
            ]

    contacts = research_contacts(
        db,
        account.id,
        settings=settings,
        public_provider=UncitedPublic(),
        apollo_provider=ApolloFallback(),
    )
    assert len(contacts) == 1
    assert contacts[0].name == "Vendor Candidate"
    assert contacts[0].email is None
    assert contacts[0].verification_status == "vendor_unverified"
    assert db.scalar(select(Contact).where(Contact.name == "Wrong Source")) is None


def test_cited_public_contact_skips_apollo_and_suppressed_email(db):
    account = real_account(db)
    settings = Settings(openai_api_key="test", openai_research_model="test-model")
    cited = "https://company.example/team"

    class PublicProvider:
        def search(self, account):
            contacts = [
                ContactCandidate(
                    name="Public Contact",
                    title="Warehouse Manager",
                    email="public@company.example",
                    source_url=cited,
                    source_title="Company team page",
                    source_type="official_company",
                    retrieved_at=datetime.now(UTC),
                ),
                ContactCandidate(
                    name="Suppressed Contact",
                    title="Director",
                    email="blocked@company.example",
                    source_url=cited,
                    source_title="Company team page",
                    source_type="official_company",
                    retrieved_at=datetime.now(UTC),
                ),
            ]
            return ContactResearchOutput(contacts=contacts), {cited}, {}

    class NoApollo:
        def search(self, account):
            raise AssertionError("Apollo should not run when public contacts exist")

    add_suppression(
        db,
        SuppressionInput(kind="email", value="blocked@company.example", reason="Opted out"),
        "reviewer",
    )
    contacts = research_contacts(
        db,
        account.id,
        settings=settings,
        public_provider=PublicProvider(),
        apollo_provider=NoApollo(),
    )
    assert [contact.name for contact in contacts] == ["Public Contact"]
    assert contacts[0].verification_status == "source_attributed"
    assert db.query(Contact).count() == 1


def test_domain_suppression_normalizes_www_and_blocks_account(db):
    account = real_account(db)
    suppression = add_suppression(
        db,
        SuppressionInput(kind="domain", value="WWW.Company.Example", reason="Do not contact"),
        "reviewer",
    )
    assert suppression.value == "company.example"
    assert is_suppressed(db, account)
    with pytest.raises(ValueError, match="suppressed"):
        research_contacts(db, account.id)


def test_apollo_search_is_bounded_and_never_accepts_email_from_search(db):
    account = real_account(db)

    def handle(request):
        assert request.url.path == "/api/v1/mixed_people/api_search"
        assert request.headers["x-api-key"] == "test"
        assert request.url.params["q_organization_domains_list[]"] == "company.example"
        assert request.url.params["per_page"] == "5"
        return httpx.Response(
            200,
            json={
                "people": [
                    {
                        "name": "Matching Person",
                        "title": "Director",
                        "email": "untrusted@company.example",
                        "organization": {"primary_domain": "company.example"},
                    },
                    {
                        "name": "Other Company",
                        "organization": {"primary_domain": "other.example"},
                    },
                ]
            },
        )

    provider = ApolloPeopleSearchProvider(
        Settings(apollo_api_key="test"), httpx.Client(transport=httpx.MockTransport(handle))
    )
    contacts = provider.search(account)
    assert len(contacts) == 1
    assert contacts[0].email is None
    assert contacts[0].name == "Matching Person"


def test_public_provider_requests_web_search_and_collects_source_urls(db):
    account = real_account(db)
    captured = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            source = SimpleNamespace(url="https://company.example/team")
            action = SimpleNamespace(sources=[source], url=None)
            block = SimpleNamespace(type="web_search_call", action=action, content=[])
            return SimpleNamespace(
                output_parsed=ContactResearchOutput(contacts=[]), output=[block], usage=None
            )

    provider = PublicContactSearchProvider(
        Settings(openai_api_key="test", openai_research_model="test-model"),
        SimpleNamespace(responses=FakeResponses()),
    )
    output, citations, _ = provider.search(account)
    assert output.contacts == []
    assert citations == {"https://company.example/team"}
    assert captured["tools"] == [{"type": "web_search"}]
    assert captured["include"] == ["web_search_call.action.sources"]


def test_openai_slot_is_shared_with_research_jobs(db):
    account = real_account(db)
    db.add(ProviderThrottle(provider="openai", next_allowed_at=now()))
    db.commit()
    assert not is_suppressed(db, account)
    _reserve_openai_slot(db)
    with pytest.raises(ValueError, match="rate slot"):
        _reserve_openai_slot(db)


def test_default_live_contact_path_reserves_slot_before_provider(db, monkeypatch):
    account = real_account(db)
    db.add(ProviderThrottle(provider="openai", next_allowed_at=now()))
    db.commit()
    settings = Settings(openai_api_key="test", openai_research_model="test-model")
    cited = "https://company.example/contact"

    class FakePublic:
        def search(self, account):
            item = ContactCandidate(
                name="Site Contact",
                title="Operations Manager",
                email="manager@company.example",
                source_url=cited,
                source_title="Official contact page",
                source_type="official_company",
                retrieved_at=datetime.now(UTC),
            )
            return ContactResearchOutput(contacts=[item]), {cited}, {}

    monkeypatch.setattr(contacts_module, "PublicContactSearchProvider", lambda _: FakePublic())
    assert research_contacts(db, account.id, settings=settings)[0].name == "Site Contact"
    with pytest.raises(ValueError, match="rate slot"):
        research_contacts(db, account.id, settings=settings)


def test_contact_routes_enforce_role_csrf_and_show_suppression(db):
    account = qualified_demo(db)
    create_operator(db, "contact.reviewer", "a long test password", "reviewer")

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        client = TestClient(app)
        path = f"/api/accounts/{account.id}/contacts/research"
        assert client.post(path).status_code == 401
        csrf = client.post(
            "/api/login", json={"username": "contact.reviewer", "password": "a long test password"}
        ).json()["csrf_token"]
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"X-CSRF-Token": csrf}).status_code == 200
        contacts = client.get(f"/api/accounts/{account.id}/contacts").json()
        assert contacts[0]["verification_status"] == "source_attributed"
        assert "Contact candidates" in client.get(f"/accounts/{account.id}").text
        result = client.post(
            "/api/suppressions",
            json={"kind": "account", "value": account.id, "reason": "Do not contact"},
            headers={"X-CSRF-Token": csrf},
        )
        assert result.status_code == 200
        assert client.get(f"/api/accounts/{account.id}/contacts").json()[0]["suppressed"]
        assert db.query(Suppression).count() == 1
    finally:
        app.dependency_overrides.clear()
