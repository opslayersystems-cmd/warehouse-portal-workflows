"""Local operator credentials and session/role checks for the web app."""

import base64
import binascii
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from warehouse_portal.db import get_db
from warehouse_portal.models import Operator

ROLES = {"viewer": 0, "operator": 1, "reviewer": 2, "admin": 3}
HASH_ITERATIONS = 600_000


def normalized_username(value: str) -> str:
    username = value.strip().casefold()
    if not 2 <= len(username) <= 255 or not all(c.isalnum() or c in "._-@" for c in username):
        raise ValueError("Username must be 2–255 letters, digits, dots, underscores, hyphens or @")
    return username


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    salt_text = base64.urlsafe_b64encode(salt).decode()
    digest_text = base64.urlsafe_b64encode(digest).decode()
    return f"pbkdf2_sha256${HASH_ITERATIONS}${salt_text}${digest_text}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(iterations) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.urlsafe_b64decode(salt), int(iterations)
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest))
    except (ValueError, TypeError, binascii.Error):
        return False


def create_operator(db: Session, username: str, password: str, role: str) -> Operator:
    name = normalized_username(username)
    if role not in ROLES:
        raise ValueError("Invalid role")
    if db.scalar(select(Operator).where(Operator.username == name)):
        raise ValueError("Operator already exists")
    operator = Operator(username=name, password_hash=hash_password(password), role=role)
    db.add(operator)
    db.commit()
    return operator


def update_operator(
    db: Session,
    username: str,
    *,
    role: str | None = None,
    active: bool | None = None,
    password: str | None = None,
) -> Operator:
    if role is None and active is None and password is None:
        raise ValueError("Choose a role, active state, or password reset")
    if role is not None and role not in ROLES:
        raise ValueError("Invalid role")
    operator = db.scalar(select(Operator).where(Operator.username == normalized_username(username)))
    if operator is None:
        raise ValueError("Operator not found")
    password_hash = hash_password(password) if password is not None else None
    if role is not None:
        operator.role = role
    if active is not None:
        operator.active = active
    if password_hash is not None:
        operator.password_hash = password_hash
    operator.session_version += 1
    db.commit()
    return operator


def authenticate(db: Session, username: str, password: str) -> Operator | None:
    if len(password) > 1024:
        return None
    try:
        name = normalized_username(username)
    except ValueError:
        return None
    operator = db.scalar(select(Operator).where(Operator.username == name))
    if operator and operator.active and verify_password(password, operator.password_hash):
        return operator
    return None


def start_session(request: Request, operator: Operator) -> str:
    request.session.clear()
    token = secrets.token_urlsafe(32)
    request.session.update(
        {"operator_id": operator.id, "version": operator.session_version, "csrf": token}
    )
    return token


def current_operator(request: Request, db: Session = Depends(get_db)) -> Operator:
    operator_id = request.session.get("operator_id")
    version = request.session.get("version")
    csrf = request.session.get("csrf")
    operator = db.get(Operator, operator_id) if isinstance(operator_id, str) else None
    if not operator or not operator.active or operator.session_version != version or not csrf:
        raise HTTPException(401, "Sign in required")
    request.state.operator = operator
    return operator


def require_mutation(role: str):
    async def check(
        request: Request,
        operator: Annotated[Operator, Depends(current_operator)],
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> Operator:
        if ROLES.get(operator.role, -1) < ROLES[role]:
            raise HTTPException(403, "Insufficient role")
        expected = request.session.get("csrf", "")
        if request.url.path.startswith("/api/"):
            supplied = x_csrf_token or ""
        else:
            # Form parsing is cached by Starlette and remains available to the endpoint.
            supplied = str((await request.form()).get("csrf_token") or "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(403, "Invalid CSRF token")
        return operator

    return check
