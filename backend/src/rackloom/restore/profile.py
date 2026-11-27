"""F40 : profil de restauration pur (utilisable par le résolveur, sans file)."""

from __future__ import annotations

from typing import Any

from ..errors import ApiError

QUARANTINE_ROLE = "quarantine"


def restore_profile(contract: dict[str, Any]) -> dict[str, Any]:
    profile = contract.get("restore_profile") or {}
    return {
        "network_role": profile.get("network_role") or QUARANTINE_ROLE,
        "naming": profile.get("naming") or "{project_code}-{environment}-rst-{nn}",
        "folder": profile.get("folder"),
        "attach_production_network": False,
        "max_age_hours": int((profile.get("backup") or {}).get("max_age_hours") or 24),
        "backup_policy": (profile.get("backup") or {}).get("policy") or "restore-verify",
    }


def assert_isolated_network(network: dict[str, Any]) -> None:
    role = network.get("role") or (network.get("attributes") or {}).get("role")
    if role != QUARANTINE_ROLE:
        raise ApiError(
            422,
            "restore_network_not_quarantine",
            "la restauration n'autorise que le réseau de quarantaine isolé",
            field="/placement/network",
        )


def verify_plan(resolution: dict[str, Any], source_instance: str) -> None:
    """Contrôles post-résolution : quarantaine, nom distinct, pas de prod."""
    network = (resolution.get("placement") or {}).get("network") or {}
    role = network.get("role") or (network.get("attributes") or {}).get("role")
    if role != QUARANTINE_ROLE:
        raise ApiError(
            422,
            "restore_network_not_quarantine",
            "la restauration doit résoudre le réseau de quarantaine uniquement",
            field="/placement/network",
        )
    for inst in resolution.get("instances") or []:
        name = inst.get("name")
        if name == source_instance:
            raise ApiError(
                422,
                "restore_name_conflict",
                "le nom restauré doit être distinct de la VM d'origine",
                field="/instances/name",
            )
    restore_meta = resolution.get("restore") or {}
    if restore_meta.get("attach_production_network"):
        raise ApiError(
            422,
            "restore_production_network_forbidden",
            "rattachement automatique au réseau de production interdit",
        )
