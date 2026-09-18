from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from google_auth_oauthlib.flow import Flow

import warehouse_portal.google_oauth as oauth_module
import warehouse_portal.main as main_module
from warehouse_portal.auth import create_operator
from warehouse_portal.config import Settings
from warehouse_portal.db import get_db
from warehouse_portal.google_oauth import (
    GoogleIdentityRejected,
    GoogleOAuth,
    valid_pending,
    validate_identity,
)
from warehouse_portal.main import app

EMAIL = "owner@opslayersystems.example"
CLIENT_ID = "client-id.apps.googleusercontent.com"


def configured_settings(**overrides):
    values = {
        "google_allowed_email": EMAIL,
        "google_oauth_client_id": CLIENT_ID,
        "google_oauth_client_secret": "test-secret",
        "google_oauth_redirect_uri": "http://localhost:8000/auth/google/callback",
    }
    values.update(overrides)
    return Settings(**values)


def claims(**overrides):
    values = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "nonce": "expected-nonce",
        "email": EMAIL,
        "email_verified": True,
        "hd": "opslayersystems.example",
        "sub": "google-subject-1",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "override",
    [
        {"google_allowed_email": ""},
        {"google_oauth_client_id": ""},
        {"google_oauth_client_secret": ""},
        {"google_oauth_redirect_uri": ""},
        {"google_oauth_redirect_uri": "http://other.example/auth/google/callback"},
        {"google_oauth_redirect_uri": "http://localhost:8000/wrong"},
    ],
)
def test_google_oauth_fails_closed_without_exact_configuration(override):
    assert not GoogleOAuth(configured_settings(**override)).ready


@pytest.mark.parametrize(
    "override",
    [
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": "other@opslayersystems.example"},
        {"email": None},
        {"hd": None},
        {"hd": "other.example"},
        {"nonce": "replayed"},
        {"aud": "other-client"},
        {"iss": "https://other.example"},
        {"sub": ""},
    ],
)
def test_identity_gate_rejects_unverified_or_mismatched_claims(override):
    with pytest.raises(GoogleIdentityRejected):
        validate_identity(claims(**override), configured_settings(), "expected-nonce")


def test_owner_gmail_identity_is_allowed_without_workspace_claim():
    email = "opslayersystems@gmail.com"
    settings = configured_settings(google_allowed_email=email)
    verified = claims(email=email, hd=None)
    assert validate_identity(verified, settings, "expected-nonce") == (email, "google-subject-1")
    with pytest.raises(GoogleIdentityRejected):
        validate_identity({**verified, "email_verified": False}, settings, "expected-nonce")


def test_authorization_request_uses_state_nonce_and_pkce():
    provider = GoogleOAuth(configured_settings())
    url, pending = provider.begin()
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "accounts.google.com"
    assert query["client_id"] == [CLIENT_ID]
    assert set(query["scope"][0].split()) == {"openid", "email"}
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["online"]
    assert query["state"] == [pending["state"]]
    assert query["nonce"] == [pending["nonce"]]
    assert query["code_challenge_method"] == ["S256"]
    assert len(pending["verifier"]) >= 43
    assert valid_pending(pending, pending["state"])
    assert not valid_pending(pending, "wrong")
    assert not valid_pending({**pending, "issued_at": 0}, pending["state"])


def test_google_callback_binds_subject_and_rejects_replay(monkeypatch, db):
    config = configured_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: config)
    monkeypatch.setattr(main_module, "settings", config)
    seen = {"subject": "google-subject-1"}

    def fake_fetch(self, **kwargs):
        assert kwargs["code"] == "valid-code"
        assert kwargs["timeout"] == 10
        assert self.code_verifier
        return {"id_token": "signed-token"}

    def fake_verify(token, request, audience):
        assert token == "signed-token"
        assert audience == CLIENT_ID
        assert callable(request)
        return claims(nonce=seen["nonce"], sub=seen["subject"])

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch)
    monkeypatch.setattr(oauth_module, "verify_oauth2_token", fake_verify)
    operator = create_operator(db, EMAIL, "a long test password", "admin")

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:

            def begin():
                response = client.get("/auth/google/start", follow_redirects=False)
                assert response.status_code == 303
                assert "samesite=lax" in response.headers["set-cookie"].lower()
                query = parse_qs(urlparse(response.headers["location"]).query)
                seen["nonce"] = query["nonce"][0]
                return query["state"][0]

            state = begin()
            assert (
                client.get(
                    "/auth/google/callback",
                    params={"state": "wrong", "code": "valid-code"},
                    follow_redirects=False,
                ).status_code
                == 400
            )
            assert (
                client.get(
                    "/auth/google/callback",
                    params={"state": state, "code": "valid-code"},
                    follow_redirects=False,
                ).status_code
                == 400
            )
            state = begin()
            result = client.get(
                "/auth/google/callback",
                params={"state": state, "code": "valid-code"},
                follow_redirects=False,
            )
            assert result.status_code == 303
            assert result.headers["location"] == "/"
            db.refresh(operator)
            assert operator.google_subject == "google-subject-1"
            assert client.get("/api/session").json()["username"] == EMAIL
            assert (
                client.get(
                    "/auth/google/callback", params={"state": state, "code": "valid-code"}
                ).status_code
                == 400
            )

            state = begin()
            seen["subject"] = "different-google-subject"
            assert (
                client.get(
                    "/auth/google/callback",
                    params={"state": state, "code": "valid-code"},
                    follow_redirects=False,
                ).status_code
                == 403
            )
            db.refresh(operator)
            assert operator.google_subject == "google-subject-1"
    finally:
        app.dependency_overrides.clear()


def test_google_start_disabled_and_callback_requires_state(monkeypatch, db):
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings())
    monkeypatch.setattr(main_module, "settings", Settings())

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            assert "Sign in with Google" not in client.get("/login").text
            assert client.get("/auth/google/start").status_code == 503
            assert (
                client.get("/auth/google/callback", params={"state": "x", "code": "x"}).status_code
                == 503
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("claim_override", "local_operator"),
    [
        ({"email_verified": False}, True),
        ({"email": "other@opslayersystems.example"}, True),
        ({"hd": None}, True),
        ({}, False),
    ],
)
def test_google_callback_never_authenticates_rejected_identity(
    monkeypatch, db, claim_override, local_operator
):
    config = configured_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: config)
    monkeypatch.setattr(main_module, "settings", config)
    monkeypatch.setattr(Flow, "fetch_token", lambda self, **kwargs: {"id_token": "signed"})
    current_nonce = {"value": ""}
    monkeypatch.setattr(
        oauth_module,
        "verify_oauth2_token",
        lambda token, request, audience: claims(nonce=current_nonce["value"], **claim_override),
    )
    operator = (
        create_operator(db, EMAIL, "a long test password", "admin") if local_operator else None
    )

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            start = client.get("/auth/google/start", follow_redirects=False)
            query = parse_qs(urlparse(start.headers["location"]).query)
            current_nonce["value"] = query["nonce"][0]
            result = client.get(
                "/auth/google/callback",
                params={"state": query["state"][0], "code": "valid-code"},
                follow_redirects=False,
            )
            assert result.status_code == 403
            assert client.get("/api/session").status_code == 401
            if operator is not None:
                db.refresh(operator)
                assert operator.google_subject is None
    finally:
        app.dependency_overrides.clear()
