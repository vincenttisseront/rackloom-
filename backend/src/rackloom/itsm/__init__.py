"""F25 : ITSM et notifications.

Relie demandes et jobs à des tickets via un adaptateur testable (fictif en labo).
Livraisons idempotentes (`dedupe_key`) avec reprise. Les notifications pointent vers
une page authentifiée de Rackloom. Un webhook répété ne crée pas un second ticket ;
répondre à une notification / webhook ne peut jamais approuver un changement.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from .. import audit, events
from ..canonical import digest
from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact
from . import fake as itsm_fake

DEFAULT_ADAPTER_SLUG = itsm_fake.SLUG
APPROVAL_ACTIONS = frozenset(
    {
        "approve",
        "approved",
        "approval",
        "reject",
        "rejected",
        "decide",
        "decision",
    }
)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def change_href(change_id: UUID) -> str:
    return f"/demandes/{change_id}"


def change_absolute_url(change_id: UUID) -> str:
    origin = get_settings().public_origin.rstrip("/")
    return f"{origin}{change_href(change_id)}"


def get_adapter(conn: Conn, slug: str = DEFAULT_ADAPTER_SLUG) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM itsm_adapters WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise ApiError(503, "itsm_unavailable", "aucun adaptateur ITSM configuré")
    return row


def adapter_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "kind": row["kind"],
        "title": row["title"],
        "simulated": row["simulated"],
        "enabled": row["enabled"],
        "capabilities": itsm_fake.capabilities() if row["kind"] == "fake" else {},
    }


def ticket_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "adapter_id": str(row["adapter_id"]),
        "project_id": str(row["project_id"]),
        "change_id": str(row["change_id"]),
        "job_id": str(row["job_id"]) if row["job_id"] else None,
        "external_ticket_id": row["external_ticket_id"],
        "external_key": row["external_key"],
        "title": row["title"],
        "status": row["status"],
        "dedupe_key": row["dedupe_key"],
        "rackloom_url": row["rackloom_url"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "simulated": True,
    }


def delivery_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "adapter_id": str(row["adapter_id"]),
        "ticket_id": str(row["ticket_id"]) if row["ticket_id"] else None,
        "change_id": str(row["change_id"]),
        "job_id": str(row["job_id"]) if row["job_id"] else None,
        "kind": row["kind"],
        "dedupe_key": row["dedupe_key"],
        "status": row["status"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "delivered_at": row["delivered_at"].isoformat() if row["delivered_at"] else None,
        "result": row["result"] or {},
    }


def list_tickets_for_change(conn: Conn, change_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM itsm_tickets WHERE change_id = %s ORDER BY created_at",
        (change_id,),
    ).fetchall()
    return [ticket_view(r) for r in rows]


def get_ticket_for_change(
    conn: Conn, change_id: UUID, *, adapter_slug: str = DEFAULT_ADAPTER_SLUG
) -> dict[str, Any] | None:
    adapter = get_adapter(conn, adapter_slug)
    row = conn.execute(
        "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND change_id = %s",
        (adapter["id"], change_id),
    ).fetchone()
    return ticket_view(row) if row else None


def _next_sequence(conn: Conn, adapter_id: UUID) -> int:
    row = conn.execute(
        "SELECT count(*)::int AS n FROM itsm_tickets WHERE adapter_id = %s",
        (adapter_id,),
    ).fetchone()
    assert row is not None
    return int(row["n"] or 0) + 1


def _enqueue(
    conn: Conn,
    *,
    adapter_id: UUID,
    change_id: UUID,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    job_id: UUID | None = None,
    ticket_id: UUID | None = None,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM itsm_deliveries WHERE adapter_id = %s AND dedupe_key = %s",
        (adapter_id, dedupe_key),
    ).fetchone()
    if existing is not None:
        return delivery_view(existing)
    did = uuid7()
    conn.execute(
        """
        INSERT INTO itsm_deliveries
            (id, adapter_id, ticket_id, change_id, job_id, kind, dedupe_key, status, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (adapter_id, dedupe_key) DO NOTHING
        """,
        (
            did,
            adapter_id,
            ticket_id,
            change_id,
            job_id,
            kind,
            dedupe_key,
            Jsonb(redact(payload)),
        ),
    )
    row = conn.execute(
        "SELECT * FROM itsm_deliveries WHERE adapter_id = %s AND dedupe_key = %s",
        (adapter_id, dedupe_key),
    ).fetchone()
    assert row is not None
    return delivery_view(row)


def sync_change(
    conn: Conn,
    principal: Principal,
    change_id: UUID,
    *,
    adapter_slug: str = DEFAULT_ADAPTER_SLUG,
    job_id: UUID | None = None,
) -> dict[str, Any]:
    """Lie la demande à un ticket (création ou mise à jour) puis livre les pending."""
    from .. import notifications as notif
    from ..changes.service import change_ref, get_change

    change = get_change(conn, principal, change_id)
    adapter = get_adapter(conn, adapter_slug)
    if not adapter["enabled"]:
        raise ApiError(409, "itsm_disabled", "adaptateur ITSM désactivé")

    create_key = f"ticket_create:change:{change_id}"
    existing = conn.execute(
        "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND dedupe_key = %s",
        (adapter["id"], f"change:{change_id}"),
    ).fetchone()

    href = change_href(change_id)
    absolute = change_absolute_url(change_id)
    ref = change_ref(change)
    title = f"[{ref}] {change['title']}"[:300]

    if existing is None:
        _enqueue(
            conn,
            adapter_id=adapter["id"],
            change_id=change_id,
            job_id=job_id,
            kind="ticket_create",
            dedupe_key=create_key,
            payload={
                "title": title,
                "change_id": str(change_id),
                "change_ref": ref,
                "change_state": change["state"],
                "project_id": str(change["project_id"]),
                "rackloom_url": absolute,
                "href": href,
                "job_id": str(job_id) if job_id else None,
            },
        )
    else:
        update_key = f"ticket_update:change:{change_id}:state:{change['state']}"
        if job_id:
            update_key = f"ticket_update:change:{change_id}:job:{job_id}"
        _enqueue(
            conn,
            adapter_id=adapter["id"],
            change_id=change_id,
            job_id=job_id,
            ticket_id=existing["id"],
            kind="ticket_update",
            dedupe_key=update_key,
            payload={
                "external_ticket_id": existing["external_ticket_id"],
                "status": "in_progress"
                if change["state"] not in ("succeeded", "failed", "rejected", "cancelled", "partially_succeeded")
                else "resolved",
                "comment": f"État Rackloom : {change['state']}",
                "rackloom_url": absolute,
                "change_state": change["state"],
            },
        )

    delivered = deliver_pending(conn, adapter_id=adapter["id"])
    ticket = get_ticket_for_change(conn, change_id, adapter_slug=adapter_slug)

    # Notification au demandeur (idempotente) pointant vers la page authentifiée.
    if ticket:
        notif.notify_user(
            conn,
            user_id=change["created_by"],
            project_id=change["project_id"],
            change_id=change_id,
            ticket_id=UUID(ticket["id"]),
            kind="itsm_ticket",
            title=f"Ticket {ticket['external_key']}",
            body=f"Demande reliée au ticket {ticket['external_key']} (adaptateur {adapter['slug']}).",
            href=href,
            dedupe_key=f"itsm_ticket:{ticket['id']}:user:{change['created_by']}",
        )

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="itsm.sync",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change_id,
        targets={
            "change_id": str(change_id),
            "adapter": adapter["slug"],
            "ticket_id": ticket["id"] if ticket else None,
            "delivered": delivered.get("delivered", 0),
        },
    )
    events.emit(
        conn,
        type="itsm.synced",
        source="system",
        project_id=change["project_id"],
        correlation_id=change_id,
        data={
            "adapter": adapter["slug"],
            "ticket_key": ticket["external_key"] if ticket else None,
            "simulated": True,
        },
        dedupe_key=f"itsm.synced:{change_id}:{ticket['id'] if ticket else 'none'}",
    )
    return {
        "adapter": adapter_view(adapter),
        "ticket": ticket,
        "deliveries": delivered,
        "simulated": True,
    }


def deliver_pending(conn: Conn, *, adapter_id: UUID | None = None, limit: int = 100) -> dict[str, Any]:
    """Livre les pending/failed ; rejeu sans doublon grâce à dedupe_key + ON CONFLICT tickets."""
    params: list[Any] = []
    where = "d.status IN ('pending', 'failed')"
    if adapter_id is not None:
        where += " AND d.adapter_id = %s"
        params.append(adapter_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT d.*, a.kind AS adapter_kind, a.slug AS adapter_slug
        FROM itsm_deliveries d
        JOIN itsm_adapters a ON a.id = d.adapter_id
        WHERE {where}
        ORDER BY d.created_at
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()

    delivered = failed = skipped = 0
    for row in rows:
        try:
            if row["kind"] == "ticket_create":
                result = _deliver_create(conn, row)
            elif row["kind"] == "ticket_update":
                result = _deliver_update(conn, row)
            elif row["kind"] == "notification":
                result = {"ok": True, "kind": "notification"}
            else:
                raise ApiError(500, "unknown_delivery", f"kind inconnu : {row['kind']}")
            ticket_uuid = result.get("ticket_uuid")
            ticket_id_val = UUID(str(ticket_uuid)) if ticket_uuid else None
            conn.execute(
                """
                UPDATE itsm_deliveries SET status = 'delivered', delivered_at = now(),
                    attempts = attempts + 1, last_error = NULL, result = %s,
                    ticket_id = COALESCE(%s, ticket_id)
                WHERE id = %s
                """,
                (Jsonb(result), ticket_id_val, row["id"]),
            )
            if result.get("already_existed"):
                skipped += 1
            else:
                delivered += 1
        except ApiError as exc:
            conn.execute(
                """
                UPDATE itsm_deliveries SET status = 'failed', attempts = attempts + 1,
                    last_error = %s WHERE id = %s
                """,
                (exc.message[:500], row["id"]),
            )
            failed += 1
        except Exception as exc:  # noqa: BLE001 — reprise : marquer failed, ne pas casser le lot
            conn.execute(
                """
                UPDATE itsm_deliveries SET status = 'failed', attempts = attempts + 1,
                    last_error = %s WHERE id = %s
                """,
                (str(exc)[:500], row["id"]),
            )
            failed += 1
    return {
        "delivered": delivered,
        "failed": failed,
        "already_present": skipped,
        "simulated": True,
    }


def _deliver_create(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    change_id = row["change_id"]
    adapter_id = row["adapter_id"]
    ticket_dedupe = f"change:{change_id}"
    existing = conn.execute(
        "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND dedupe_key = %s",
        (adapter_id, ticket_dedupe),
    ).fetchone()
    if existing is not None:
        return {
            "already_existed": True,
            "external_ticket_id": existing["external_ticket_id"],
            "external_key": existing["external_key"],
            "ticket_uuid": str(existing["id"]),
            "simulated": True,
        }

    payload = dict(row["payload"] or {})
    seq = _next_sequence(conn, adapter_id)
    payload["sequence"] = seq
    created = itsm_fake.create_ticket(payload)
    change = conn.execute("SELECT project_id, created_by, title FROM changes WHERE id = %s", (change_id,)).fetchone()
    if change is None:
        raise ApiError(404, "not_found", "demande introuvable pour livraison ITSM")

    tid = uuid7()
    absolute = payload.get("rackloom_url") or change_absolute_url(change_id)
    title = str(payload.get("title") or change["title"])[:300]
    inserted = conn.execute(
        """
        INSERT INTO itsm_tickets
            (id, adapter_id, project_id, change_id, job_id, external_ticket_id, external_key,
             title, status, dedupe_key, rackloom_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (adapter_id, dedupe_key) DO NOTHING
        RETURNING *
        """,
        (
            tid,
            adapter_id,
            change["project_id"],
            change_id,
            row["job_id"],
            created["external_ticket_id"],
            created["external_key"],
            title,
            created["status"],
            ticket_dedupe,
            absolute,
        ),
    ).fetchone()
    if inserted is None:
        existing = conn.execute(
            "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND dedupe_key = %s",
            (adapter_id, ticket_dedupe),
        ).fetchone()
        assert existing is not None
        return {
            "already_existed": True,
            "external_ticket_id": existing["external_ticket_id"],
            "external_key": existing["external_key"],
            "ticket_uuid": str(existing["id"]),
            "simulated": True,
        }
    return {
        "already_existed": False,
        "external_ticket_id": inserted["external_ticket_id"],
        "external_key": inserted["external_key"],
        "ticket_uuid": str(inserted["id"]),
        "simulated": True,
    }


def _deliver_update(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row["payload"] or {})
    external_id = payload.get("external_ticket_id")
    ticket = None
    if row["ticket_id"]:
        ticket = conn.execute("SELECT * FROM itsm_tickets WHERE id = %s", (row["ticket_id"],)).fetchone()
    if ticket is None and external_id:
        ticket = conn.execute(
            "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND external_ticket_id = %s",
            (row["adapter_id"], external_id),
        ).fetchone()
    if ticket is None:
        raise ApiError(404, "not_found", "ticket ITSM introuvable pour mise à jour")
    updated = itsm_fake.update_ticket(ticket["external_ticket_id"], payload)
    conn.execute(
        """
        UPDATE itsm_tickets SET status = %s, updated_at = now(),
            job_id = COALESCE(%s, job_id)
        WHERE id = %s
        """,
        (updated["status"], row["job_id"], ticket["id"]),
    )
    return {**updated, "ticket_uuid": str(ticket["id"]), "already_existed": False}


def process_webhook(
    conn: Conn,
    *,
    adapter_slug: str,
    token: str | None,
    body: dict[str, Any],
    dedupe_header: str | None = None,
) -> dict[str, Any]:
    """Traite un webhook entrant. Jamais d'approbation. Rejeu → duplicate, même ticket."""
    adapter = get_adapter(conn, adapter_slug)
    if not adapter["enabled"]:
        raise ApiError(409, "itsm_disabled", "adaptateur ITSM désactivé")
    if not adapter["webhook_token_hash"]:
        raise ApiError(503, "webhook_disabled", "webhook ITSM non configuré")
    if not token or _hash_token(token) != adapter["webhook_token_hash"]:
        raise ApiError(401, "unauthenticated", "jeton webhook ITSM absent ou invalide")

    event_type = str(body.get("event_type") or body.get("action") or "comment").strip().lower()[:80]
    if not event_type:
        raise ApiError(422, "invalid_event", "event_type requis", field="/event_type")

    # Aucun canal entrant ne crée d'approbation (invariant F25 / assistant-like).
    if (
        event_type in APPROVAL_ACTIONS
        or body.get("approve")
        or body.get("decision")
        in (
            "approved",
            "rejected",
        )
    ):
        dedupe = _inbound_dedupe(adapter["id"], body, dedupe_header, event_type)
        eid = uuid7()
        conn.execute(
            """
            INSERT INTO itsm_inbound_events
                (id, adapter_id, dedupe_key, event_type, external_ticket_id, payload,
                 outcome, rejection_code)
            VALUES (%s, %s, %s, %s, %s, %s, 'rejected', 'approval_forbidden')
            ON CONFLICT (adapter_id, dedupe_key) DO NOTHING
            """,
            (
                eid,
                adapter["id"],
                dedupe,
                event_type,
                str(body.get("external_ticket_id") or "")[:120] or None,
                Jsonb(redact(body)),
            ),
        )
        audit.record(
            conn,
            actor_type="system",
            actor_id="itsm-webhook",
            action="itsm.webhook.rejected",
            outcome="denied",
            targets={"adapter": adapter_slug, "event_type": event_type, "code": "approval_forbidden"},
        )
        raise ApiError(
            403,
            "approval_forbidden",
            "un webhook ou une réponse à une notification ne peut pas approuver un changement",
            commit=True,
        )

    dedupe = _inbound_dedupe(adapter["id"], body, dedupe_header, event_type)
    existing = conn.execute(
        "SELECT * FROM itsm_inbound_events WHERE adapter_id = %s AND dedupe_key = %s",
        (adapter["id"], dedupe),
    ).fetchone()
    if existing is not None:
        ticket = None
        if existing["external_ticket_id"]:
            ticket = conn.execute(
                "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND external_ticket_id = %s",
                (adapter["id"], existing["external_ticket_id"]),
            ).fetchone()
        return {
            "outcome": "duplicate",
            "event_id": str(existing["id"]),
            "ticket": ticket_view(ticket) if ticket else None,
            "created_ticket": False,
            "simulated": True,
        }

    external_ticket_id = str(body.get("external_ticket_id") or "").strip()[:120] or None
    change_id_raw = body.get("change_id")
    created_ticket = False
    ticket_row = None

    if external_ticket_id:
        ticket_row = conn.execute(
            "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND external_ticket_id = %s",
            (adapter["id"], external_ticket_id),
        ).fetchone()

    # Création demandée explicitement : toujours via dedupe change:{id} → pas de 2e ticket.
    if event_type in ("ticket_create", "create") and change_id_raw:
        try:
            change_id = UUID(str(change_id_raw))
        except ValueError as exc:
            raise ApiError(422, "invalid_change", "change_id invalide", field="/change_id") from exc
        change = conn.execute("SELECT * FROM changes WHERE id = %s", (change_id,)).fetchone()
        if change is None:
            raise not_found("demande")
        ticket_dedupe = f"change:{change_id}"
        ticket_row = conn.execute(
            "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND dedupe_key = %s",
            (adapter["id"], ticket_dedupe),
        ).fetchone()
        if ticket_row is None:
            seq = _next_sequence(conn, adapter["id"])
            created = itsm_fake.create_ticket(
                {
                    "sequence": seq,
                    "title": f"Webhook {change['title']}"[:300],
                    "rackloom_url": change_absolute_url(change_id),
                }
            )
            tid = uuid7()
            ticket_row = conn.execute(
                """
                INSERT INTO itsm_tickets
                    (id, adapter_id, project_id, change_id, external_ticket_id, external_key,
                     title, status, dedupe_key, rackloom_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)
                ON CONFLICT (adapter_id, dedupe_key) DO NOTHING
                RETURNING *
                """,
                (
                    tid,
                    adapter["id"],
                    change["project_id"],
                    change_id,
                    created["external_ticket_id"],
                    created["external_key"],
                    f"[CHG] {change['title']}"[:300],
                    ticket_dedupe,
                    change_absolute_url(change_id),
                ),
            ).fetchone()
            if ticket_row is None:
                ticket_row = conn.execute(
                    "SELECT * FROM itsm_tickets WHERE adapter_id = %s AND dedupe_key = %s",
                    (adapter["id"], ticket_dedupe),
                ).fetchone()
            else:
                created_ticket = True
        external_ticket_id = ticket_row["external_ticket_id"] if ticket_row else external_ticket_id

    if ticket_row and body.get("status") in ("open", "in_progress", "resolved", "closed"):
        conn.execute(
            "UPDATE itsm_tickets SET status = %s, updated_at = now() WHERE id = %s",
            (body["status"], ticket_row["id"]),
        )
        ticket_row = conn.execute("SELECT * FROM itsm_tickets WHERE id = %s", (ticket_row["id"],)).fetchone()

    eid = uuid7()
    conn.execute(
        """
        INSERT INTO itsm_inbound_events
            (id, adapter_id, dedupe_key, event_type, external_ticket_id, payload, outcome)
        VALUES (%s, %s, %s, %s, %s, %s, 'recorded')
        """,
        (
            eid,
            adapter["id"],
            dedupe,
            event_type,
            external_ticket_id,
            Jsonb(redact(body)),
        ),
    )
    audit.record(
        conn,
        actor_type="system",
        actor_id="itsm-webhook",
        action="itsm.webhook.recorded",
        outcome="ok",
        targets={
            "adapter": adapter_slug,
            "event_type": event_type,
            "external_ticket_id": external_ticket_id,
            "created_ticket": created_ticket,
        },
    )
    return {
        "outcome": "recorded",
        "event_id": str(eid),
        "ticket": ticket_view(ticket_row) if ticket_row else None,
        "created_ticket": created_ticket,
        "simulated": True,
    }


def _inbound_dedupe(adapter_id: UUID, body: dict[str, Any], header: str | None, event_type: str) -> str:
    if header and header.strip():
        return f"hdr:{header.strip()[:180]}"
    if body.get("dedupe_key"):
        return f"body:{str(body['dedupe_key'])[:180]}"
    # Empreinte stable du corps utile (sans champs volatils).
    material = {
        "event_type": event_type,
        "external_ticket_id": body.get("external_ticket_id"),
        "change_id": body.get("change_id"),
        "comment": body.get("comment"),
        "status": body.get("status"),
        "approve": body.get("approve"),
        "decision": body.get("decision"),
    }
    return f"hash:{digest(material)[:40]}"


def list_adapters(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    if not (principal.is_platform_admin or principal.is_global_auditor):
        raise forbidden("rôle administrateur ou auditeur requis")
    rows = conn.execute("SELECT * FROM itsm_adapters ORDER BY slug").fetchall()
    return [adapter_view(r) for r in rows]


def flush_deliveries(conn: Conn, principal: Principal) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise forbidden("rôle administrateur de plateforme requis")
    result = deliver_pending(conn)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="itsm.deliver.flush",
        outcome="ok",
        targets=result,
    )
    return result
