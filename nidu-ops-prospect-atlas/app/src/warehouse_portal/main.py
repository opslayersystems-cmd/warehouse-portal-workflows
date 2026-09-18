import secrets
from collections.abc import Sequence
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from warehouse_portal.auth import authenticate, current_operator, require_mutation, start_session
from warehouse_portal.config import get_settings
from warehouse_portal.contacts import add_suppression, is_suppressed, research_contacts
from warehouse_portal.db import get_db
from warehouse_portal.google_oauth import (
    GoogleIdentityRejected,
    GoogleOAuth,
    GoogleOAuthDisabled,
    valid_pending,
)
from warehouse_portal.jobs import enqueue_research
from warehouse_portal.models import (
    Account,
    AgentRun,
    Approval,
    AuditEvent,
    Contact,
    FitScore,
    Operator,
    Opportunity,
    ResearchJob,
    Suppression,
)
from warehouse_portal.product_truth import CAPABILITIES, DESIGN_PARTNER, PILOT_STATUS
from warehouse_portal.schemas import (
    Candidate,
    DecisionInput,
    DiscoveryRequest,
    LoginInput,
    SuppressionInput,
)
from warehouse_portal.seed import seed_demo
from warehouse_portal.services import (
    LIVE_STAGES,
    OrchestratorAgent,
    dashboard_counts,
    decide,
    google_discovery_usage,
    qualified_csv,
    review_queue,
)

structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)
log = structlog.get_logger()
app = FastAPI(title="Warehouse Portal GTM", version="0.1.0")
settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key or secrets.token_urlsafe(32),
    max_age=8 * 60 * 60,
    same_site="lax",
    https_only=settings.session_https_only,
)
ROOT = Path(__file__).parent
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.exception_handler(HTTPException)
async def auth_error(request: Request, exc: HTTPException):
    if (
        exc.status_code == 401
        and request.method == "GET"
        and not request.url.path.startswith("/api/")
    ):
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=303)
    return await http_exception_handler(request, exc)


def _safe_next(value: str) -> str:
    return (
        value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": False, "google_ready": GoogleOAuth(settings).ready},
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    operator = authenticate(db, username, password)
    if operator is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": True, "google_ready": GoogleOAuth(settings).ready},
            status_code=401,
        )
    start_session(request, operator)
    return RedirectResponse(_safe_next(next), status_code=303)


@app.get("/auth/google/start")
def google_start(request: Request):
    try:
        url, pending = GoogleOAuth(get_settings()).begin()
    except GoogleOAuthDisabled as exc:
        raise HTTPException(503, "Google sign-in is not configured") from exc
    request.session["google_oauth"] = pending
    return RedirectResponse(url, status_code=303)


@app.get("/auth/google/callback")
def google_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    pending = request.session.pop("google_oauth", None)
    provider = GoogleOAuth(get_settings())
    if not provider.ready:
        raise HTTPException(503, "Google sign-in is not configured")
    if not valid_pending(pending, state) or error or not code:
        raise HTTPException(400, "Invalid or expired Google sign-in response")
    try:
        email, subject = provider.finish(code, pending)
    except GoogleIdentityRejected as exc:
        raise HTTPException(403, "Google identity is not allowed") from exc
    except GoogleOAuthDisabled as exc:
        raise HTTPException(503, "Google sign-in is not configured") from exc
    except Exception as exc:
        log.warning("google_oauth_failed", error_type=type(exc).__name__)
        raise HTTPException(502, "Google sign-in could not be verified") from exc

    operator = db.scalar(select(Operator).where(Operator.username == email))
    if operator is None or not operator.active:
        raise HTTPException(403, "No active local operator for this Google account")
    if operator.google_subject is not None and not secrets.compare_digest(
        operator.google_subject, subject
    ):
        raise HTTPException(403, "Google account binding does not match")
    if operator.google_subject is None:
        operator.google_subject = subject
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(403, "Google account binding does not match") from exc
    start_session(request, operator)
    return RedirectResponse("/", status_code=303)


@app.post("/api/login")
def api_login(credentials: LoginInput, request: Request, db: Session = Depends(get_db)):
    operator = authenticate(db, credentials.username, credentials.password)
    if operator is None:
        raise HTTPException(401, "Invalid credentials")
    token = start_session(request, operator)
    return {"username": operator.username, "role": operator.role, "csrf_token": token}


@app.get("/api/session")
def api_session(request: Request, operator: Operator = Depends(current_operator)):
    return {
        "username": operator.username,
        "role": operator.role,
        "csrf_token": request.session["csrf"],
    }


@app.post("/logout")
def logout(request: Request, _: Operator = Depends(require_mutation("viewer"))):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/logout")
def api_logout(request: Request, _: Operator = Depends(require_mutation("viewer"))):
    request.session.clear()
    return {"status": "signed_out"}


def _account(db: Session, account_id: str) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    return account


def _latest(db: Session, model, account_id: str):
    return db.scalar(
        select(model).where(model.account_id == account_id).order_by(model.created_at.desc())
    )


def _score_map(db: Session, accounts: Sequence[Account]) -> dict[str, FitScore]:
    ids = [account.id for account in accounts]
    if not ids:
        return {}
    scores: dict[str, FitScore] = {}
    for row in db.scalars(
        select(FitScore)
        .where(FitScore.account_id.in_(ids))
        .order_by(FitScore.created_at.desc(), FitScore.id.desc())
    ):
        scores.setdefault(row.account_id, row)
    return scores


def _handle_error(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc)) from exc
    log.error("workflow_failure", error_type=type(exc).__name__)
    raise HTTPException(502, "Provider or workflow failed; inspect agent runs") from exc


@app.post("/api/init-db")
def api_init_db(_: Operator = Depends(require_mutation("admin"))):
    config = Config(str(Path.cwd() / "alembic.ini"))
    command.upgrade(config, "head")
    return {"status": "migrated"}


@app.post("/api/seed-demo")
def api_seed_demo(db: Session = Depends(get_db), _: Operator = Depends(require_mutation("admin"))):
    return {"account_ids": seed_demo(db), "fictional": True}


@app.post("/api/import-csv")
async def api_import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        content = (await file.read()).decode("utf-8-sig")
        created, duplicates = OrchestratorAgent(db).import_csv(content)
        return {"created": created, "duplicates": duplicates}
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/discover")
def api_discover(
    request: DiscoveryRequest,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        accounts = OrchestratorAgent(db).discover(request.provider, request.city, request.industry)
        return {"account_ids": [a.id for a in accounts]}
    except Exception as exc:
        _handle_error(exc)


@app.post("/api/accounts")
def api_create_account(
    candidate: Candidate,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    account, created = OrchestratorAgent(db).add_candidate(candidate, "manual")
    db.commit()
    return {"id": account.id, "created": created}


@app.get("/api/accounts")
def api_accounts(db: Session = Depends(get_db), _: Operator = Depends(current_operator)):
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    scores = _score_map(db, accounts)
    return [
        {
            "id": a.id,
            "name": a.name,
            "city": a.city,
            "tier": a.geographic_tier,
            "stage": a.pipeline_stage,
            "score": scores[a.id].score if a.id in scores else None,
            "confidence": scores[a.id].confidence_score if a.id in scores else None,
            "demo": a.is_demo,
        }
        for a in accounts
    ]


@app.post("/api/accounts/{account_id}/research")
def api_research(
    account_id: str,
    provider: str = "auto",
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        return OrchestratorAgent(db).research(account_id, provider)
    except Exception as exc:
        db.rollback()
        _handle_error(exc)


@app.post("/api/accounts/{account_id}/research-jobs")
def api_enqueue_research(
    account_id: str,
    provider: str = "auto",
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        job = enqueue_research(db, account_id, provider)
    except Exception as exc:
        _handle_error(exc)
    return {
        "id": job.id,
        "account_id": job.account_id,
        "provider": job.provider,
        "status": job.status,
    }


@app.get("/api/research-jobs")
def api_research_jobs(db: Session = Depends(get_db), _: Operator = Depends(current_operator)):
    jobs = db.scalars(select(ResearchJob).order_by(ResearchJob.created_at.desc()).limit(100)).all()
    return [
        {
            "id": job.id,
            "account_id": job.account_id,
            "provider": job.provider,
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "last_error_class": job.last_error_class,
        }
        for job in jobs
    ]


@app.post("/api/accounts/{account_id}/score")
def api_score(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        return OrchestratorAgent(db).score(account_id)
    except Exception as exc:
        db.rollback()
        _handle_error(exc)


@app.get("/api/review-queue")
def api_review_queue(db: Session = Depends(get_db), _: Operator = Depends(current_operator)):
    return [
        {"id": a.id, "name": a.name, "city": a.city, "stage": a.pipeline_stage}
        for a in review_queue(db)
    ]


@app.post("/api/accounts/{account_id}/contacts/research")
def api_contact_research(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("reviewer")),
):
    try:
        contacts = research_contacts(db, account_id)
        return {"account_id": account_id, "contact_ids": [contact.id for contact in contacts]}
    except Exception as exc:
        db.rollback()
        _handle_error(exc)


@app.get("/api/accounts/{account_id}/contacts")
def api_contacts(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(current_operator),
):
    account = _account(db, account_id)
    return [
        {
            "id": contact.id,
            "name": contact.name,
            "title": contact.title,
            "email": contact.email,
            "source_url": contact.source_url,
            "source_type": contact.source_type,
            "verification_status": contact.verification_status,
            "suppressed": is_suppressed(db, account, contact.email),
        }
        for contact in db.scalars(select(Contact).where(Contact.account_id == account_id))
    ]


@app.post("/api/suppressions")
def api_add_suppression(
    entry: SuppressionInput,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_mutation("reviewer")),
):
    try:
        suppression = add_suppression(db, entry, operator.username)
        return {"id": suppression.id, "kind": suppression.kind, "value": suppression.value}
    except Exception as exc:
        db.rollback()
        _handle_error(exc)


@app.get("/api/suppressions")
def api_suppressions(db: Session = Depends(get_db), _: Operator = Depends(current_operator)):
    return [
        {"id": item.id, "kind": item.kind, "value": item.value, "reason": item.reason}
        for item in db.scalars(select(Suppression).order_by(Suppression.created_at.desc()))
    ]


@app.post("/api/accounts/{account_id}/approve")
def api_approve(
    account_id: str,
    decision: DecisionInput,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_mutation("reviewer")),
):
    try:
        account = decide(db, account_id, operator.username, "approved", decision.reason)
        return {"id": account.id, "stage": account.pipeline_stage}
    except Exception as exc:
        _handle_error(exc)


@app.post("/api/accounts/{account_id}/disqualify")
def api_disqualify(
    account_id: str,
    decision: DecisionInput,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_mutation("reviewer")),
):
    try:
        account = decide(db, account_id, operator.username, "disqualified", decision.reason)
        return {"id": account.id, "stage": account.pipeline_stage}
    except Exception as exc:
        _handle_error(exc)


@app.get("/api/export-qualified")
def api_export(db: Session = Depends(get_db), _: Operator = Depends(current_operator)):
    return Response(
        content=qualified_csv(db),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=qualified.csv"},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, db: Session = Depends(get_db), _: Operator = Depends(current_operator)
):
    counts = dashboard_counts(db)
    recent = list(db.scalars(select(Account).order_by(Account.updated_at.desc()).limit(8)))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "counts": counts,
            "recent": recent,
            "scores": _score_map(db, recent),
            "review_count": counts.get("needs_review", 0),
        },
    )


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    q: str = "",
    stage: str = "",
    db: Session = Depends(get_db),
    _: Operator = Depends(current_operator),
):
    if stage and stage not in LIVE_STAGES:
        raise HTTPException(400, "Unknown pipeline stage")
    query = select(Account).order_by(Account.updated_at.desc())
    if q:
        query = query.where(Account.name.ilike(f"%{q}%"))
    if stage:
        query = query.where(Account.pipeline_stage == stage)
    accounts = list(db.scalars(query))
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "active": "accounts",
            "accounts": accounts,
            "scores": _score_map(db, accounts),
            "q": q,
            "stage": stage,
            "stage_options": [
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
            ],
        },
    )


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(current_operator),
):
    account = _account(db, account_id)
    contacts = db.scalars(
        select(Contact).where(Contact.account_id == account_id).order_by(Contact.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "active": "accounts",
            "account": account,
            "contacts": contacts,
            "account_suppressed": is_suppressed(db, account),
            "suppressed_contact_ids": {
                contact.id for contact in contacts if is_suppressed(db, account, contact.email)
            },
            "research_job": _latest(db, ResearchJob, account_id),
            "openai_ready": bool(settings.openai_api_key and settings.openai_research_model),
            "score": _latest(db, FitScore, account_id),
            "opportunity": _latest(db, Opportunity, account_id),
            "audit": db.scalars(
                select(AuditEvent)
                .where(AuditEvent.account_id == account_id)
                .order_by(AuditEvent.created_at.desc())
            ).all(),
            "approvals": db.scalars(
                select(Approval).where(Approval.account_id == account_id)
            ).all(),
        },
    )


@app.get("/discovery", response_class=HTMLResponse)
def discovery_page(
    request: Request,
    db: Session = Depends(get_db),
    _: Operator = Depends(current_operator),
):
    return templates.TemplateResponse(
        request,
        "discovery.html",
        {
            "active": "discovery",
            "google_used": google_discovery_usage(db),
            "google_limit": get_settings().google_discovery_monthly_limit,
        },
    )


@app.post("/ui/discover")
def ui_discover(
    provider: str = Form("mock"),
    city: str = Form("Savannah"),
    industry: str = Form("industrial supply distributor"),
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        OrchestratorAgent(db).discover(provider, city, industry)
    except Exception as exc:
        _handle_error(exc)
    return RedirectResponse("/accounts", status_code=303)


@app.post("/ui/manual")
def ui_manual(
    name: str = Form(...),
    website: str = Form(""),
    industry: str = Form(""),
    city: str = Form(""),
    state: str = Form("GA"),
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    candidate = Candidate(
        name=name,
        website=website or None,
        industry=industry or None,
        city=city or None,
        state=state or None,
    )
    account, _created = OrchestratorAgent(db).add_candidate(candidate, "manual")
    db.commit()
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@app.post("/ui/import-csv")
async def ui_import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        OrchestratorAgent(db).import_csv((await file.read()).decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/accounts", status_code=303)


@app.get("/research-queue", response_class=HTMLResponse)
def research_queue_page(
    request: Request, db: Session = Depends(get_db), _: Operator = Depends(current_operator)
):
    accounts = db.scalars(
        select(Account)
        .where(Account.pipeline_stage.in_(["discovered", "deduplicated", "researched"]))
        .order_by(Account.updated_at.desc())
    ).all()
    jobs: dict[str, ResearchJob] = {}
    for job in db.scalars(select(ResearchJob).order_by(ResearchJob.created_at.desc())):
        jobs.setdefault(job.account_id, job)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "active": "research",
            "title": "Research queue",
            "accounts": accounts,
            "scores": _score_map(db, accounts),
            "jobs": jobs,
            "action": "research",
            "button": "Queue research",
            "openai_ready": bool(settings.openai_api_key and settings.openai_research_model),
        },
    )


@app.get("/scoring-review", response_class=HTMLResponse)
def scoring_page(
    request: Request, db: Session = Depends(get_db), _: Operator = Depends(current_operator)
):
    accounts = db.scalars(
        select(Account)
        .where(Account.pipeline_stage.in_(["researched", "scored"]))
        .order_by(Account.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "active": "scoring",
            "title": "Scoring review",
            "accounts": accounts,
            "scores": _score_map(db, accounts),
            "action": "score",
            "button": "Score account",
        },
    )


@app.get("/approval-queue", response_class=HTMLResponse)
def approval_page(
    request: Request, db: Session = Depends(get_db), _: Operator = Depends(current_operator)
):
    accounts = review_queue(db)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "active": "approval",
            "title": "Approval queue",
            "accounts": accounts,
            "scores": _score_map(db, accounts),
            "action": "review",
            "button": "Review details",
        },
    )


@app.post("/ui/accounts/{account_id}/research")
def ui_research(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        enqueue_research(db, account_id)
    except Exception as exc:
        db.rollback()
        _handle_error(exc)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/ui/accounts/{account_id}/contacts/research")
def ui_contact_research(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("reviewer")),
):
    try:
        research_contacts(db, account_id)
    except Exception as exc:
        db.rollback()
        _handle_error(exc)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/ui/accounts/{account_id}/suppress")
def ui_suppress_account(
    account_id: str,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_mutation("reviewer")),
):
    try:
        add_suppression(
            db,
            SuppressionInput(kind="account", value=account_id, reason=reason),
            operator.username,
        )
    except Exception as exc:
        db.rollback()
        _handle_error(exc)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/ui/accounts/{account_id}/score")
def ui_score(
    account_id: str,
    db: Session = Depends(get_db),
    _: Operator = Depends(require_mutation("operator")),
):
    try:
        OrchestratorAgent(db).score(account_id)
    except Exception as exc:
        db.rollback()
        _handle_error(exc)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.post("/ui/accounts/{account_id}/decision")
def ui_decision(
    account_id: str,
    decision: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_mutation("reviewer")),
):
    try:
        decide(db, account_id, operator.username, decision, reason or None)
    except Exception as exc:
        _handle_error(exc)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.get("/agent-runs", response_class=HTMLResponse)
def runs_page(
    request: Request, db: Session = Depends(get_db), _: Operator = Depends(current_operator)
):
    runs = db.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(100)).all()
    return templates.TemplateResponse(request, "runs.html", {"active": "runs", "runs": runs})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: Operator = Depends(current_operator)):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "settings": settings,
            "openai_ready": bool(settings.openai_api_key and settings.openai_research_model),
            "google_ready": bool(settings.google_maps_api_key),
            "google_oauth_ready": GoogleOAuth(settings).ready,
            "capabilities": CAPABILITIES,
            "design_partner": DESIGN_PARTNER,
            "pilot_status": PILOT_STATUS,
        },
    )
