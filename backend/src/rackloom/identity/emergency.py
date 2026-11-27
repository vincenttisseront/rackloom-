"""Accès d'urgence local lorsque le SSO est indisponible (F14).

Activation depuis la console (`rackloom emergency activate`), durée limitée, allowlist
d'utilisateurs locaux, audit complet. Après fermeture ou expiration, la connexion
d'urgence n'est plus utilisable.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from .. import audit, clock
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..ids import uuid7
from ..redact import redact

SSO_KEY = "sso.available"
MAX_TTL = timedelta(hours=8)


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def sso_available(conn: Conn) -> bool:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = %s", (SSO_KEY,)).fetchone()
    if row is None:
        return True
    val = row["value"]
    if isinstance(val, dict):
        return bool(val.get("available", True))
    return True


def set_sso_available(conn: Conn, available: bool, *, actor: str = "console") -> None:
    conn.execute(
        """
        INSERT INTO platform_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (SSO_KEY, Jsonb({"available": available, "updated_by": actor})),
    )
    audit.record(
        conn,
        actor_type="system",
        actor_id=actor,
        action="sso.availability",
        outcome="ok",
        policy_decision={"available": available},
    )


def active(conn: Conn, *, at=None) -> dict[str, Any] | None:
    instant = at or clock.now()
    row = conn.execute(
        """
        SELECT * FROM emergency_interventions
        WHERE closed_at IS NULL AND expires_at > %s
        ORDER BY activated_at DESC LIMIT 1
        """,
        (instant,),
    ).fetchone()
    return dict(row) if row else None


def allowlist_user_ids(conn: Conn, intervention_id: UUID) -> set[UUID]:
    rows = conn.execute(
        "SELECT user_id FROM emergency_allowlist WHERE intervention_id = %s",
        (intervention_id,),
    ).fetchall()
    return {r["user_id"] for r in rows}


def login_permitted(conn: Conn, user_id: UUID, *, at=None) -> tuple[bool, str]:
    """Décide si une connexion locale est acceptée.

    - SSO disponible : connexion locale autorisée (comptes de secours / démo).
    - SSO indisponible : uniquement pendant une intervention active et pour l'allowlist.
    """
    if sso_available(conn):
        return True, "sso_available"
    intervention = active(conn, at=at)
    if intervention is None:
        return False, "emergency_closed"
    if user_id not in allowlist_user_ids(conn, intervention["id"]):
        return False, "emergency_not_listed"
    return True, "emergency_active"


def activate(
    conn: Conn,
    *,
    reason: Any,
    minutes: int,
    user_ids: list[UUID],
    actor: str = "console",
    simulate_idp_down: bool = True,
) -> dict[str, Any]:
    text = _reason(reason)
    if not 5 <= minutes <= int(MAX_TTL.total_seconds() // 60):
        raise ApiError(422, "invalid_duration", "durée entre 5 minutes et 8 heures")
    if not user_ids:
        raise ApiError(422, "allowlist_required", "au moins un utilisateur local dans l'allowlist")
    if active(conn) is not None:
        raise ApiError(409, "already_active", "une intervention d'urgence est déjà ouverte")
    if simulate_idp_down:
        set_sso_available(conn, False, actor=actor)
    instant = clock.now()
    expires = instant + timedelta(minutes=minutes)
    intervention_id = uuid7()
    conn.execute(
        """
        INSERT INTO emergency_interventions
            (id, reason, activated_by, expires_at, sso_was_unavailable)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (intervention_id, text, actor, expires, True),
    )
    for uid in user_ids:
        user = conn.execute(
            "SELECT id FROM users WHERE id = %s AND status = 'active' AND kind = 'human'",
            (uid,),
        ).fetchone()
        if user is None:
            raise ApiError(404, "not_found", f"utilisateur {uid} introuvable")
        conn.execute(
            "INSERT INTO emergency_allowlist (id, intervention_id, user_id) VALUES (%s, %s, %s)",
            (uuid7(), intervention_id, uid),
        )
    audit.record(
        conn,
        actor_type="system",
        actor_id=actor,
        action="emergency.activate",
        outcome="ok",
        targets={"intervention": str(intervention_id), "users": [str(u) for u in user_ids]},
        policy_decision={"expires_at": expires.isoformat(), "reason": text, "minutes": minutes},
    )
    return {
        "id": str(intervention_id),
        "expires_at": expires.isoformat(),
        "users": [str(u) for u in user_ids],
        "sso_available": sso_available(conn),
    }


def close(
    conn: Conn,
    *,
    reason: Any,
    actor: str = "console",
    restore_sso: bool = True,
) -> dict[str, Any]:
    text = _reason(reason, "close_reason")
    row = conn.execute("SELECT * FROM emergency_interventions WHERE closed_at IS NULL FOR UPDATE").fetchone()
    if row is None:
        raise ApiError(409, "not_active", "aucune intervention d'urgence ouverte")
    conn.execute(
        """
        UPDATE emergency_interventions
        SET closed_at = now(), closed_by = %s, close_reason = %s
        WHERE id = %s
        """,
        (actor, text, row["id"]),
    )
    # Révoque les sessions des utilisateurs de l'allowlist.
    from . import service as identity

    for uid in allowlist_user_ids(conn, row["id"]):
        identity.bump_privilege_epoch(conn, uid, "emergency_closed")
    if restore_sso:
        set_sso_available(conn, True, actor=actor)
    audit.record(
        conn,
        actor_type="system",
        actor_id=actor,
        action="emergency.close",
        outcome="ok",
        targets={"intervention": str(row["id"])},
        policy_decision={"reason": text},
    )
    return {"id": str(row["id"]), "closed": True, "sso_available": sso_available(conn)}


def status(conn: Conn) -> dict[str, Any]:
    intervention = active(conn)
    out: dict[str, Any] = {
        "sso_available": sso_available(conn),
        "intervention": None,
    }
    if intervention:
        users = [
            {"id": str(r["user_id"]), "email": r["email"], "display_name": r["display_name"]}
            for r in conn.execute(
                """
                SELECT u.id AS user_id, u.email, u.display_name
                FROM emergency_allowlist a JOIN users u ON u.id = a.user_id
                WHERE a.intervention_id = %s
                """,
                (intervention["id"],),
            ).fetchall()
        ]
        out["intervention"] = {
            "id": str(intervention["id"]),
            "reason": intervention["reason"],
            "activated_at": intervention["activated_at"].isoformat(),
            "activated_by": intervention["activated_by"],
            "expires_at": intervention["expires_at"].isoformat(),
            "users": users,
        }
    return out
