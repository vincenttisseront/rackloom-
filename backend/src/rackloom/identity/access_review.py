"""F42 : revue périodique des accès.

Campagnes préparées automatiquement : comptes sans propriétaire de projet, droits non
justifiés (binding sans référent / hors périmètre attendu), exceptions et grants temporaires
expirés. Le responsable de projet ne voit que son périmètre ; confirmation ou révocation
auditées.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, clock
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..ids import uuid7
from ..redact import redact
from . import service as identity
from .authz import Principal


def _may_manage_project(principal: Principal, project_id: UUID) -> bool:
    return principal.is_platform_admin or principal.has_role("project_admin", project_id)


def prepare(conn: Conn, principal: Principal, *, title: str, note: str = "") -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise forbidden("seule la plateforme prépare une campagne de revue")
    text = redact((title or "").strip())
    if not 3 <= len(text) <= 200:
        raise ApiError(422, "invalid_title", "titre obligatoire (3 à 200 caractères)", field="/title")
    note_text = redact((note or "").strip())
    if len(note_text) > 500:
        raise ApiError(422, "invalid_note", "note trop longue", field="/note")
    campaign_id = uuid7()
    conn.execute(
        "INSERT INTO access_review_campaigns (id, title, prepared_by, note) VALUES (%s, %s, %s, %s)",
        (campaign_id, text, principal.user_id, note_text),
    )
    items = 0
    items += _collect_orphan_accounts(conn, campaign_id)
    items += _collect_unjustified_bindings(conn, campaign_id)
    items += _collect_expired_exceptions(conn, campaign_id)
    items += _collect_expired_grants(conn, campaign_id)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access_review.prepare",
        outcome="ok",
        targets={"campaign_id": str(campaign_id), "items": items},
    )
    return get_campaign(conn, principal, campaign_id)


def _safe_insert(
    conn: Conn,
    *,
    campaign_id: UUID,
    project_id: UUID,
    kind: str,
    summary: str,
    subject_user_id: UUID | None = None,
    binding_id: UUID | None = None,
    exception_id: UUID | None = None,
    temporary_grant_id: UUID | None = None,
) -> int:
    exists = conn.execute(
        """
        SELECT 1 FROM access_review_items
        WHERE campaign_id = %s AND kind = %s
          AND (binding_id IS NOT DISTINCT FROM %s)
          AND (exception_id IS NOT DISTINCT FROM %s)
          AND (temporary_grant_id IS NOT DISTINCT FROM %s)
          AND (subject_user_id IS NOT DISTINCT FROM %s)
          AND project_id = %s
        """,
        (campaign_id, kind, binding_id, exception_id, temporary_grant_id, subject_user_id, project_id),
    ).fetchone()
    if exists:
        return 0
    conn.execute(
        """
        INSERT INTO access_review_items
            (id, campaign_id, project_id, kind, subject_user_id, binding_id, exception_id,
             temporary_grant_id, summary)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid7(),
            campaign_id,
            project_id,
            kind,
            subject_user_id,
            binding_id,
            exception_id,
            temporary_grant_id,
            summary[:500],
        ),
    )
    return 1


def _collect_orphan_accounts(conn: Conn, campaign_id: UUID) -> int:
    """Projets sans référent, ou référent désactivé."""
    n = 0
    rows = conn.execute(
        """
        SELECT p.id AS project_id, p.slug, p.referent_user_id, u.status
        FROM projects p
        LEFT JOIN users u ON u.id = p.referent_user_id
        """
    ).fetchall()
    for row in rows:
        if row["referent_user_id"] is None:
            n += _safe_insert(
                conn,
                campaign_id=campaign_id,
                project_id=row["project_id"],
                kind="orphan_account",
                summary=f"projet « {row['slug']} » sans propriétaire (référent)",
            )
        elif row["status"] != "active":
            n += _safe_insert(
                conn,
                campaign_id=campaign_id,
                project_id=row["project_id"],
                kind="orphan_account",
                subject_user_id=row["referent_user_id"],
                summary=f"référent du projet « {row['slug']} » désactivé",
            )
    return n


def _collect_unjustified_bindings(conn: Conn, campaign_id: UUID) -> int:
    """Bindings actifs sur un projet sans que l'utilisateur soit référent ni justifié autrement.

    Heuristique locale : binding `platform_admin` hors plateforme, ou utilisateur désactivé
    avec un binding encore actif.
    """
    n = 0
    rows = conn.execute(
        """
        SELECT b.id AS binding_id, b.user_id, b.role, b.project_id, u.status, u.email, p.slug
        FROM role_bindings b
        JOIN users u ON u.id = b.user_id
        JOIN projects p ON p.id = b.project_id
        WHERE b.revoked_at IS NULL AND b.project_id IS NOT NULL
          AND (u.status <> 'active' OR b.role = 'platform_admin')
        """
    ).fetchall()
    for row in rows:
        reason = (
            f"compte désactivé « {row['email']} » conserve le rôle {row['role']} sur « {row['slug']} »"
            if row["status"] != "active"
            else f"rôle platform_admin lié au projet « {row['slug']} » (non justifié)"
        )
        n += _safe_insert(
            conn,
            campaign_id=campaign_id,
            project_id=row["project_id"],
            kind="unjustified_binding",
            subject_user_id=row["user_id"],
            binding_id=row["binding_id"],
            summary=reason,
        )
    return n


def _collect_expired_exceptions(conn: Conn, campaign_id: UUID) -> int:
    now = clock.now()
    n = 0
    rows = conn.execute(
        """
        SELECT id, project_id, rule_id, owner_user_id, expires_at
        FROM policy_exceptions
        WHERE revoked_at IS NULL AND expires_at <= %s
        """,
        (now,),
    ).fetchall()
    for row in rows:
        n += _safe_insert(
            conn,
            campaign_id=campaign_id,
            project_id=row["project_id"],
            kind="expired_exception",
            subject_user_id=row["owner_user_id"],
            exception_id=row["id"],
            summary=f"exception {row['rule_id']} expirée le {row['expires_at'].isoformat()}",
        )
    return n


def _collect_expired_grants(conn: Conn, campaign_id: UUID) -> int:
    now = clock.now()
    n = 0
    rows = conn.execute(
        """
        SELECT id, project_id, user_id, role, expires_at
        FROM temporary_grants
        WHERE revoked_at IS NULL AND expires_at <= %s
        """,
        (now,),
    ).fetchall()
    for row in rows:
        n += _safe_insert(
            conn,
            campaign_id=campaign_id,
            project_id=row["project_id"],
            kind="expired_temporary_grant",
            subject_user_id=row["user_id"],
            temporary_grant_id=row["id"],
            summary=f"droit temporaire {row['role']} expiré le {row['expires_at'].isoformat()}",
        )
    return n


def list_campaigns(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    if not (
        principal.is_platform_admin
        or principal.is_global_auditor
        or any(b.role in ("project_admin", "auditor") for b in principal.bindings)
    ):
        raise forbidden("rôle project_admin, auditeur ou administrateur requis")
    rows = conn.execute(
        "SELECT id, title, status, prepared_at, closed_at, note FROM access_review_campaigns ORDER BY prepared_at DESC"
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "status": r["status"],
            "prepared_at": r["prepared_at"].isoformat(),
            "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
            "note": r["note"],
        }
        for r in rows
    ]


def get_campaign(conn: Conn, principal: Principal, campaign_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM access_review_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if row is None:
        raise not_found("campagne")
    items = list_items(conn, principal, campaign_id)
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "status": row["status"],
        "prepared_at": row["prepared_at"].isoformat(),
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "note": row["note"],
        "items": items,
        "item_count": len(items),
    }


def list_items(conn: Conn, principal: Principal, campaign_id: UUID) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM access_review_campaigns WHERE id = %s", (campaign_id,)).fetchone() is None:
        raise not_found("campagne")
    rows = conn.execute(
        """
        SELECT i.*, p.slug AS project_slug, u.email AS subject_email
        FROM access_review_items i
        JOIN projects p ON p.id = i.project_id
        LEFT JOIN users u ON u.id = i.subject_user_id
        WHERE i.campaign_id = %s
        ORDER BY i.project_id, i.kind
        """,
        (campaign_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not (
            principal.is_platform_admin
            or principal.is_global_auditor
            or principal.has_role("project_admin", r["project_id"])
            or principal.has_role("auditor", r["project_id"])
        ):
            continue
        out.append(_item_view(r))
    return out


def get_item(conn: Conn, principal: Principal, item_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT i.*, p.slug AS project_slug, u.email AS subject_email
        FROM access_review_items i
        JOIN projects p ON p.id = i.project_id
        LEFT JOIN users u ON u.id = i.subject_user_id
        WHERE i.id = %s
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise not_found("élément de revue")
    if not (
        principal.is_platform_admin
        or principal.is_global_auditor
        or principal.has_role("project_admin", row["project_id"])
        or principal.has_role("auditor", row["project_id"])
    ):
        raise not_found("élément de revue")
    return _item_view(row)


def _item_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "campaign_id": str(r["campaign_id"]),
        "project_id": str(r["project_id"]),
        "project_slug": r.get("project_slug"),
        "kind": r["kind"],
        "subject_user_id": str(r["subject_user_id"]) if r.get("subject_user_id") else None,
        "subject_email": r.get("subject_email"),
        "binding_id": str(r["binding_id"]) if r.get("binding_id") else None,
        "exception_id": str(r["exception_id"]) if r.get("exception_id") else None,
        "temporary_grant_id": str(r["temporary_grant_id"]) if r.get("temporary_grant_id") else None,
        "summary": r["summary"],
        "status": r["status"],
        "decided_at": r["decided_at"].isoformat() if r.get("decided_at") else None,
        "decision_note": r.get("decision_note"),
    }


def decide(
    conn: Conn,
    principal: Principal,
    item_id: UUID,
    *,
    decision: str,
    note: str = "",
    request_id: str | None = None,
) -> dict[str, Any]:
    if decision not in ("confirmed", "revoked"):
        raise ApiError(422, "invalid_decision", "decision attendue : confirmed ou revoked", field="/decision")
    row = conn.execute("SELECT * FROM access_review_items WHERE id = %s FOR UPDATE", (item_id,)).fetchone()
    if row is None:
        raise not_found("élément de revue")
    if not _may_manage_project(principal, row["project_id"]):
        # Hors périmètre : 404 (pas 403) pour ne pas révéler l'existence.
        raise not_found("élément de revue")
    if row["status"] != "pending":
        raise ApiError(409, "already_decided", "élément déjà tranché")
    campaign = conn.execute(
        "SELECT status FROM access_review_campaigns WHERE id = %s", (row["campaign_id"],)
    ).fetchone()
    if campaign is None or campaign["status"] != "open":
        raise ApiError(409, "campaign_closed", "campagne clôturée")
    note_text = redact((note or "").strip())
    if note_text and not 1 <= len(note_text) <= 500:
        raise ApiError(422, "invalid_note", "note invalide", field="/note")
    if decision == "revoked":
        _apply_revoke(conn, principal, row)
    conn.execute(
        "UPDATE access_review_items SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s "
        "WHERE id = %s",
        (decision, principal.user_id, note_text or None, item_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action=f"access_review.{decision}",
        outcome="ok",
        project_id=row["project_id"],
        targets={"item_id": str(item_id), "kind": row["kind"], "note": note_text or None},
        request_id=request_id,
    )
    return get_item(conn, principal, item_id)


def _apply_revoke(conn: Conn, principal: Principal, item: dict[str, Any]) -> None:
    if item["kind"] == "unjustified_binding" and item.get("binding_id"):
        conn.execute(
            "UPDATE role_bindings SET revoked_at = now(), revoked_by = %s WHERE id = %s AND revoked_at IS NULL",
            (principal.user_id, item["binding_id"]),
        )
        if item.get("subject_user_id"):
            identity.bump_privilege_epoch(conn, item["subject_user_id"], "access_review_revoke")
    elif item["kind"] == "expired_exception" and item.get("exception_id"):
        conn.execute(
            "UPDATE policy_exceptions SET revoked_at = now(), revoked_by = %s, revoke_reason = %s "
            "WHERE id = %s AND revoked_at IS NULL",
            (principal.user_id, "revue d'accès : exception expirée", item["exception_id"]),
        )
    elif item["kind"] == "expired_temporary_grant" and item.get("temporary_grant_id"):
        conn.execute(
            "UPDATE temporary_grants SET revoked_at = now(), revoked_by = %s, revoke_reason = %s "
            "WHERE id = %s AND revoked_at IS NULL",
            (principal.user_id, "revue d'accès : droit temporaire expiré", item["temporary_grant_id"]),
        )
        if item.get("subject_user_id"):
            identity.bump_privilege_epoch(conn, item["subject_user_id"], "access_review_revoke")
    elif item["kind"] == "orphan_account":
        # Confirmation attendue côté métier ; la révocation orpheline ne détruit pas le projet.
        pass
