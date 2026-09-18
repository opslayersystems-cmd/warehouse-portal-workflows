"""Source-backed contact candidates and durable suppression checks."""

import re
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from warehouse_portal.config import Settings, get_settings
from warehouse_portal.contact_providers import (
    ApolloPeopleSearchProvider,
    MockContactSearchProvider,
    PublicContactSearchProvider,
)
from warehouse_portal.models import Account, Contact, ProviderThrottle, Suppression, now
from warehouse_portal.schemas import ContactCandidate, SuppressionInput
from warehouse_portal.services import audit_event, recorded_run


def _contact_key(name: str) -> str:
    key = "".join(character for character in name.casefold() if character.isalnum())
    if not key:
        raise ValueError("Contact name needs letters or digits")
    return key


def _normalized_suppression(db: Session, kind: str, value: str) -> str:
    value = value.strip().casefold()
    if kind == "account":
        if db.get(Account, value) is None:
            raise LookupError("Account not found")
    elif kind == "email":
        if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", value):
            raise ValueError("Invalid suppression email")
    elif kind == "domain":
        value = value.removeprefix("www.")
        labels = value.split(".")
        if (
            len(labels) < 2
            or not re.fullmatch(r"[a-z]{2,}", labels[-1])
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels[:-1]
            )
        ):
            raise ValueError("Invalid suppression domain")
    else:
        raise ValueError("Suppression kind must be email, domain, or account")
    return value


def add_suppression(db: Session, entry: SuppressionInput, actor: str) -> Suppression:
    value = _normalized_suppression(db, entry.kind, entry.value)
    existing = db.scalar(
        select(Suppression).where(Suppression.kind == entry.kind, Suppression.value == value)
    )
    if existing is not None:
        return existing
    suppression = Suppression(kind=entry.kind, value=value, reason=entry.reason)
    db.add(suppression)
    audit_event(
        db,
        value if entry.kind == "account" else None,
        actor,
        "suppression_added",
        {"kind": entry.kind, "value": value},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Suppression).where(Suppression.kind == entry.kind, Suppression.value == value)
        )
        if existing is None:
            raise
        return existing
    db.refresh(suppression)
    return suppression


def is_suppressed(db: Session, account: Account, email: str | None = None) -> bool:
    checks = [("account", account.id)]
    if account.domain:
        checks.append(("domain", account.domain))
    if email:
        normalized = email.strip().casefold()
        checks.append(("email", normalized))
        domain = normalized.partition("@")[2]
        if domain:
            checks.append(("domain", domain))
    return any(
        db.scalar(
            select(Suppression.id).where(Suppression.kind == kind, Suppression.value == value)
        )
        is not None
        for kind, value in checks
    )


def _safe_public_contact(candidate: ContactCandidate, citations: set[str]) -> bool:
    url = str(candidate.source_url)
    host = urlparse(url).hostname or ""
    if url not in citations or host.endswith(("linkedin.com", "facebook.com", "instagram.com")):
        return False
    return candidate.source_type not in {"llm_unsourced", "manual_unsourced", "unknown"}


def _reserve_openai_slot(db: Session) -> None:
    current = now()
    with Session(db.get_bind()) as limiter:
        reserved = limiter.execute(
            update(ProviderThrottle)
            .where(
                ProviderThrottle.provider == "openai",
                ProviderThrottle.next_allowed_at <= current,
            )
            .values(next_allowed_at=current + timedelta(seconds=10))
            .returning(ProviderThrottle.provider)
            .execution_options(synchronize_session=False)
        ).first()
        if reserved is None:
            raise ValueError("OpenAI provider rate slot is busy; retry shortly")
        limiter.commit()


def _save_contact(db: Session, account: Account, item: ContactCandidate, status: str) -> Contact:
    key = _contact_key(item.name)
    existing = db.scalar(
        select(Contact).where(Contact.account_id == account.id, Contact.identity_key == key)
    )
    if existing is not None:
        if existing.verification_status == "vendor_unverified" and status == "source_attributed":
            existing.email = item.email
            existing.title = item.title
            existing.source_url = str(item.source_url)
            existing.source_title = item.source_title
            existing.source_type = item.source_type
            existing.retrieved_at = item.retrieved_at
            existing.verification_status = status
        elif existing.email is None and item.email and status == "source_attributed":
            existing.email = item.email
        return existing
    contact = Contact(
        account_id=account.id,
        identity_key=key,
        name=item.name,
        title=item.title,
        email=item.email,
        source_url=str(item.source_url),
        source_title=item.source_title,
        source_type=item.source_type,
        retrieved_at=item.retrieved_at,
        verification_status=status,
    )
    db.add(contact)
    db.flush()
    return contact


def research_contacts(
    db: Session,
    account_id: str,
    *,
    settings: Settings | None = None,
    public_provider=None,
    apollo_provider=None,
) -> list[Contact]:
    account = db.get(Account, account_id)
    if account is None:
        raise LookupError("Account not found")
    if account.pipeline_stage != "qualified":
        raise ValueError("Approve the account before researching contacts")
    if is_suppressed(db, account):
        raise ValueError("Account or domain is suppressed")
    settings = settings or get_settings()
    if not account.is_demo and not (settings.openai_api_key and settings.openai_research_model):
        raise ValueError("OPENAI_API_KEY and OPENAI_RESEARCH_MODEL are required")
    if not account.is_demo and public_provider is None:
        _reserve_openai_slot(db)
    provider = public_provider or (
        MockContactSearchProvider() if account.is_demo else PublicContactSearchProvider(settings)
    )
    with recorded_run(db, "contact_research", account_id, "ContactResearchAgent", settings) as run:
        output, citations, metadata = provider.search(account)
        run.usage_metadata = metadata
        accepted = [item for item in output.contacts[:5] if _safe_public_contact(item, citations)]
        contacts = [
            _save_contact(db, account, item, "source_attributed")
            for item in accepted
            if not is_suppressed(db, account, item.email)
        ]
        if not contacts and not account.is_demo and settings.apollo_api_key and account.domain:
            fallback = apollo_provider or ApolloPeopleSearchProvider(settings)
            for item in fallback.search(account)[:5]:
                if not is_suppressed(db, account, item.email):
                    contacts.append(_save_contact(db, account, item, "vendor_unverified"))
        run.source_count = len({contact.source_url for contact in contacts})
        audit_event(
            db,
            account_id,
            "contact_research_agent",
            "contact_research_completed",
            {"stored": len(contacts), "public_sources": len(accepted)},
        )
    db.commit()
    return contacts
