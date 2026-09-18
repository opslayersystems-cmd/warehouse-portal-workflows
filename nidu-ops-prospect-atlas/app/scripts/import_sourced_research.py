"""Load a reviewed public-source research batch into existing real accounts.

This is a one-time local bootstrap path, not an OpenAI provider call. Review every
source and claim before using it on a different batch.
"""

import sys
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import select

from warehouse_portal.db import SessionLocal
from warehouse_portal.models import Account
from warehouse_portal.schemas import Candidate, ResearchOutput
from warehouse_portal.services import (
    EvidenceAuditorAgent,
    OrchestratorAgent,
    audit_event,
    normalized_domain,
)


class StaticSourceProvider:
    def __init__(self, research: ResearchOutput):
        self.research_output = research

    def research(self, _account):
        return self.research_output, {
            "research_mode": "manual_public_source_review",
            "_citation_urls": sorted(
                {str(item.source_url) for item in self.research_output.evidence}
            ),
        }


def main(path: Path) -> None:
    raw = TypeAdapter(list[dict]).validate_json(path.read_bytes())
    records = []
    for item in raw:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each research record needs an account name")
        research = ResearchOutput.model_validate(
            {key: value for key, value in item.items() if key not in {"name", "website"}}
        )
        website = Candidate(name=name, website=item.get("website")).website
        if not research.evidence:
            raise ValueError(f"{name}: at least one source-backed claim is required")
        if website and normalized_domain(website) not in {
            normalized_domain(str(evidence.source_url))
            for evidence in research.evidence
            if evidence.source_type == "official_company_site"
        }:
            raise ValueError(f"{name}: website needs a matching official company source")
        audited = EvidenceAuditorAgent().run(research.evidence)
        if len(audited.accepted) != len(research.evidence):
            raise ValueError(f"{name}: an evidence item failed URL or timestamp validation")
        records.append((name, website, research))

    with SessionLocal() as db:
        manager = OrchestratorAgent(db)
        for name, website, research in records:
            matches = list(db.scalars(select(Account).where(Account.name == name)))
            if len(matches) != 1 or matches[0].is_demo:
                raise ValueError(f"{name}: expected exactly one existing real account")
            account = matches[0]
            if account.research_status == "completed":
                print(f"Skipped already researched: {name}")
                continue
            if account.pipeline_stage not in {"discovered", "deduplicated"}:
                raise ValueError(f"{name}: account is not ready for initial research")
            if website:
                domain = normalized_domain(website)
                if account.website and normalized_domain(account.website) != domain:
                    raise ValueError(f"{name}: website conflicts with existing account domain")
                other = db.scalar(
                    select(Account).where(Account.domain == domain, Account.id != account.id)
                )
                if other:
                    raise ValueError(f"{name}: website domain belongs to {other.name}")
                if not account.website:
                    account.website = website
                    account.domain = domain
                    audit_event(
                        db,
                        account.id,
                        "manual_public_source_review",
                        "official_website_added",
                        {"url": website},
                    )
            manager.research(account.id, "manual", StaticSourceProvider(research))
            result = manager.score(account.id)
            print(f"Scored {name}: {result.score} points, review required")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/import_sourced_research.py RESEARCH.json")
    main(Path(sys.argv[1]))
