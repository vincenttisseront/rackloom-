"""F41 : arrivées et départs via SCIM (annuaire de test / mock explicite).

Sous-ensemble SCIM 2.0 Users. Aucun IdP réel : les jetons et les utilisateurs de test
sont locaux. À la désactivation : sessions révoquées (`privilege_epoch`), jobs en file
liés au demandeur passés en `blocked` ; les jobs déjà envoyés restent visibles.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..ids import uuid7
from ..security import new_token, token_hash
from . import service as identity
from .authz import Principal

SCIM_CONTENT = "application/scim+json"


def mint_token(conn: Conn, principal: Principal, *, label: str) -> dict[str, Any]:
    """Jeton Bearer SCIM (affiché une seule fois). Réservé aux administrateurs de plateforme."""
    if not principal.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    text = (label or "").strip()
    if not 1 <= len(text) <= 120:
        raise ApiError(422, "invalid_label", "libellé obligatoire (1 à 120 caractères)", field="/label")
    token = new_token(32)
    token_id = uuid7()
    conn.execute(
        "INSERT INTO scim_tokens (id, token_hash, label, created_by) VALUES (%s, %s, %s, %s)",
        (token_id, token_hash(token), text, principal.user_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="scim.token.create",
        outcome="ok",
        targets={"token_id": str(token_id), "label": text},
    )
    return {"id": str(token_id), "label": text, "token": token}


def resolve_token(conn: Conn, token: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, label, revoked_at FROM scim_tokens WHERE token_hash = %s",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    conn.execute("UPDATE scim_tokens SET last_used_at = now() WHERE id = %s", (row["id"],))
    return dict(row)


def _view(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": str(user["id"]),
        "externalId": user.get("external_id"),
        "userName": user["email"],
        "displayName": user["display_name"],
        "active": user["status"] == "active",
        "meta": {"resourceType": "User"},
    }


def get_user(conn: Conn, user_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id = %s AND kind = 'human'", (user_id,)).fetchone()
    if row is None:
        raise not_found("utilisateur SCIM")
    return _view(row)


def _password_placeholder() -> str:
    # Compte SCIM : pas de mot de passe utilisable tant qu'OIDC n'est pas là ; login local refusé
    # si le hash n'est jamais connu. On pose un secret aléatoire non communiqué.
    return new_token(48)


def create_user(conn: Conn, body: dict[str, Any]) -> dict[str, Any]:
    email = str(body.get("userName") or "").strip().lower()
    display = str(body.get("displayName") or email).strip()
    external = str(body.get("externalId") or "").strip() or None
    active = body.get("active", True)
    if not email or "@" not in email or len(email) > 200:
        raise ApiError(400, "invalidValue", "userName (email) invalide")
    if external is not None and not 1 <= len(external) <= 200:
        raise ApiError(400, "invalidValue", "externalId invalide")
    if conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
        raise ApiError(409, "uniqueness", "userName déjà présent")
    if external and conn.execute("SELECT 1 FROM users WHERE external_id = %s", (external,)).fetchone():
        raise ApiError(409, "uniqueness", "externalId déjà présent")
    user_id = identity.create_user(conn, email, display or email, _password_placeholder(), is_demo=False)
    if external:
        conn.execute("UPDATE users SET external_id = %s WHERE id = %s", (external, user_id))
    if active is False:
        _deactivate(conn, user_id, actor="scim", detail={"via": "create"})
    else:
        conn.execute(
            "INSERT INTO scim_sync_events (id, external_id, user_id, operation, active, detail) "
            "VALUES (%s, %s, %s, 'create', true, %s)",
            (uuid7(), external or str(user_id), user_id, Jsonb({})),
        )
    audit.record(
        conn,
        actor_type="scim",
        actor_id="directory",
        action="scim.user.create",
        outcome="ok",
        targets={"user_id": str(user_id), "external_id": external, "active": bool(active)},
    )
    row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row is not None
    return _view(row)


def _block_queued_jobs(conn: Conn, user_id: UUID) -> int:
    """Jobs encore en file pour des changements dont le demandeur est cet utilisateur → blocked."""
    rows = conn.execute(
        """
        UPDATE jobs j SET state = 'blocked', blocked_reason = 'identity_disabled', finished_at = now(),
                          lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
        FROM changes c
        WHERE j.change_id = c.id AND c.created_by = %s AND j.state = 'queued'
          AND j.dispatched_at IS NULL
        RETURNING j.id, j.change_id, c.project_id
        """,
        (user_id,),
    ).fetchall()
    for row in rows:
        from .. import events

        events.emit(
            conn,
            type="job.blocked",
            source="job",
            project_id=row["project_id"],
            correlation_id=row["change_id"],
            job_id=row["id"],
            data={"code": "identity_disabled", "message": "identité désactivée via SCIM"},
            severity="error",
            dedupe_key=f"job.blocked.scim:{row['id']}",
        )
    return len(rows)


def _deactivate(conn: Conn, user_id: UUID, *, actor: str, detail: dict[str, Any]) -> int:
    row = conn.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
    if row is None:
        raise not_found("utilisateur SCIM")
    conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (user_id,))
    identity.bump_privilege_epoch(conn, user_id, "scim_deactivate")
    blocked = _block_queued_jobs(conn, user_id)
    conn.execute(
        "INSERT INTO scim_sync_events (id, external_id, user_id, operation, active, actor, detail) "
        "VALUES (%s, %s, %s, 'deactivate', false, %s, %s)",
        (
            uuid7(),
            row.get("external_id") or str(user_id),
            user_id,
            actor,
            Jsonb({**detail, "queued_jobs_blocked": blocked}),
        ),
    )
    audit.record(
        conn,
        actor_type="scim",
        actor_id=actor,
        action="scim.user.deactivate",
        outcome="ok",
        targets={"user_id": str(user_id), "queued_jobs_blocked": blocked},
    )
    return blocked


def patch_user(conn: Conn, user_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id = %s AND kind = 'human' FOR UPDATE", (user_id,)).fetchone()
    if row is None:
        raise not_found("utilisateur SCIM")
    ops = body.get("Operations") or body.get("operations") or []
    if not isinstance(ops, list):
        raise ApiError(400, "invalidSyntax", "Operations attendu")
    active: bool | None = None
    display: str | None = None
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "").lower()
        value = op.get("value")
        if path == "active" or (not path and isinstance(value, dict) and "active" in value):
            active = bool(value if path == "active" else value["active"])
        if path == "displayname":
            display = str(value).strip()
        if isinstance(value, dict):
            if "active" in value:
                active = bool(value["active"])
            if "displayName" in value:
                display = str(value["displayName"]).strip()
    if display:
        conn.execute("UPDATE users SET display_name = %s WHERE id = %s", (display, user_id))
    if active is False and row["status"] == "active":
        _deactivate(conn, user_id, actor="scim", detail={"via": "patch"})
    elif active is True and row["status"] == "disabled":
        conn.execute("UPDATE users SET status = 'active' WHERE id = %s", (user_id,))
        identity.bump_privilege_epoch(conn, user_id, "scim_reactivate")
        conn.execute(
            "INSERT INTO scim_sync_events (id, external_id, user_id, operation, active, detail) "
            "VALUES (%s, %s, %s, 'reactivate', true, %s)",
            (uuid7(), row.get("external_id") or str(user_id), user_id, Jsonb({"via": "patch"})),
        )
        audit.record(
            conn,
            actor_type="scim",
            actor_id="directory",
            action="scim.user.reactivate",
            outcome="ok",
            targets={"user_id": str(user_id)},
        )
    elif active is None and display:
        conn.execute(
            "INSERT INTO scim_sync_events (id, external_id, user_id, operation, active, detail) "
            "VALUES (%s, %s, %s, 'update', %s, %s)",
            (
                uuid7(),
                row.get("external_id") or str(user_id),
                user_id,
                row["status"] == "active",
                Jsonb({"via": "patch"}),
            ),
        )
    refreshed = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    assert refreshed is not None
    return _view(refreshed)


def apply_fixture(conn: Conn, users: list[dict[str, Any]], *, actor: str = "scim-fixture") -> dict[str, Any]:
    """Synchronise depuis une liste fictive explicite (pas d'IdP réel)."""
    created = updated = deactivated = 0
    for entry in users:
        external = str(entry.get("externalId") or entry.get("external_id") or "").strip()
        email = str(entry.get("userName") or entry.get("email") or "").strip().lower()
        active = bool(entry.get("active", True))
        display = str(entry.get("displayName") or entry.get("display_name") or email).strip()
        if not external or not email:
            continue
        existing = conn.execute(
            "SELECT * FROM users WHERE external_id = %s OR email = %s",
            (external, email),
        ).fetchone()
        if existing is None:
            create_user(
                conn,
                {"userName": email, "displayName": display, "externalId": external, "active": active},
            )
            created += 1
            continue
        if not active and existing["status"] == "active":
            _deactivate(conn, existing["id"], actor=actor, detail={"via": "fixture"})
            deactivated += 1
        else:
            conn.execute(
                "UPDATE users SET display_name = %s, external_id = COALESCE(external_id, %s) WHERE id = %s",
                (display, external, existing["id"]),
            )
            updated += 1
    return {"created": created, "updated": updated, "deactivated": deactivated, "simulated": True}
