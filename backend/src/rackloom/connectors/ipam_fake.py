"""Point d'entrée connecteur pour l'adaptateur IPAM fictif (F03)."""

from __future__ import annotations

from ..ipam.fake import POOL_CIDR, candidates, capabilities, dns_name_for, next_address

__all__ = ["POOL_CIDR", "capabilities", "candidates", "dns_name_for", "next_address"]
