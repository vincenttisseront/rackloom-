"""F07 : constructeur d'images fictif (outil réel ouvert — D11).

Produit une empreinte déterministe et une liste de tests labo. Aucun appel réseau,
aucune ressource vSphere. Les échecs injectés (`fail_tests=True`) servent la recette
« promotion prod refusée après échec de test ».
"""

from __future__ import annotations

import hashlib
from typing import Any

DEFAULT_TESTS = (
    "layout_matches",
    "guest_tools_present",
    "cloud_init_ready",
    "no_default_credentials",
)


def build_digest(*, slug: str, version: str, source_digest: str, recipe_digest: str, os: str, layout_id: str) -> str:
    material = f"{slug}|{version}|{source_digest}|{recipe_digest}|{os}|{layout_id}|fake-builder".encode()
    return hashlib.sha256(material).hexdigest()


def stable_id_for(*, slug: str, version: str) -> str:
    digest = hashlib.sha256(f"image-build:{slug}:{version}".encode()).hexdigest()[:12]
    return f"img-factory-{digest}"


def run_tests(*, fail: bool = False) -> list[dict[str, Any]]:
    """Exécute la batterie locale. Si `fail`, le premier test échoue ; les autres passent."""
    out: list[dict[str, Any]] = []
    for i, name in enumerate(DEFAULT_TESTS):
        if fail and i == 0:
            out.append(
                {
                    "name": name,
                    "outcome": "failed",
                    "detail": "échec injecté (fabrique fictive) : layout non conforme",
                    "simulated": True,
                }
            )
        else:
            out.append(
                {
                    "name": name,
                    "outcome": "passed",
                    "detail": "ok (fabrique fictive)",
                    "simulated": True,
                }
            )
    return out
