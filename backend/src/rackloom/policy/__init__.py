"""Bundles de politiques versionnés. Lot 1 : un seul bundle intégré `builtin@1`.

Le bundle décrit les règles d'admission (registre ordonné) et les contraintes de plateforme
appliquées par le résolveur. Sa version et son empreinte sont enregistrées dans chaque
décision d'admission, chaque résolution et chaque dossier d'exécution.
"""

from __future__ import annotations

from typing import Any

import psycopg

from ..canonical import digest
from ..db import Jsonb

BUILTIN_VERSION = "builtin@1"

BUILTIN_RULES: dict[str, Any] = {
    "admission": [
        {"id": "R010", "order": 10, "class": "all", "title": "connecteur simulé refusé en production"},
        {"id": "R020", "order": 20, "class": "all", "title": "droits courants du demandeur"},
        {"id": "R030", "order": 30, "class": "write", "title": "droits courants de l'approbateur, séparation"},
        {"id": "R040", "order": 40, "class": "write", "title": "approbation valide, non révoquée, non consommée"},
        {"id": "R050", "order": 50, "class": "write", "title": "empreinte du dossier recalculée identique"},
        {"id": "R060", "order": 60, "class": "write", "title": "plan non expiré, sans suppression ni remplacement"},
        {"id": "R070", "order": 70, "class": "all", "title": "annulation non demandée"},
        {"id": "R900", "order": 900, "class": "write", "title": "verrou de stack acquis"},
    ],
    # Emplacements réservés au backlog, documentés mais non enregistrés au lot 1.
    "reserved": {"80": "F28", "90": "F27", "100": "F11", "110": "F05", "120": "F03", "130": "F16",
                 "140": "F10"},
    "platform": {
        "production_requires_explicit_environment": True,
        "production_requires_distinct_approver": True,
        "never_default_to_production": True,
        "plan_expiry_margin_seconds": 60,
        "inventory_max_age_hours": 24,
        "destructive_actions_blocked": True,
    },
}


def builtin_digest() -> str:
    return digest(BUILTIN_RULES)


def ensure_builtin(conn: psycopg.Connection) -> dict[str, str]:
    d = builtin_digest()
    conn.execute(
        "INSERT INTO policy_bundles (version, digest, source, rules) VALUES (%s, %s, 'builtin', %s) "
        "ON CONFLICT (version) DO NOTHING",
        (BUILTIN_VERSION, d, Jsonb(BUILTIN_RULES)),
    )
    row = conn.execute("SELECT digest FROM policy_bundles WHERE version = %s", (BUILTIN_VERSION,)).fetchone()
    if row["digest"] != d:
        raise RuntimeError("bundle builtin@1 modifié sans changement de version")
    return {"version": BUILTIN_VERSION, "digest": d}


def active() -> dict[str, str]:
    return {"version": BUILTIN_VERSION, "digest": builtin_digest()}
