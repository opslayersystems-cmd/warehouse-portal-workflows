"""Google OpenID Connect sign-in, limited to one configured Google identity."""

import hmac
import secrets
import time
from functools import partial
from typing import Any
from urllib.parse import urlparse

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from google_auth_oauthlib.flow import Flow

from warehouse_portal.auth import normalized_username
from warehouse_portal.config import Settings

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = ["openid", "email"]
PENDING_LIFETIME_SECONDS = 600


class GoogleOAuthDisabled(Exception):
    pass


class GoogleIdentityRejected(Exception):
    pass


def allowed_email(settings: Settings) -> str:
    try:
        email = normalized_username(settings.google_allowed_email)
    except ValueError as exc:
        raise GoogleOAuthDisabled("Google allowed email is not configured") from exc
    if email.count("@") != 1:
        raise GoogleOAuthDisabled("Google allowed email is not configured")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise GoogleOAuthDisabled("Google allowed email is not configured")
    return email


def validate_identity(claims: dict[str, Any], settings: Settings, nonce: str) -> tuple[str, str]:
    email = allowed_email(settings)
    actual_email = claims.get("email")
    subject = claims.get("sub")
    if (
        claims.get("iss") not in {"https://accounts.google.com", "accounts.google.com"}
        or claims.get("aud") != settings.google_oauth_client_id
        or claims.get("nonce") != nonce
        or claims.get("email_verified") is not True
        or not isinstance(actual_email, str)
        or not hmac.compare_digest(actual_email.casefold().encode(), email.encode())
        or not isinstance(subject, str)
        or not subject
    ):
        raise GoogleIdentityRejected("Google identity is not allowed")
    domain = email.split("@", 1)[1]
    hosted_domain = claims.get("hd")
    if domain not in {"gmail.com", "googlemail.com"} and (
        not isinstance(hosted_domain, str) or hosted_domain.casefold() != domain
    ):
        raise GoogleIdentityRejected("Google Workspace domain is not verified")
    return email, subject


class GoogleOAuth:
    def __init__(self, settings: Settings):
        self.settings = settings

    def require_config(self) -> str:
        email = allowed_email(self.settings)
        redirect = urlparse(self.settings.google_oauth_redirect_uri)
        local_http = redirect.scheme == "http" and redirect.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            not self.settings.google_oauth_client_id
            or not self.settings.google_oauth_client_secret
            or redirect.path != "/auth/google/callback"
            or redirect.query
            or redirect.fragment
            or redirect.username
            or redirect.password
            or not (redirect.scheme == "https" or local_http)
            or not redirect.hostname
        ):
            raise GoogleOAuthDisabled("Google OAuth is not configured")
        return email

    @property
    def ready(self) -> bool:
        try:
            self.require_config()
            return True
        except GoogleOAuthDisabled:
            return False

    def _flow(self, *, state: str | None = None, verifier: str | None = None) -> Flow:
        web = {
            "client_id": self.settings.google_oauth_client_id,
            "client_secret": self.settings.google_oauth_client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [self.settings.google_oauth_redirect_uri],
        }
        return Flow.from_client_config(
            {"web": web},
            scopes=SCOPES,
            redirect_uri=self.settings.google_oauth_redirect_uri,
            state=state,
            code_verifier=verifier,
            autogenerate_code_verifier=verifier is None,
        )

    def begin(self) -> tuple[str, dict[str, Any]]:
        self.require_config()
        nonce = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        flow = self._flow()
        url, returned_state = flow.authorization_url(
            access_type="online", prompt="select_account", nonce=nonce, state=state
        )
        if not flow.code_verifier or returned_state != state:
            raise GoogleOAuthDisabled("Google OAuth initialization failed")
        return url, {
            "state": state,
            "nonce": nonce,
            "verifier": flow.code_verifier,
            "issued_at": int(time.time()),
        }

    def finish(self, code: str, pending: dict[str, Any]) -> tuple[str, str]:
        self.require_config()
        flow = self._flow(state=pending["state"], verifier=pending["verifier"])
        tokens = flow.fetch_token(code=code, timeout=10)
        token = tokens.get("id_token")
        if not isinstance(token, str) or not token:
            raise GoogleIdentityRejected("Google ID token is missing")
        try:
            claims = verify_oauth2_token(
                token,
                partial(GoogleRequest(), timeout=10),
                self.settings.google_oauth_client_id,
            )
        except (GoogleAuthError, ValueError) as exc:
            raise GoogleIdentityRejected("Google ID token could not be verified") from exc
        return validate_identity(dict(claims), self.settings, pending["nonce"])


def valid_pending(pending: Any, received_state: str | None) -> bool:
    if not isinstance(pending, dict) or not isinstance(received_state, str):
        return False
    state = pending.get("state")
    nonce = pending.get("nonce")
    verifier = pending.get("verifier")
    issued = pending.get("issued_at")
    if (
        not isinstance(state, str)
        or not state
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(verifier, str)
        or not verifier
    ):
        return False
    if not isinstance(issued, int) or not 0 <= time.time() - issued <= PENDING_LIFETIME_SECONDS:
        return False
    return hmac.compare_digest(state, received_state)
