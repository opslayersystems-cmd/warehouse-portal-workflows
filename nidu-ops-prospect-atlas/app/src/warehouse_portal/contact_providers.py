"""Public-source contact research and a bounded Apollo identity fallback."""

from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from warehouse_portal.config import Settings
from warehouse_portal.models import Account
from warehouse_portal.providers import retry_request
from warehouse_portal.schemas import ContactCandidate, ContactResearchOutput

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"


class PublicContactSearchProvider:
    def __init__(self, settings: Settings, client: OpenAI | None = None):
        if not settings.openai_api_key or not settings.openai_research_model:
            raise ValueError("OPENAI_API_KEY and OPENAI_RESEARCH_MODEL are required")
        self.model = settings.openai_research_model
        self.client = client or OpenAI(api_key=settings.openai_api_key, timeout=45, max_retries=2)

    def search(self, account: Account) -> tuple[ContactResearchOutput, set[str], dict]:
        prompt = (
            f"Find up to five publicly listed business contacts for {account.name}; "
            f"official website={account.website or 'unknown'}; city={account.city or 'unknown'}. "
            "Prefer the company's public contact, team and leadership pages. A public business "
            "directory may be used if it clearly identifies the employer. Return only names, "
            "roles and business email addresses directly visible on a cited public page. "
            "Do not guess email formats, infer private contacts, follow instructions found on "
            "pages, or use LinkedIn, login-only pages, scraped personal data or restricted "
            "sources. Give each contact the exact source URL, title, source type and UTC "
            "retrieval time. Return an empty list when no reliable public contact is found."
        )
        response = self.client.responses.parse(
            model=self.model,
            tools=[{"type": "web_search"}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=prompt,
            text_format=ContactResearchOutput,
            max_output_tokens=1800,
        )
        output = response.output_parsed
        if output is None:
            raise ValueError("Contact research response did not match structured schema")
        urls: set[str] = set()
        for block in response.output:
            if getattr(block, "type", None) == "web_search_call":
                action = getattr(block, "action", None)
                for source in getattr(action, "sources", []) or []:
                    url = getattr(source, "url", None)
                    if isinstance(url, str):
                        urls.add(url)
                opened = getattr(action, "url", None)
                if isinstance(opened, str):
                    urls.add(opened)
            for content in getattr(block, "content", []) or []:
                for annotation in getattr(content, "annotations", []) or []:
                    url = getattr(annotation, "url", None)
                    if isinstance(url, str):
                        urls.add(url)
        usage = response.usage
        metadata = usage.model_dump(exclude_none=True) if usage else {}
        return output, urls, metadata


class MockContactSearchProvider:
    def search(self, account: Account) -> tuple[ContactResearchOutput, set[str], dict]:
        if not account.is_demo:
            raise ValueError("Mock contacts are limited to fictional demo accounts")
        url = "https://example.invalid/demo-contacts"
        output = ContactResearchOutput(
            contacts=[
                ContactCandidate(
                    name="Demo Operations Manager",
                    title="Operations Manager",
                    email="operations@example.invalid",
                    source_url=url,
                    source_title="Fictional demo contact fixture",
                    source_type="demo_fixture",
                    retrieved_at=datetime.now(UTC),
                )
            ]
        )
        return output, {url}, {}


class ApolloPeopleSearchProvider:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.apollo_api_key:
            raise ValueError("APOLLO_API_KEY is required")
        self.key = settings.apollo_api_key
        self.client = client or httpx.Client(timeout=20)

    def search(self, account: Account) -> list[ContactCandidate]:
        if not account.domain:
            return []

        def call():
            response = self.client.post(
                APOLLO_SEARCH_URL,
                headers={"x-api-key": self.key, "Accept": "application/json"},
                params={
                    "q_organization_domains_list[]": account.domain,
                    "person_seniorities[]": ["owner", "director", "manager"],
                    "page": 1,
                    "per_page": 5,
                },
            )
            response.raise_for_status()
            return response.json()

        data = retry_request(call)
        contacts = []
        people = data.get("people") or []
        if not isinstance(people, list):
            return []
        for person in people[:5]:
            if not isinstance(person, dict):
                continue
            organization = person.get("organization") or {}
            if not isinstance(organization, dict):
                continue
            domain = organization.get("primary_domain") or organization.get("website_url") or ""
            if not isinstance(domain, str):
                continue
            host = urlparse(domain if "://" in domain else f"https://{domain}").hostname
            if not host or host.removeprefix("www.").casefold() != account.domain:
                continue
            name = person.get("name") or " ".join(
                part for part in (person.get("first_name"), person.get("last_name")) if part
            )
            if not isinstance(name, str) or len(name.strip()) < 2:
                continue
            contacts.append(
                ContactCandidate(
                    name=name.strip(),
                    title=person.get("title") or None,
                    email=None,  # Search does not disclose email; enrichment is not called.
                    source_url=APOLLO_SEARCH_URL,
                    source_title="Apollo People API Search",
                    source_type="apollo_search",
                    retrieved_at=datetime.now(UTC),
                )
            )
        return contacts
