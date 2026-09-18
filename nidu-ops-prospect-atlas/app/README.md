# NIDU OPS Prospect Atlas — local application

This directory contains the FastAPI application source. The [GitHub Pages page](https://opslayersystems-cmd.github.io/warehouse-portal-workflows/nidu-ops-prospect-atlas/) is a static guide; the application runs locally with Python and SQLite. No live database, API credentials, prospect research batches, or contact records are included here.

## Run locally

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/), then run these commands from this directory:

```bash
uv sync --all-groups
cp .env.example .env
uv run alembic upgrade head
uv run warehouse-portal create-operator YOUR_USERNAME --role admin
uv run uvicorn warehouse_portal.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/login`. The operator command prompts for a password. Set a random `SESSION_SECRET_KEY` of at least 32 bytes in `.env` so sessions survive restarts. Keep `.env` and `*.db` local.

## What works

- Manual and CSV import, duplicate detection, optional Google Places discovery
- Source-attributed research records, deterministic fit score and confidence
- Pipeline stage filters, account detail, approval or reasoned disqualification
- Research jobs, approval queue, audit history, qualified CSV export
- Contact candidate research after qualification and suppression controls

Demo research uses fictional data. Automated research of real companies inside the app requires `OPENAI_API_KEY`, `OPENAI_RESEARCH_MODEL`, and a separate `warehouse-portal run-jobs --watch` worker. Source-checked research prepared outside the app can be imported with `scripts/import_sourced_research.py`. Contact and Google sign-in integrations require their respective credentials. Drafting and sending outreach are not implemented.

Run `uv run ruff check src tests migrations`, `uv run mypy src`, and `uv run pytest -q` to validate. The PostgreSQL integration test is skipped without `TEST_POSTGRES_URL`.
