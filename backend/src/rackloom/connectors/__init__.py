"""Contrat des connecteurs et matrice de capacités.

Chaque connecteur déclare, pour chaque capacité, si elle est disponible et dans quel mode
(`real`, `simulated`, `request` pour une annulation demandée mais non garantie). L'écran
Connecteurs affiche « non disponible » et « simulé » à partir de cette matrice.
"""

from __future__ import annotations

from typing import Any

CAPABILITIES = ("configure", "test_connection", "inventory", "plan", "execute", "track", "cancel")


def cap(mode: str | None, note: str = "") -> dict[str, Any]:
    return {"available": mode is not None, "mode": mode, "note": note}


MATRIX: dict[str, dict[str, dict[str, Any]]] = {
    "vsphere": {
        "configure": cap("real"), "test_connection": cap("simulated"), "inventory": cap("simulated"),
        "plan": cap(None), "execute": cap(None), "track": cap("simulated"), "cancel": cap(None),
    },
    "terraform": {
        "configure": cap("real"), "test_connection": cap("real", "version et miroir de providers"),
        "inventory": cap(None), "plan": cap("real"), "execute": cap("real", "plan sauvegardé uniquement"),
        "track": cap("real", "state"), "cancel": cap("request", "SIGINT, non garanti"),
    },
    "ansible": {
        "configure": cap("real"), "test_connection": cap("real", "environnement"), "inventory": cap(None),
        "plan": cap("real", "contrat du rôle seulement"), "execute": cap("real"), "track": cap("real"),
        "cancel": cap("request", "SIGTERM, non garanti"),
    },
    "model": {
        "configure": cap("real"), "test_connection": cap("simulated"), "inventory": cap(None),
        "plan": cap(None), "execute": cap(None), "track": cap(None), "cancel": cap(None),
    },
}


def capabilities_for(kind: str, mode: str) -> dict[str, dict[str, Any]]:
    matrix = MATRIX[kind]
    if mode == "simulated":
        return {k: (dict(v, mode="simulated") if v["available"] and v["mode"] == "real" and k != "configure"
                    else v) for k, v in matrix.items()}
    return matrix
