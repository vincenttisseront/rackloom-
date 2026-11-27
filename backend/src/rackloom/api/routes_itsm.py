"""F25 : ITSM, livraisons idempotentes, webhooks entrants, notifications."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import itsm, notifications
from ..errors import ApiError
from ..itsm.fake import DEMO_WEBHOOK_TOKEN
from .deps import bearer_token, csrf_guard, unit_of_work

router = APIRouter(tags=["itsm"], dependencies=[Depends(csrf_guard)])
# Webhooks : Bearer jeton adaptateur, pas de cookie CSRF.
webhook_router = APIRouter(tags=["itsm-webhooks"])


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


@router.get("/admin/itsm/adapters")
def list_adapters(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return itsm.list_adapters(u.conn, u.p)


@router.post("/admin/itsm/deliver")
def flush_deliveries(request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return itsm.flush_deliveries(u.conn, u.p)


@router.get("/changes/{change_id}/itsm")
def change_itsm(change_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        from ..changes.service import get_change

        change = get_change(u.conn, u.p, change_id)
        tickets = itsm.list_tickets_for_change(u.conn, change_id)
        return {
            "change_id": str(change["id"]),
            "tickets": tickets,
            "adapter": itsm.adapter_view(itsm.get_adapter(u.conn)),
            "simulated": True,
        }


@router.post("/changes/{change_id}/itsm/sync")
def sync_itsm(change_id: UUID, request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _only(body, {"job_id", "adapter"})
    job_id = None
    if body.get("job_id"):
        try:
            job_id = UUID(str(body["job_id"]))
        except ValueError as exc:
            raise ApiError(422, "invalid_job", "job_id invalide", field="/job_id") from exc
    adapter = str(body.get("adapter") or itsm.DEFAULT_ADAPTER_SLUG)
    with unit_of_work(request, write=True) as u:
        return itsm.sync_change(u.conn, u.p, change_id, adapter_slug=adapter, job_id=job_id)


@router.get("/notifications")
def list_notifications(request: Request) -> dict[str, Any]:
    unread = (request.query_params.get("unread") or "").lower() in ("1", "true", "yes")
    with unit_of_work(request) as u:
        items = notifications.list_for_user(u.conn, u.p, unread_only=unread)
        return {"items": items, "can_approve": False}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return notifications.mark_read(u.conn, u.p, notification_id)


@router.post("/notifications/{notification_id}/respond")
def respond_notification(
    notification_id: UUID, request: Request, body: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    """Canal explicite : toute tentative d'approbation via notification est refusée."""
    _only(body, {"action", "decision", "comment"})
    action = str(body.get("action") or body.get("decision") or "").strip().lower()
    if action in itsm.APPROVAL_ACTIONS or body.get("decision") in ("approved", "rejected"):
        raise ApiError(
            403,
            "approval_forbidden",
            "répondre à une notification ne peut pas approuver un changement",
        )
    with unit_of_work(request, write=True) as u:
        # Lecture seule métier : marquer lue ; aucune transition de changement.
        note = notifications.mark_read(u.conn, u.p, notification_id)
        return {
            "notification": note,
            "outcome": "acknowledged",
            "can_approve": False,
            "message": "accusé de réception enregistré ; l'approbation reste sur la page de la demande",
        }


@webhook_router.post("/itsm/webhooks/{adapter_slug}")
def inbound_webhook(
    adapter_slug: str, request: Request, body: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    token = bearer_token(request) or request.headers.get("x-itsm-token")
    dedupe = request.headers.get("idempotency-key") or request.headers.get("x-idempotency-key")
    with unit_of_work(request, write=True, anonymous=True) as u:
        return itsm.process_webhook(
            u.conn,
            adapter_slug=adapter_slug,
            token=token,
            body=body if isinstance(body, dict) else {},
            dedupe_header=dedupe,
        )


@webhook_router.get("/itsm/webhooks/{adapter_slug}/demo-token")
def demo_token_hint(adapter_slug: str, request: Request) -> dict[str, Any]:
    """Indique le jeton de démo uniquement en profil demo (jamais en production)."""
    from ..config import get_settings

    if get_settings().profile == "production":
        raise ApiError(404, "not_found", "non disponible")
    with unit_of_work(request) as u:
        adapter = itsm.get_adapter(u.conn, adapter_slug)
        if not u.p.is_platform_admin:
            raise ApiError(403, "forbidden", "administrateur requis")
        return {
            "adapter": adapter["slug"],
            "demo_token": DEMO_WEBHOOK_TOKEN if adapter["slug"] == "itsm-fake" else None,
            "simulated": True,
            "notice": "jeton de laboratoire uniquement ; jamais un secret réel",
        }
