"""Logique pure de l'adaptateur IPAM fictif (aucune I/O). Voir aussi connectors.ipam_fake."""

from __future__ import annotations

import ipaddress
from typing import Any

POOL_CIDR = "192.0.2.0/24"
FIRST_HOST = 10


def candidates(cidr: str = POOL_CIDR, *, skip: set[str] | None = None) -> list[str]:
    network = ipaddress.ip_network(cidr)
    used = skip or set()
    out: list[str] = []
    for host in network.hosts():
        if int(host) < int(network.network_address) + FIRST_HOST:
            continue
        addr = str(host)
        if addr not in used:
            out.append(addr)
    return out


def next_address(used: set[str], cidr: str = POOL_CIDR) -> str | None:
    free = candidates(cidr, skip=used)
    return free[0] if free else None


def dns_name_for(hostname: str, suffix: str) -> str:
    host = hostname.strip().lower().replace(" ", "-")
    return f"{host}.{suffix.lstrip('.')}"


def capabilities() -> dict[str, Any]:
    return {
        "slug": "ipam-fake",
        "mode": "simulated",
        "prepare": True,
        "reserve": True,
        "confirm": True,
        "release": True,
        "preview": True,
        "notice": "Adaptateur fictif : aucune réservation externe.",
    }
