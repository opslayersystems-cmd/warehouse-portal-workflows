import csv
import re
from datetime import UTC, datetime
from io import StringIO
from typing import Protocol

import httpx
from openai import OpenAI

from warehouse_portal.config import Settings
from warehouse_portal.schemas import Candidate, EvidenceInput, ResearchOutput
from warehouse_portal.territory import CITY_COORDS

CITY_STATES = {
    name: (
        "SC"
        if name in {"beaufort", "bluffton", "charleston", "north charleston"}
        else "FL"
        if name == "jacksonville"
        else "GA"
    )
    for name in CITY_COORDS
}


def city_from_address(address: str) -> tuple[str | None, str | None]:
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        state = CITY_STATES[name]
        if re.search(rf",\s*{re.escape(name)}\s*,\s*{state}\b", address, re.IGNORECASE):
            return name.title(), state
    return None, None


class GooglePlacesProvider(Protocol):
    def discover(self, city: str, industry: str) -> list[Candidate]: ...


class OpenAIWebResearchProvider(Protocol):
    def research(self, account: Candidate) -> tuple[ResearchOutput, dict]: ...


class CsvImportProvider(Protocol):
    def parse(self, content: str) -> list[Candidate]: ...


class ManualAccountProvider(Protocol):
    def create_candidate(self, data: Candidate) -> Candidate: ...


class ContactEnrichmentProvider(Protocol):
    def find_contacts(self, account_id: str) -> list[dict]: ...


class EmailProvider(Protocol):
    def create_draft(self, account_id: str, content: str) -> str: ...

    def send(self, draft_id: str) -> None: ...


def retry_request(call, attempts: int = 3):
    """Retry only transient transport and server/rate-limit errors."""
    for attempt in range(attempts):
        try:
            return call()
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == attempts - 1:
                raise
        except httpx.HTTPStatusError as exc:
            if attempt == attempts - 1 or exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
    raise RuntimeError("Unreachable retry state")


class CsvProvider:
    def parse(self, content: str) -> list[Candidate]:
        reader = csv.DictReader(StringIO(content.lstrip("\ufeff")))
        if not reader.fieldnames or "name" not in reader.fieldnames:
            raise ValueError("CSV requires a name column")
        result = []
        for row in reader:
            if not (row.get("name") or "").strip():
                continue
            values = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            result.append(Candidate.model_validate({k: v or None for k, v in values.items()}))
        return result


class ManualProvider:
    def create_candidate(self, data: Candidate) -> Candidate:
        return data


class MockDiscoveryProvider:
    def discover(self, city: str, industry: str) -> list[Candidate]:
        return [
            Candidate(
                name="Demo Savannah Industrial Supply",
                website="https://example.invalid/savannah-industrial",
                industry="industrial supply distributor",
                description="Fictional demonstration account; no real company is represented.",
                city="Savannah",
                state="GA",
                latitude=32.0809,
                longitude=-81.0912,
                source_url="https://example.invalid/savannah-industrial",
                source_title="Fictional demo fixture",
                source_type="demo_fixture",
                is_demo=True,
            ),
            Candidate(
                name="Demo Lowcountry Packaging",
                website="https://example.invalid/lowcountry-packaging",
                industry="packaging supplier",
                description="Fictional demonstration account; no real company is represented.",
                city="Bluffton",
                state="SC",
                source_url="https://example.invalid/lowcountry-packaging",
                source_title="Fictional demo fixture",
                source_type="demo_fixture",
                is_demo=True,
            ),
        ]


class MockResearchProvider:
    def research(self, account: Candidate) -> tuple[ResearchOutput, dict]:
        if not account.is_demo:
            return ResearchOutput(
                account_summary="No live research performed in mock mode.",
                uncertainties=[
                    "Real company operations remain unverified; supply an API key for live research."
                ],
            ), {}
        timestamp = datetime.now(UTC)
        url = account.source_url or account.website or "https://example.invalid/demo"
        claims = [
            ("distribution", "Fictional distributor serving local business customers"),
            ("warehouse", "Fictional warehouse handling physical stock"),
            ("receiving", "Fictional receiving workflow"),
            ("orders", "Fictional purchase and sales order workflow"),
            ("inventory", "Fictional inventory visibility need"),
            ("single_site", "Fictional single-site starting scope"),
            ("repeatable", "Fictional workflows resemble distributor patterns"),
        ]
        return ResearchOutput(
            account_summary="Fictional demo research; all claims are synthetic examples.",
            evidence=[
                EvidenceInput(
                    source_url=url,
                    source_title="Fictional demo fixture",
                    retrieved_at=timestamp,
                    source_type="demo_fixture",
                    supported_claim=claim,
                    summary="Synthetic example only.",
                    evidence_level="STRONG_INFERENCE",
                    freshness_status="DEMO",
                    claim_type=kind,
                )
                for kind, claim in claims
            ],
            uncertainties=[
                "All operational details are fictional and require real-world verification."
            ],
        ), {}


class PlacesTextSearchProvider:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.google_maps_api_key:
            raise ValueError("GOOGLE_MAPS_API_KEY is required")
        self.key = settings.google_maps_api_key
        self.client = client or httpx.Client(timeout=20)

    def discover(self, city: str, industry: str) -> list[Candidate]:
        if len(city) > 100 or len(industry) > 100:
            raise ValueError("Query is too long")
        state = CITY_STATES.get(city.strip().casefold())
        if state is None:
            raise ValueError("Google discovery city must be in the territory whitelist")
        headers = {
            "X-Goog-Api-Key": self.key,
            "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.googleMapsUri,places.location",
        }

        def call():
            response = self.client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers=headers,
                json={"textQuery": f"{industry} in {city}, {state}", "pageSize": 20},
            )
            response.raise_for_status()
            return response.json()

        data = retry_request(call)
        candidates = []
        for place in data.get("places", []):
            name = place.get("displayName", {}).get("text")
            if not name:
                continue
            loc = place.get("location", {})
            actual_city, actual_state = city_from_address(place.get("formattedAddress", ""))
            candidates.append(
                Candidate(
                    name=name,
                    website=place.get("websiteUri"),
                    industry=industry,
                    local_address=place.get("formattedAddress"),
                    city=actual_city,
                    state=actual_state,
                    latitude=loc.get("latitude"),
                    longitude=loc.get("longitude"),
                    source_url=place.get("googleMapsUri"),
                    source_title=f"Google Places: {name}",
                    source_type="google_places",
                )
            )
        return candidates


class ResponsesWebResearchProvider:
    def __init__(self, settings: Settings, client: OpenAI | None = None):
        if not settings.openai_api_key or not settings.openai_research_model:
            raise ValueError("OPENAI_API_KEY and OPENAI_RESEARCH_MODEL are required")
        self.model = settings.openai_research_model
        self.client = client or OpenAI(api_key=settings.openai_api_key, timeout=45, max_retries=2)

    def research(self, account: Candidate) -> tuple[ResearchOutput, dict]:
        prompt = (
            f"Research this B2B company for warehouse workflow fit: {account.name}; "
            f"website={account.website or 'unknown'}; city={account.city or 'unknown'}. "
            "Prefer official company, contact, location and careers pages, then government, "
            "trade association and local business directories. Return only claims directly "
            "supported by public sources. Every evidence item needs its real source URL/title "
            "and UTC retrieval timestamp. Mark inference honestly. No LinkedIn scraping, "
            "login-only pages, guessed employee count/revenue/software/private problems, or "
            "claims of Warehouse Portal rollout/ROI. Use claim_type among warehouse, "
            "distribution, receiving, orders, inventory, buyer_access, deal_value, "
            "integration_simple, integration_required, single_site, repeatable, mature_wms, complex_3pl_billing, "
            "enterprise_required, transfers_required, high_support, general."
        )
        response = self.client.responses.parse(
            model=self.model,
            tools=[{"type": "web_search"}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=prompt,
            text_format=ResearchOutput,
            max_output_tokens=2500,
        )
        output = response.output_parsed
        if output is None:
            raise ValueError("Research response did not match structured schema")
        usage = response.usage
        metadata = usage.model_dump(exclude_none=True) if usage else {}
        citation_urls: set[str] = set()
        for block in response.output:
            if getattr(block, "type", None) == "web_search_call":
                action = getattr(block, "action", None)
                for source in getattr(action, "sources", []) or []:
                    url = getattr(source, "url", None)
                    if isinstance(url, str):
                        citation_urls.add(url)
                opened = getattr(action, "url", None)
                if isinstance(opened, str):
                    citation_urls.add(opened)
            for content in getattr(block, "content", []) or []:
                for annotation in getattr(content, "annotations", []) or []:
                    url = getattr(annotation, "url", None)
                    if isinstance(url, str):
                        citation_urls.add(url)
        metadata["_citation_urls"] = sorted(citation_urls)
        return output, metadata
