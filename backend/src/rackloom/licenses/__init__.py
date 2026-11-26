"""F34 : contrôle des licences avant déploiement.

Exigences déduites du profil (`deployment_policy.licenses`) ; source locale fictive
interrogée à l'admission (R125). États : available, unavailable, unverified. La politique
décide si « non vérifié » bloque (`license_unverified_policy`). Aucune clé collée par
l'utilisateur.
"""

from __future__ import annotations

from typing import Any

from .. import policy
from ..db import Conn


def requirements_from(resolution: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Liste [{sku, quantity}] depuis la révision figée ; vide = R125 sans effet."""
    if not resolution:
        return []
    specs = resolution.get("licenses") or []
    quantity = len(resolution.get("instances") or []) or 1
    out: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict) or "sku" not in spec:
            continue
        per = int(spec.get("per_instance", 1))
        out.append({"sku": str(spec["sku"]), "quantity": per * quantity})
    return out


def set_quota(conn: Conn, sku: str, available: int, *, reachable: bool = True) -> None:
    conn.execute(
        "INSERT INTO license_quotas (sku, available, reachable, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (sku) DO UPDATE SET available = EXCLUDED.available, reachable = EXCLUDED.reachable, "
        "updated_at = now()",
        (sku, available, reachable),
    )


def check(conn: Conn, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    """Interroge la source fictive. `status` global : available | unavailable | unverified."""
    if not requirements:
        return {"status": "available", "items": []}
    items: list[dict[str, Any]] = []
    status = "available"
    for req in requirements:
        row = conn.execute("SELECT * FROM license_quotas WHERE sku = %s", (req["sku"],)).fetchone()
        if row is None or not row["reachable"]:
            items.append({"sku": req["sku"], "needed": req["quantity"], "status": "unverified",
                          "available": None})
            if status != "unavailable":
                status = "unverified"
            continue
        ok = int(row["available"]) >= int(req["quantity"])
        items.append({"sku": req["sku"], "needed": req["quantity"], "status": "available" if ok else "unavailable",
                      "available": int(row["available"])})
        if not ok:
            status = "unavailable"
    return {"status": status, "items": items}


def unverified_policy() -> str:
    return str(policy.BUILTIN_RULES["platform"].get("license_unverified_policy", "block"))


def refusal(result: dict[str, Any]) -> tuple[str, str]:
    """(code, message) pour l'admission."""
    if result["status"] == "unavailable":
        skus = ", ".join(i["sku"] for i in result["items"] if i["status"] == "unavailable")
        return "license_unavailable", (
            f"licence indisponible pour {skus} : quota épuisé. "
            "Contactez l'administrateur du projet ; aucune clé n'est demandée."
        )
    return "license_unverified", (
        "contrôle de licence non vérifié (source injoignable). "
        "La politique bloque le déploiement jusqu'à vérification."
    )
