"""Comptes locaux, sessions serveur révocables, CSRF et jeton d'amorçage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg

from .. import audit
from ..config import get_settings
from ..errors import ApiError
from ..ids import uuid7
from ..security import (
    DUMMY_PASSWORD_HASH,
    csrf_token_for,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)
from .authz import Principal, load_bindings

SESSION_COOKIE = "__Host-rl_session"
DEV_SESSION_COOKIE = "rl_session"


def cookie_name() -> str:
    return SESSION_COOKIE if get_settings().cookie_secure else DEV_SESSION_COOKIE


@dataclass
class NewSession:
    token: str
    csrf_token: str
    session_id: UUID
    user_id: UUID


def now() -> datetime:
    return datetime.now(UTC)


def create_user(conn: psycopg.Connection, email: str, display_name: str, password: str,
                is_demo: bool = False) -> UUID:
    settings = get_settings()
    if is_demo and settings.profile == "production":
        raise ApiError(403, "demo_identity_refused", "identités de démonstration refusées en production")
    if email.endswith(".invalid") and settings.profile == "production":
        raise ApiError(403, "demo_identity_refused", "domaine .invalid refusé en production")
    user_id = uuid7()
    conn.execute(
        "INSERT INTO users (id, email, display_name, password_hash, is_demo) VALUES (%s, %s, %s, %s, %s)",
        (user_id, email.lower(), display_name, hash_password(password), is_demo),
    )
    return user_id


def _rate_limited(conn: psycopg.Connection, email: str) -> bool:
    settings = get_settings()
    row = conn.execute(
        "SELECT count(*) AS n FROM login_attempts WHERE email = %s AND NOT succeeded "
        "AND at > now() - make_interval(secs => %s)",
        (email, settings.login_window_seconds),
    ).fetchone()
    return row["n"] >= settings.login_max_failures


def login(conn: psycopg.Connection, email: str, password: str, client: str,
          previous_token: str | None = None) -> NewSession | ApiError:
    """Renvoie l'erreur au lieu de la lever : l'échec doit être enregistré (limitation de débit)."""
    settings = get_settings()
    email = email.strip().lower()
    if _rate_limited(conn, email):
        audit.record(conn, actor_type="anonymous", actor_id=None, action="auth.login",
                     outcome="rate_limited", targets={"email": email})
        return ApiError(429, "rate_limited", "trop de tentatives, réessayez plus tard")
    user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    ok = verify_password(user["password_hash"] if user else DUMMY_PASSWORD_HASH, password)
    if user and settings.profile == "production" and user["is_demo"]:
        ok = False
    if not user or not ok or user["status"] != "active":
        conn.execute("INSERT INTO login_attempts (email, client, succeeded) VALUES (%s, %s, false)",
                     (email, client))
        audit.record(conn, actor_type="anonymous", actor_id=None, action="auth.login", outcome="denied",
                     targets={"email": email})
        return ApiError(401, "invalid_credentials", "identifiants invalides")
    conn.execute("INSERT INTO login_attempts (email, client, succeeded) VALUES (%s, %s, true)", (email, client))
    # Rotation : toute session présentée au moment de la connexion est révoquée.
    if previous_token:
        conn.execute(
            "UPDATE sessions SET revoked_at = now(), revoked_reason = 'rotation' "
            "WHERE id_hash = %s AND revoked_at IS NULL",
            (token_hash(previous_token),),
        )
    session = _create_session(conn, user["id"], user["privilege_epoch"], "password")
    audit.record(conn, actor_type="user", actor_id=str(user["id"]), action="auth.login", outcome="ok",
                 targets={"session_id": str(session.session_id)})
    return session


def _create_session(conn: psycopg.Connection, user_id: UUID, epoch: int, method: str) -> NewSession:
    settings = get_settings()
    token = new_token()
    csrf = csrf_token_for(token)
    session_id = uuid7()
    t = now()
    conn.execute(
        """
        INSERT INTO sessions (id, id_hash, user_id, csrf_hash, auth_method, auth_time, idle_expires_at,
                              absolute_expires_at, privilege_epoch)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (session_id, token_hash(token), user_id, token_hash(csrf), method, t,
         t + timedelta(minutes=settings.session_idle_minutes),
         t + timedelta(minutes=settings.session_absolute_minutes), epoch),
    )
    return NewSession(token=token, csrf_token=csrf, session_id=session_id, user_id=user_id)


def logout(conn: psycopg.Connection, principal: Principal) -> None:
    conn.execute("UPDATE sessions SET revoked_at = now(), revoked_reason = 'logout' WHERE id = %s",
                 (principal.session_id,))
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="auth.logout", outcome="ok")


def resolve_session(conn: psycopg.Connection, token: str, touch: bool = True) -> Principal | None:
    row = conn.execute(
        """
        SELECT s.id, s.user_id, s.auth_method, s.auth_time, s.idle_expires_at, s.absolute_expires_at,
               s.privilege_epoch AS s_epoch, s.revoked_at, s.last_seen_at,
               u.email, u.display_name, u.status, u.privilege_epoch AS u_epoch, u.is_demo
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.id_hash = %s
        """,
        (token_hash(token),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        return None
    t = now()
    if row["idle_expires_at"] <= t or row["absolute_expires_at"] <= t:
        return None
    if row["s_epoch"] != row["u_epoch"]:
        return None
    if get_settings().profile == "production" and row["is_demo"]:
        return None
    if touch and (t - row["last_seen_at"]).total_seconds() > 30:
        conn.execute(
            "UPDATE sessions SET last_seen_at = %s, idle_expires_at = LEAST(%s, absolute_expires_at) "
            "WHERE id = %s",
            (t, t + timedelta(minutes=get_settings().session_idle_minutes), row["id"]),
        )
    return Principal(
        user_id=row["user_id"], email=row["email"], display_name=row["display_name"], session_id=row["id"],
        auth_method=row["auth_method"], auth_time=row["auth_time"],
        bindings=load_bindings(conn, row["user_id"]), csrf_token=csrf_token_for(token),
    )


def session_is_active(conn: psycopg.Connection, session_id: UUID) -> bool:
    row = conn.execute(
        """
        SELECT s.revoked_at, s.idle_expires_at, s.absolute_expires_at, s.privilege_epoch AS s_epoch,
               u.privilege_epoch AS u_epoch, u.status
        FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = %s
        """,
        (session_id,),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        return False
    t = now()
    return row["absolute_expires_at"] > t and row["s_epoch"] == row["u_epoch"]


def bump_privilege_epoch(conn: psycopg.Connection, user_id: UUID, reason: str) -> None:
    """Changement de privilège : toutes les sessions de l'utilisateur deviennent invalides."""
    conn.execute("UPDATE users SET privilege_epoch = privilege_epoch + 1 WHERE id = %s", (user_id,))
    conn.execute(
        "UPDATE sessions SET revoked_at = now(), revoked_reason = %s WHERE user_id = %s AND revoked_at IS NULL",
        (reason, user_id),
    )


def create_bootstrap_token(conn: psycopg.Connection, minutes: int = 30) -> str:
    token = new_token()
    conn.execute(
        "INSERT INTO bootstrap_tokens (id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (uuid7(), token_hash(token), now() + timedelta(minutes=minutes)),
    )
    audit.record(conn, actor_type="system", actor_id="console", action="auth.bootstrap_token.create",
                 outcome="ok")
    return token


def consume_bootstrap(conn: psycopg.Connection, token: str, email: str, display_name: str,
                      password: str) -> UUID:
    row = conn.execute(
        "SELECT id FROM bootstrap_tokens WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() "
        "FOR UPDATE",
        (token_hash(token),),
    ).fetchone()
    if row is None:
        audit.record(conn, actor_type="anonymous", actor_id=None, action="auth.bootstrap", outcome="denied")
        raise ApiError(401, "invalid_bootstrap_token", "jeton d'amorçage invalide, expiré ou déjà utilisé",
                       commit=True)
    if len(password) < 12:
        raise ApiError(422, "weak_password", "mot de passe trop court (12 caractères minimum)",
                       field="/password")
    conn.execute("UPDATE bootstrap_tokens SET used_at = now() WHERE id = %s", (row["id"],))
    user_id = create_user(conn, email, display_name, password)
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role) VALUES (%s, %s, 'platform_admin')", (uuid7(), user_id)
    )
    audit.record(conn, actor_type="user", actor_id=str(user_id), action="auth.bootstrap", outcome="ok",
                 targets={"email": email})
    return user_id
