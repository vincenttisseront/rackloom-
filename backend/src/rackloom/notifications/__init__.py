"""Notifications in-app (F25).

Chaque notification pointe vers une page authentifiée de Rackloom (`href` relatif).
Répondre à une notification ne peut pas approuver un changement (pas de route d'action
sensible ici ; l'approbation reste `POST /changes/{id}/approvals` avec session + CSRF).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..config import get_settings
from ..db import Conn
from ..errors import not_found
from ..identity.authz import Principal
from ..ids import uuid7


def absolute_url(href: str) -> str:
    origin = get_settings().public_origin.rstrip("/")
    path = href if href.startswith("/") else f"/{href}"
    return f"{origin}{path}"


def notify_user(
    conn: Conn,
    *,
    user_id: UUID,
    title: str,
    body: str,
    href: str,
    dedupe_key: str,
    kind: str = "change_update",
    project_id: UUID | None = None,
    change_id: UUID | None = None,
    ticket_id: UUID | None = None,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM notifications WHERE user_id = %s AND dedupe_key = %s",
        (user_id, dedupe_key),
    ).fetchone()
    if existing is not None:
        return view(existing)
    path = href if href.startswith("/") else f"/{href}"
    nid = uuid7()
    conn.execute(
        """
        INSERT INTO notifications
            (id, project_id, user_id, change_id, ticket_id, kind, title, body,
             href, absolute_url, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, dedupe_key) DO NOTHING
        """,
        (
            nid,
            project_id,
            user_id,
            change_id,
            ticket_id,
            kind,
            title[:300],
            (body or "")[:1000],
            path[:300],
            absolute_url(path)[:500],
            dedupe_key[:200],
        ),
    )
    row = conn.execute(
        "SELECT * FROM notifications WHERE user_id = %s AND dedupe_key = %s",
        (user_id, dedupe_key),
    ).fetchone()
    assert row is not None
    return view(row)


def view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]) if row["project_id"] else None,
        "user_id": str(row["user_id"]),
        "change_id": str(row["change_id"]) if row["change_id"] else None,
        "ticket_id": str(row["ticket_id"]) if row["ticket_id"] else None,
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "href": row["href"],
        "absolute_url": row["absolute_url"],
        "read_at": row["read_at"].isoformat() if row["read_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        # Pas d'action d'approbation exposée : lien de lecture seulement.
        "actions": [],
        "can_approve": False,
    }


def list_for_user(
    conn: Conn, principal: Principal, *, unread_only: bool = False, limit: int = 50
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    if unread_only:
        rows = conn.execute(
            """
            SELECT * FROM notifications WHERE user_id = %s AND read_at IS NULL
            ORDER BY created_at DESC LIMIT %s
            """,
            (principal.user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM notifications WHERE user_id = %s
            ORDER BY created_at DESC LIMIT %s
            """,
            (principal.user_id, limit),
        ).fetchall()
    return [view(r) for r in rows]


def mark_read(conn: Conn, principal: Principal, notification_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM notifications WHERE id = %s AND user_id = %s",
        (notification_id, principal.user_id),
    ).fetchone()
    if row is None:
        raise not_found("notification")
    if row["read_at"] is None:
        conn.execute(
            "UPDATE notifications SET read_at = now() WHERE id = %s",
            (notification_id,),
        )
        row = conn.execute("SELECT * FROM notifications WHERE id = %s", (notification_id,)).fetchone()
    assert row is not None
    return view(row)
