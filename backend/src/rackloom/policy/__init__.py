"""Bundles de politiques versionnés, intégrés au code (`builtin@N`).

Le bundle décrit les règles d'admission (registre ordonné) et les contraintes de plateforme
appliquées par le résolveur. Sa version et son empreinte sont enregistrées dans chaque
décision d'admission, chaque résolution et chaque dossier d'exécution : un plan approuvé sous
une version antérieure est refusé à l'admission (`policy_changed`) et doit être réapprouvé.

Historique : `builtin@1` (lot 1), `builtin@2` (F28 : R080), `builtin@3` (F27 : R090 ;
R065 AT01), `builtin@4` (F37 : R095), `builtin@5` (F05 : R110), `builtin@6` (F03 : R120 IPAM).
"""

from __future__ import annotations

from typing import Any

from ..canonical import digest
from ..db import Conn, Jsonb

BUILTIN_VERSION = "builtin@6"

BUILTIN_RULES: dict[str, Any] = {
    "admission": [
        {"id": "R010", "order": 10, "class": "all", "title": "connecteur simulé refusé en production"},
        {"id": "R020", "order": 20, "class": "all", "title": "droits courants du demandeur"},
        {"id": "R030", "order": 30, "class": "write", "title": "droits courants de l'approbateur, séparation"},
        {"id": "R040", "order": 40, "class": "write", "title": "approbation valide, non révoquée, non consommée"},
        {"id": "R050", "order": 50, "class": "write", "title": "empreinte du dossier recalculée identique"},
        {"id": "R060", "order": 60, "class": "write", "title": "plan non expiré, sans suppression ni remplacement"},
        {"id": "R065", "order": 65, "class": "write",
         "title": "configuration limitée aux cibles de la liste approuvée"},
        {"id": "R070", "order": 70, "class": "all", "title": "annulation non demandée"},
        {"id": "R080", "order": 80, "class": "write", "stages": ["start", "dispatch"],
         "title": "environnement non gelé, au démarrage et à l'envoi au fournisseur"},
        {"id": "R090", "order": 90, "class": "write", "stages": ["start", "dispatch"],
         "title": "dans une fenêtre de maintenance, hors gel calendaire"},
        {"id": "R095", "order": 95, "class": "write", "stages": ["start", "dispatch"],
         "title": "disjoncteur du connecteur fermé"},
        {"id": "R110", "order": 110, "class": "write", "stages": ["start", "dispatch"],
         "title": "capacité encore suffisante sur le placement (mesure, pas réservation)"},
        {"id": "R120", "order": 120, "class": "write", "stages": ["start", "dispatch"],
         "title": "réservations IPAM/DNS confirmées ou réservées pour les cibles"},
        {"id": "R900", "order": 900, "class": "write", "title": "verrou de stack acquis"},
    ],
    # Emplacements réservés au backlog, documentés mais non encore enregistrés.
    "reserved": {"100": "F11", "115": "F39", "125": "F34",
                 "130": "F16", "135": "F38", "140": "F10"},
    "platform": {
        "production_requires_explicit_environment": True,
        "production_requires_distinct_approver": True,
        "never_default_to_production": True,
        "plan_expiry_margin_seconds": 60,
        "inventory_max_age_hours": 24,
        "capacity_max_age_hours": 24,
        # Marge minimale restante après allocation, en points de base (500 = 5 %).
        "capacity_min_free_bps": 500,
        "destructive_actions_blocked": True,
        "connector_circuit_failure_threshold": 3,
    },
}


def builtin_digest() -> str:
    return digest(BUILTIN_RULES)


def ensure_builtin(conn: Conn) -> dict[str, str]:
    d = builtin_digest()
    conn.execute(
        "INSERT INTO policy_bundles (version, digest, source, rules) VALUES (%s, %s, 'builtin', %s) "
        "ON CONFLICT (version) DO NOTHING",
        (BUILTIN_VERSION, d, Jsonb(BUILTIN_RULES)),
    )
    row = conn.execute("SELECT digest FROM policy_bundles WHERE version = %s", (BUILTIN_VERSION,)).fetchone()
    if row["digest"] != d:
        raise RuntimeError(f"bundle {BUILTIN_VERSION} modifié sans changement de version")
    return {"version": BUILTIN_VERSION, "digest": d}


def active() -> dict[str, str]:
    return {"version": BUILTIN_VERSION, "digest": builtin_digest()}
