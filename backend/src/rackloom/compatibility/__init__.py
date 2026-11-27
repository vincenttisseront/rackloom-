"""F38 : matrice de compatibilité active.

Compare les versions réellement utilisées (providers Terraform du lockfile, images
runner, composants) à une matrice versionnée. Combinaisons `incompatible` → blocage
(R135) ; absentes → `unverified` (politique).
"""

from __future__ import annotations

from typing import Any

from .. import policy, registry
from ..db import Conn
from ..ids import uuid7

MATRIX_VERSION = "matrix@1"


def seed_defaults(conn: Conn) -> None:
    """Matrice de démo : providers du module lab-vm vérifiés ; entrée vide pour tests."""
    defaults = [
        ("terraform_provider", "registry.terraform.io/hashicorp/local", "2.5.3", "verified", "miroir de test local"),
        ("terraform_provider", "registry.terraform.io/hashicorp/random", "3.7.2", "verified", "miroir de test local"),
        ("terraform", "lab-vm", "1.0.0", "verified", "module de démonstration"),
    ]
    for kind, name, version, status, note in defaults:
        upsert(conn, kind=kind, name=name, version=version, status=status, note=note)


def upsert(
    conn: Conn,
    *,
    kind: str,
    name: str,
    version: str,
    status: str,
    note: str = "",
    matrix_version: str = MATRIX_VERSION,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id FROM compatibility_matrix WHERE matrix_version = %s AND component_kind = %s "
        "AND name = %s AND version = %s",
        (matrix_version, kind, name, version),
    ).fetchone()
    if row:
        return conn.execute(
            "UPDATE compatibility_matrix SET status = %s, note = %s WHERE id = %s RETURNING *",
            (status, note, row["id"]),
        ).fetchone()
    return conn.execute(
        "INSERT INTO compatibility_matrix "
        "(id, matrix_version, component_kind, name, version, status, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), matrix_version, kind, name, version, status, note),
    ).fetchone()


def set_status(conn: Conn, *, kind: str, name: str, version: str, status: str, note: str = "") -> dict[str, Any]:
    return upsert(conn, kind=kind, name=name, version=version, status=status, note=note)


def used_versions(conn: Conn, resolution: dict[str, Any] | None) -> list[dict[str, str]]:
    """Versions figées dans la révision / dossier d'exécution."""
    if not resolution:
        return []
    out: list[dict[str, str]] = []
    tf = resolution.get("terraform") or {}
    if tf.get("module") and tf.get("version"):
        out.append({"kind": "terraform", "name": str(tf["module"]), "version": str(tf["version"])})
        module = registry.artifact(conn, "tf_module", tf["module"], tf["version"])
        if module and isinstance(module.get("manifest"), dict):
            for p in module["manifest"].get("providers") or []:
                if isinstance(p, dict) and p.get("source") and p.get("version"):
                    out.append(
                        {
                            "kind": "terraform_provider",
                            "name": str(p["source"]),
                            "version": str(p["version"]),
                        }
                    )
    for c in resolution.get("components") or []:
        if isinstance(c, dict) and c.get("slug") and c.get("version"):
            out.append({"kind": "component", "name": str(c["slug"]), "version": str(c["version"])})
            role = c.get("role") or {}
            if role.get("name") and role.get("version"):
                out.append(
                    {
                        "kind": "ansible_role",
                        "name": str(role["name"]),
                        "version": str(role["version"]),
                    }
                )
    for kind, digest_val in (resolution.get("runner_images") or {}).items():
        out.append({"kind": "runner_image", "name": str(kind), "version": str(digest_val)})
    # Dédupliquer en conservant l'ordre.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in out:
        key = (item["kind"], item["name"], item["version"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def lookup(conn: Conn, kind: str, name: str, version: str, *, matrix_version: str = MATRIX_VERSION) -> dict | None:
    return conn.execute(
        "SELECT * FROM compatibility_matrix WHERE matrix_version = %s AND component_kind = %s "
        "AND name = %s AND version = %s",
        (matrix_version, kind, name, version),
    ).fetchone()


def check(conn: Conn, resolution: dict[str, Any] | None) -> dict[str, Any]:
    """status global : ok | incompatible | unverified."""
    items_used = used_versions(conn, resolution)
    if not items_used:
        return {"status": "ok", "matrix_version": MATRIX_VERSION, "items": []}
    items: list[dict[str, Any]] = []
    status = "ok"
    for u in items_used:
        row = lookup(conn, u["kind"], u["name"], u["version"])
        if row is None:
            entry_status = "unverified"
            note = "combinaison absente de la matrice"
        else:
            entry_status = row["status"]
            note = row["note"] or ""
        items.append({**u, "status": entry_status, "note": note})
        if entry_status == "incompatible":
            status = "incompatible"
        elif entry_status == "unverified" and status != "incompatible":
            status = "unverified"
    return {"status": status, "matrix_version": MATRIX_VERSION, "items": items}


def unverified_policy() -> str:
    return str(policy.BUILTIN_RULES["platform"].get("compatibility_unverified_policy", "continue"))


def refusal(result: dict[str, Any]) -> tuple[str, str]:
    if result["status"] == "incompatible":
        bad = [i for i in result["items"] if i["status"] == "incompatible"]
        names = ", ".join(f"{i['name']}@{i['version']}" for i in bad)
        return "compatibility_incompatible", (
            f"combinaison de versions incompatible : {names}. Corrigez le catalogue ou la matrice avant le déploiement."
        )
    return "compatibility_unverified", (
        "combinaison de versions non vérifiée dans la matrice. "
        "La politique bloque le déploiement jusqu'à qualification."
    )


def ensure_seeded(conn: Conn) -> None:
    n = conn.execute(
        "SELECT count(*) AS n FROM compatibility_matrix WHERE matrix_version = %s", (MATRIX_VERSION,)
    ).fetchone()["n"]
    if n == 0:
        seed_defaults(conn)
