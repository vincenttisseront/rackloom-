"""F56 : estimation de mise à disposition.

Distingue attente d'approbation, prochain créneau autorisé et durée d'exécution.
Fourchettes fondées sur des exécutions comparables ; jamais inventées par un modèle (U10).
File d'attente (F36) : non disponible tant que F36 n'est pas livré.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from ..db import Conn
from ..identity.authz import Principal
from ..projects import calendars

# Seuil minimal d'historique pour publier une fourchette (recette F56).
MIN_SAMPLES = 5

# Jobs qui composent une livraison typique après approbation.
PIPELINE_KINDS = ("preflight", "tf_plan", "tf_apply", "ansible_configure", "verify")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _approval_block(conn: Conn, change: dict[str, Any]) -> dict[str, Any]:
    state = change["state"]
    if state in (
        "approved",
        "blocked",
        "executing",
        "verifying",
        "succeeded",
        "partially_succeeded",
        "failed",
        "uncertain",
        "reconciling",
    ):
        return {
            "status": "done",
            "guaranteed_delivery_at": None,
            "note": "Plan approuvé ; pas de date de livraison garantie.",
        }
    if state == "plan_ready":
        return {
            "status": "pending",
            "guaranteed_delivery_at": None,
            "note": "Pas de date de livraison tant que le plan n'est pas approuvé.",
        }
    if state in (
        "draft",
        "needs_info",
        "resolution_blocked",
        "validated",
        "planning",
        "plan_failed",
        "plan_expired",
        "rejected",
    ):
        return {
            "status": "not_ready",
            "guaranteed_delivery_at": None,
            "note": "Pas de date de livraison : le plan n'est pas encore prêt à approuver.",
        }
    return {
        "status": "n/a",
        "guaranteed_delivery_at": None,
        "note": "État terminal ou non applicable.",
    }


def _schedule_block(conn: Conn, change: dict[str, Any]) -> dict[str, Any]:
    env_id = change.get("environment_id")
    if env_id is None:
        return {
            "run_after": None,
            "next_window": None,
            "timezone": None,
            "code": None,
            "note": "Aucun environnement résolu.",
        }
    state = calendars.writable(conn, env_id)
    job = conn.execute(
        "SELECT run_after FROM jobs WHERE change_id = %s AND run_after IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (change["id"],),
    ).fetchone()
    run_after = job["run_after"] if job else None
    next_window = state.get("next_open")
    if run_after is None and state.get("ok") is False:
        run_after = next_window
    return {
        "run_after": _iso(run_after),
        "next_window": _iso(next_window),
        "timezone": state.get("timezone"),
        "code": None if state.get("ok") else state.get("code"),
        "note": None
        if state.get("ok")
        else "Hors créneau ou calendrier figé ; le prochain créneau est indiqué s'il est connu.",
    }


def _execution_samples(conn: Conn, change: dict[str, Any]) -> list[float]:
    """Durées totales (secondes) de pipelines comparables réussis (même template + env)."""
    tpl = None
    version = conn.execute(
        "SELECT need FROM request_versions WHERE change_id = %s ORDER BY version_no DESC LIMIT 1",
        (change["id"],),
    ).fetchone()
    if version and version["need"]:
        tpl = (version["need"].get("service") or {}).get("template")
    env_id = change.get("environment_id")
    if not tpl or env_id is None:
        return []

    # Changements réussis du même projet/template/environnement.
    peers = conn.execute(
        """
        SELECT c.id FROM changes c
        JOIN request_versions v ON v.change_id = c.id
          AND v.version_no = (SELECT max(version_no) FROM request_versions WHERE change_id = c.id)
        WHERE c.project_id = %s AND c.environment_id = %s AND c.state = 'succeeded'
          AND v.need -> 'service' ->> 'template' = %s
          AND c.id <> %s
        ORDER BY c.updated_at DESC
        LIMIT 50
        """,
        (change["project_id"], env_id, tpl, change["id"]),
    ).fetchall()

    durations: list[float] = []
    for peer in peers:
        jobs = conn.execute(
            "SELECT kind, created_at, finished_at FROM jobs "
            "WHERE change_id = %s AND state = 'succeeded' AND finished_at IS NOT NULL "
            "AND kind = ANY(%s)",
            (peer["id"], list(PIPELINE_KINDS)),
        ).fetchall()
        if not jobs:
            continue
        start = min(j["created_at"] for j in jobs)
        end = max(j["finished_at"] for j in jobs)
        seconds = (end - start).total_seconds()
        if seconds > 0:
            durations.append(seconds)
    return durations


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _execution_block(conn: Conn, change: dict[str, Any]) -> dict[str, Any]:
    samples = _execution_samples(conn, change)
    n = len(samples)
    if n < MIN_SAMPLES:
        return {
            "status": "insufficient_history",
            "label": "délai non estimable",
            "sample_size": n,
            "minimum_required": MIN_SAMPLES,
            "range": None,
            "note": "Historique insuffisant d'exécutions comparables.",
        }
    ordered = sorted(samples)
    return {
        "status": "estimated",
        "label": "fourchette d'exécution",
        "sample_size": n,
        "minimum_required": MIN_SAMPLES,
        "range": {
            "p50_seconds": int(round(median(ordered))),
            "p90_seconds": int(round(_percentile(ordered, 0.9))),
            "based_on_job_kinds": list(PIPELINE_KINDS),
        },
        "note": "Fondée sur des exécutions réussies comparables ; ce n'est pas une garantie.",
    }


def estimate(conn: Conn, principal: Principal, change: dict[str, Any]) -> dict[str, Any]:
    """Estimation structurée ; aucun appel au modèle."""
    # principal déjà scopé via get_change.
    _ = principal
    known_blockers: list[str] = []
    if change["state"] == "plan_ready":
        known_blockers.append("approbation en attente")
    if change["state"] == "blocked":
        known_blockers.append("bloqué à l'admission")
    if change["state"] in ("needs_info", "resolution_blocked"):
        known_blockers.append("besoin incomplet ou résolution impossible")

    return {
        "approval": _approval_block(conn, change),
        "schedule": _schedule_block(conn, change),
        "execution": _execution_block(conn, change),
        "queue": {
            "status": "unavailable",
            "reason": "file équitable (F36) non livrée",
        },
        "known_blockers": known_blockers,
        "model_used": False,
        "notice": (
            "Attente d'approbation, créneau et durée d'exécution sont distincts. "
            "Aucune estimation n'est demandée à un modèle."
        ),
    }
