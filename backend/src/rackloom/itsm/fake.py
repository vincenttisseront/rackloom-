"""Adaptateur ITSM fictif (laboratoire).

Aucun appel réseau. Les tickets vivent dans la base (`itsm_tickets`) ; cet adaptateur
produit uniquement les identifiants externes et valide les payloads de livraison.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..ids import uuid7

SLUG = "itsm-fake"
KIND = "fake"
# Jeton webhook de démonstration uniquement (empreinte en migration 0032).
DEMO_WEBHOOK_TOKEN = "itsm-fake-webhook-demo"  # noqa: S105


def capabilities() -> dict[str, Any]:
    return {
        "create_ticket": True,
        "update_ticket": True,
        "inbound_webhook": True,
        "approve_via_webhook": False,
        "simulated": True,
    }


def external_ids(*, sequence: int) -> tuple[str, str]:
    """Identifiants externes déterministes pour un numéro de séquence local."""
    ticket_id = f"fake-{sequence:06d}"
    key = f"RLM-{sequence}"
    return ticket_id, key


def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
    """Simule la création d'un ticket ; l'appelant persiste le résultat."""
    seq = int(payload.get("sequence") or 1)
    external_id, key = external_ids(sequence=seq)
    return {
        "external_ticket_id": external_id,
        "external_key": key,
        "status": "open",
        "simulated": True,
        "adapter": SLUG,
        "url_hint": payload.get("rackloom_url"),
    }


def update_ticket(external_ticket_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "in_progress")
    if status not in ("open", "in_progress", "resolved", "closed"):
        status = "in_progress"
    return {
        "external_ticket_id": external_ticket_id,
        "status": status,
        "comment": str(payload.get("comment") or "")[:500],
        "simulated": True,
        "adapter": SLUG,
    }


def new_delivery_id() -> UUID:
    return uuid7()
