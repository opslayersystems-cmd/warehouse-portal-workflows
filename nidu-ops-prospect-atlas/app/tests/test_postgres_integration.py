"""Opt-in migration and worker smoke against an isolated PostgreSQL schema."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from warehouse_portal.config import get_settings
from warehouse_portal.jobs import enqueue_research, run_next_job
from warehouse_portal.models import ResearchJob
from warehouse_portal.schemas import Candidate
from warehouse_portal.services import OrchestratorAgent


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL is not set")
def test_postgres_migration_and_durable_job(monkeypatch):
    base_url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(base_url).get_backend_name() == "postgresql"
    schema = f"wp_test_{uuid4().hex[:12]}"
    admin_engine = create_engine(base_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    schema_url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(schema_url)
    try:
        monkeypatch.setenv("DATABASE_URL", str(schema_url))
        get_settings.cache_clear()
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names(schema=schema))
        assert {
            "accounts",
            "operators",
            "research_jobs",
            "provider_throttles",
            "contacts",
            "suppressions",
        } <= tables
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with Session(engine, expire_on_commit=False) as db:
            account, _ = OrchestratorAgent(db).add_candidate(
                Candidate(name="Postgres Fictional Test", city="Savannah", is_demo=True)
            )
            db.commit()
            job = enqueue_research(db, account.id)
        assert run_next_job(factory).status == "completed"
        with Session(engine) as db:
            assert db.get(ResearchJob, job.id).status == "completed"
    finally:
        get_settings.cache_clear()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
