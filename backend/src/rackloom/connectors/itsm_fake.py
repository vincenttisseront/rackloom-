"""Point d'entrée connecteur pour l'adaptateur ITSM fictif (F25)."""

from __future__ import annotations

from ..itsm.fake import DEMO_WEBHOOK_TOKEN, KIND, SLUG, capabilities, create_ticket, update_ticket

__all__ = [
    "DEMO_WEBHOOK_TOKEN",
    "KIND",
    "SLUG",
    "capabilities",
    "create_ticket",
    "update_ticket",
]
