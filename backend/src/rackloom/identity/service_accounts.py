"""F35 : identités de service limitées à un projet, authentifiées par jeton Bearer.

Une identité de service ne reçoit que le rôle `operator` sur son projet. Elle ne peut
ni approuver, ni recevoir d'autres rôles. Le jeton n'apparaît qu'à la création.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import audit
from ..db import Conn
from ..errors import ApiError
from ..ids import uuid7
from ..security import new_token, token_hash
from .authz import Principal, load_bindings
from .service import create_user


def mint(conn: Conn, *, email: str, display_name: str, project_id: UUID, label: str,
         is_demo: bool = False, actor_id: UUID | None = None) -> dict[str, Any]:
    """Crée le compte service + jeton. Renvoie le jeton en clair une seule fois."""
    existing = conn.execute("SELECT id FROM users WHERE email = %s", (email.lower(),)).fetchone()
    if existing:
        raise ApiError(409, "already_exists", "identité de service déjà présente")
    # Mot de passe inutilisable : connexion password refusée pour kind=service.
    user_id = create_user(conn, email, display_name, new_token(48), is_demo=is_demo)
    conn.execute("UPDATE users SET kind = 'service' WHERE id = %s", (user_id,))
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, project_id, granted_by) VALUES (%s, %s, 'operator', %s, %s)",
        (uuid7(), user_id, project_id, actor_id),
    )
    token = new_token(32)
    token_id = uuid7()
    conn.execute(
        "INSERT INTO service_tokens (id, user_id, token_hash, project_id, label) VALUES (%s, %s, %s, %s, %s)",
        (token_id, user_id, token_hash(token), project_id, label),
    )
    audit.record(
        conn,
        actor_type="user" if actor_id else "system",
        actor_id=str(actor_id) if actor_id else "bootstrap",
        action="identity.service_token.create",
        outcome="ok",
        project_id=project_id,
        targets={"user_id": str(user_id), "token_id": str(token_id), "label": label},
    )
    return {"user_id": user_id, "token_id": token_id, "token": token, "project_id": project_id}


def issue_token(conn: Conn, *, user_id: UUID, project_id: UUID, label: str) -> str:
    """Émet un nouveau jeton pour une identité de service existante (tests / rotation locale)."""
    user = conn.execute("SELECT kind, status FROM users WHERE id = %s", (user_id,)).fetchone()
    if user is None or user["kind"] != "service" or user["status"] != "active":
        raise ApiError(404, "not_found", "identité de service introuvable")
    token = new_token(32)
    conn.execute(
        "INSERT INTO service_tokens (id, user_id, token_hash, project_id, label) VALUES (%s, %s, %s, %s, %s)",
        (uuid7(), user_id, token_hash(token), project_id, label),
    )
    return token


def resolve_token(conn: Conn, token: str) -> Principal | None:
    row = conn.execute(
        """
        SELECT t.id AS token_id, t.project_id, t.revoked_at, u.id AS user_id, u.email, u.display_name,
               u.status, u.kind, u.is_demo, u.privilege_epoch
        FROM service_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = %s
        """,
        (token_hash(token),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or row["status"] != "active" or row["kind"] != "service":
        return None
    from ..config import get_settings

    if get_settings().profile == "production" and row["is_demo"]:
        return None
    conn.execute("UPDATE service_tokens SET last_used_at = now() WHERE id = %s", (row["token_id"],))
    return Principal(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        session_id=row["token_id"],  # pas de session cookie ; id du jeton pour l'audit
        auth_method="service_token",
        auth_time=datetime.now(UTC),
        bindings=load_bindings(conn, row["user_id"]),
        csrf_token="",  # Bearer : pas de CSRF
        kind="service",
        service_project_id=row["project_id"],
    )


def refuse_approval(principal: Principal) -> None:
    if principal.kind == "service" or principal.auth_method == "service_token":
        raise ApiError(
            403,
            "service_identity_forbidden",
            "une identité de service ne peut ni approuver ni contourner l'approbation",
        )
