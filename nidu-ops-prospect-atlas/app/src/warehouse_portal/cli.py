from pathlib import Path
from time import sleep

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from warehouse_portal.auth import ROLES, create_operator, update_operator
from warehouse_portal.config import get_settings
from warehouse_portal.contacts import add_suppression, research_contacts
from warehouse_portal.db import SessionLocal
from warehouse_portal.jobs import (
    MAX_ENQUEUE_BATCH,
    MAX_WORKER_BATCH,
    enqueue_research,
    run_next_job,
)
from warehouse_portal.models import Account, Contact, ResearchJob
from warehouse_portal.schemas import SuppressionInput
from warehouse_portal.seed import seed_demo
from warehouse_portal.services import OrchestratorAgent, decide, qualified_csv, review_queue

app = typer.Typer(help="Warehouse Portal GTM administration")


def migrate():
    config = Config(str(Path.cwd() / "alembic.ini"))
    command.upgrade(config, "head")


@app.command("init-db")
def init_db():
    migrate()
    typer.echo("Database migrated to head")


@app.command("create-operator")
def create_operator_command(
    username: str,
    role: str = typer.Option("operator", help="viewer, operator, reviewer, or admin"),
):
    if role not in ROLES:
        raise typer.BadParameter("Role must be viewer, operator, reviewer, or admin")
    password = typer.prompt("Password (12+ characters)", hide_input=True, confirmation_prompt=True)
    migrate()
    try:
        with SessionLocal() as db:
            operator = create_operator(db, username, password, role)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Created {operator.username} ({operator.role})")


@app.command("update-operator")
def update_operator_command(
    username: str,
    role: str | None = typer.Option(None, help="Change role"),
    enable: bool = typer.Option(False, help="Enable this account"),
    disable: bool = typer.Option(False, help="Disable this account"),
    reset_password: bool = typer.Option(False, help="Prompt for a new password"),
):
    if enable and disable:
        raise typer.BadParameter("Choose either --enable or --disable")
    if role is None and not enable and not disable and not reset_password:
        raise typer.BadParameter("Choose a role, --enable, --disable, or --reset-password")
    password = (
        typer.prompt("New password (12+ characters)", hide_input=True, confirmation_prompt=True)
        if reset_password
        else None
    )
    try:
        with SessionLocal() as db:
            operator = update_operator(
                db,
                username,
                role=role,
                active=True if enable else False if disable else None,
                password=password,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"Updated {operator.username} ({operator.role}, {'active' if operator.active else 'disabled'})"
    )


@app.command("seed-demo")
def seed_demo_command():
    with SessionLocal() as db:
        ids = seed_demo(db)
    typer.echo(f"Fictional demo accounts ready: {len(ids)}")


@app.command("import-csv")
def import_csv(path: Path):
    with SessionLocal() as db:
        created, duplicates = OrchestratorAgent(db).import_csv(path.read_text(encoding="utf-8"))
    typer.echo(f"Created {created}; duplicates {duplicates}")


@app.command("discover")
def discover(
    provider: str = "mock", city: str = "Savannah", industry: str = "industrial supply distributor"
):
    with SessionLocal() as db:
        accounts = OrchestratorAgent(db).discover(provider, city, industry)
    typer.echo(f"Discovered {len(accounts)} candidates")


@app.command("research")
def research(
    account_id: str | None = None,
    all_accounts: bool = typer.Option(False, "--all"),
    provider: str = "auto",
):
    with SessionLocal() as db:
        ids = (
            [account_id]
            if account_id
            else [
                row.id
                for row in db.scalars(
                    select(Account).where(
                        Account.pipeline_stage.in_(["discovered", "deduplicated", "researched"])
                    )
                )
            ]
            if all_accounts
            else []
        )
        if not ids:
            raise typer.BadParameter("Provide ACCOUNT_ID or --all")
        manager = OrchestratorAgent(db)
        for item in ids:
            manager.research(item, provider)
        typer.echo(f"Researched {len(ids)} accounts")


@app.command("enqueue-research")
def enqueue_research_command(
    account_id: str | None = None,
    all_accounts: bool = typer.Option(False, "--all"),
    provider: str = "auto",
    limit: int = typer.Option(10, min=1, max=MAX_ENQUEUE_BATCH),
):
    if bool(account_id) == all_accounts:
        raise typer.BadParameter("Provide one ACCOUNT_ID or --all")
    with SessionLocal() as db:
        accounts = (
            []
            if account_id
            else db.scalars(
                select(Account)
                .outerjoin(ResearchJob, ResearchJob.active_account_id == Account.id)
                .where(
                    Account.pipeline_stage.in_(["discovered", "deduplicated"]),
                    ResearchJob.id.is_(None),
                )
                .order_by(Account.created_at)
                .limit(limit)
            ).all()
        )
        ids = [account_id] if account_id else [row.id for row in accounts]
        if not ids:
            raise typer.BadParameter("No researchable accounts")
        if (
            accounts
            and provider in {"auto", "openai"}
            and any(not row.is_demo or provider == "openai" for row in accounts)
        ):
            settings = get_settings()
            if not (settings.openai_api_key and settings.openai_research_model):
                raise typer.BadParameter("OPENAI_API_KEY and OPENAI_RESEARCH_MODEL are required")
        if accounts and provider == "mock" and any(not row.is_demo for row in accounts):
            raise typer.BadParameter("Mock jobs are limited to fictional demo accounts")
        try:
            jobs = [enqueue_research(db, item, provider) for item in ids]
        except (LookupError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Queued {len(jobs)} research jobs")


@app.command("run-jobs")
def run_jobs_command(
    max_jobs: int = typer.Option(10, min=1, max=MAX_WORKER_BATCH),
    watch: bool = typer.Option(False, help="Poll until max-jobs have been processed"),
    poll_seconds: float = typer.Option(2.0, min=0.5, max=60.0),
):
    processed = 0
    while processed < max_jobs:
        result = run_next_job(SessionLocal)
        if result is None:
            if not watch:
                break
            sleep(poll_seconds)
            continue
        processed += 1
        typer.echo(f"{result.id}: {result.status}")
    typer.echo(f"Processed {processed} research jobs")


@app.command("score")
def score(account_id: str | None = None, all_accounts: bool = typer.Option(False, "--all")):
    with SessionLocal() as db:
        ids = (
            [account_id]
            if account_id
            else [
                row.id
                for row in db.scalars(select(Account).where(Account.pipeline_stage == "researched"))
            ]
            if all_accounts
            else []
        )
        if not ids:
            raise typer.BadParameter("Provide ACCOUNT_ID or --all")
        manager = OrchestratorAgent(db)
        for item in ids:
            manager.score(item)
        typer.echo(f"Scored {len(ids)} accounts")


@app.command("review-queue")
def list_review_queue():
    with SessionLocal() as db:
        for account in review_queue(db):
            typer.echo(f"{account.id}  {account.name}  {account.geographic_tier}")


@app.command("research-contacts")
def research_contacts_command(account_id: str):
    try:
        with SessionLocal() as db:
            contacts = research_contacts(db, account_id)
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Stored {len(contacts)} sourced contact candidates")


@app.command("list-contacts")
def list_contacts_command(account_id: str):
    with SessionLocal() as db:
        for contact in db.scalars(select(Contact).where(Contact.account_id == account_id)):
            typer.echo(
                f"{contact.name} | {contact.title or 'Role unknown'} | "
                f"{contact.email or 'Email unknown'} | {contact.verification_status}"
            )


@app.command("suppress")
def suppress_command(kind: str, value: str, reason: str = typer.Option(...)):
    try:
        entry = SuppressionInput(kind=kind, value=value, reason=reason)
        with SessionLocal() as db:
            suppression = add_suppression(db, entry, "trusted_cli")
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Suppressed {suppression.kind}: {suppression.value}")


@app.command("approve")
def approve(account_id: str, reviewer: str = typer.Option(...), reason: str | None = None):
    with SessionLocal() as db:
        account = decide(db, account_id, reviewer, "approved", reason)
    typer.echo(f"{account.name}: {account.pipeline_stage}")


@app.command("disqualify")
def disqualify(account_id: str, reviewer: str = typer.Option(...), reason: str = typer.Option(...)):
    with SessionLocal() as db:
        account = decide(db, account_id, reviewer, "disqualified", reason)
    typer.echo(f"{account.name}: {account.pipeline_stage}")


@app.command("export-qualified")
def export_qualified(path: Path):
    with SessionLocal() as db:
        content = qualified_csv(db)
    path.write_text(content, encoding="utf-8")
    typer.echo(f"Wrote {path}")


if __name__ == "__main__":
    app()
