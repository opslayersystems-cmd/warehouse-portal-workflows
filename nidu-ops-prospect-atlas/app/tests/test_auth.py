import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from warehouse_portal.auth import create_operator, update_operator, verify_password
from warehouse_portal.config import Settings
from warehouse_portal.db import get_db
from warehouse_portal.main import app
from warehouse_portal.models import Approval, Operator

PASSWORD = "long local test password"


@pytest.fixture
def client(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as browser:
            yield browser
    finally:
        app.dependency_overrides.clear()


def login(client, username):
    response = client.post("/api/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/api/init-db", {}),
        ("/api/seed-demo", {}),
        ("/api/import-csv", {"files": {"file": ("input.csv", "name\nExample\n")}}),
        ("/api/discover", {"json": {"provider": "mock"}}),
        ("/api/accounts", {"json": {"name": "Example"}}),
        ("/api/accounts/missing/research", {}),
        ("/api/accounts/missing/research-jobs", {}),
        ("/api/accounts/missing/contacts/research", {}),
        (
            "/api/suppressions",
            {"json": {"kind": "email", "value": "x@example.com", "reason": "No"}},
        ),
        ("/api/accounts/missing/score", {}),
        ("/api/accounts/missing/approve", {"json": {}}),
        ("/api/accounts/missing/disqualify", {"json": {"reason": "No fit"}}),
        ("/ui/discover", {"data": {"provider": "mock"}}),
        ("/ui/manual", {"data": {"name": "Example"}}),
        ("/ui/import-csv", {"files": {"file": ("input.csv", "name\nExample\n")}}),
        ("/ui/accounts/missing/research", {}),
        ("/ui/accounts/missing/score", {}),
        ("/ui/accounts/missing/decision", {"data": {"decision": "approved"}}),
        ("/logout", {}),
        ("/api/logout", {}),
    ],
)
def test_every_mutation_requires_a_session(client, path, kwargs):
    assert client.post(path, **kwargs).status_code == 401


def test_login_roles_csrf_and_session_reviewer(client, db):
    create_operator(db, "viewer", PASSWORD, "viewer")
    create_operator(db, "worker", PASSWORD, "operator")
    create_operator(db, "reviewer", PASSWORD, "reviewer")
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert client.get("/api/accounts").status_code == 401
    assert (
        client.post("/api/login", json={"username": "viewer", "password": "wrong"}).status_code
        == 401
    )

    viewer_headers = login(client, "viewer")
    assert client.get("/").status_code == 200
    assert (
        client.post("/api/discover", json={"provider": "mock"}, headers=viewer_headers).status_code
        == 403
    )
    assert "Run discovery" not in client.get("/discovery").text

    worker_headers = login(client, "worker")
    assert client.post("/api/discover", json={"provider": "mock"}).status_code == 403
    assert (
        client.post(
            "/api/discover", json={"provider": "mock"}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    discovered = client.post("/api/discover", json={"provider": "mock"}, headers=worker_headers)
    assert discovered.status_code == 200
    account_id = discovered.json()["account_ids"][0]
    assert (
        client.post(
            f"/api/accounts/{account_id}/research?provider=mock", headers=worker_headers
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/accounts/{account_id}/score", headers=worker_headers).status_code == 200
    )
    assert (
        client.post(
            f"/api/accounts/{account_id}/approve", json={}, headers=worker_headers
        ).status_code
        == 403
    )
    assert "Approve as qualified" not in client.get(f"/accounts/{account_id}").text

    reviewer_headers = login(client, "reviewer")
    forged = client.post(
        f"/api/accounts/{account_id}/approve",
        json={"reviewer": "forged"},
        headers=reviewer_headers,
    )
    assert forged.status_code == 422
    assert (
        client.post(
            f"/api/accounts/{account_id}/approve", json={}, headers=reviewer_headers
        ).status_code
        == 200
    )
    approval = db.scalar(select(Approval).where(Approval.account_id == account_id))
    assert approval.reviewer == "reviewer"
    assert client.post("/api/logout", headers=reviewer_headers).status_code == 200
    assert client.get("/api/accounts").status_code == 401


def test_ui_forms_and_revoked_session(client, db):
    operator = create_operator(db, "local.admin", PASSWORD, "admin")
    assert verify_password(PASSWORD, operator.password_hash)
    assert not verify_password("wrong", operator.password_hash)
    signed_in = client.post("/login", data={"username": "LOCAL.ADMIN", "password": PASSWORD})
    assert signed_in.status_code == 200
    session = client.get("/api/session").json()
    token = session["csrf_token"]
    assert token in client.get("/discovery").text
    assert client.post("/ui/manual", data={"name": "Blocked"}).status_code == 403
    created = client.post("/ui/manual", data={"name": "Allowed", "csrf_token": token})
    assert created.status_code == 200
    assert "Allowed" in created.text
    update_operator(db, "local.admin", active=False)
    assert client.get("/api/session").status_code == 401
    assert (
        client.post("/ui/manual", data={"name": "Blocked", "csrf_token": token}).status_code == 401
    )


def test_role_and_password_session_version_checked_on_each_request(client, db):
    create_operator(db, "manager", PASSWORD, "admin")
    headers = login(client, "manager")
    update_operator(db, "manager", role="viewer")
    assert client.post("/api/seed-demo", headers=headers).status_code == 401
    headers = login(client, "manager")
    assert client.post("/api/seed-demo", headers=headers).status_code == 403
    update_operator(db, "manager", password="a new long test password")
    assert client.get("/api/session").status_code == 401


def test_duplicate_operator_and_short_password(db):
    create_operator(db, "example", PASSWORD, "viewer")
    with pytest.raises(ValueError):
        create_operator(db, "EXAMPLE", PASSWORD, "admin")
    with pytest.raises(ValueError):
        create_operator(db, "other", "too short", "operator")
    assert db.scalar(select(Operator).where(Operator.username == "other")) is None


def test_configured_session_secret_must_be_long():
    with pytest.raises(ValidationError):
        Settings(session_secret_key="short")
