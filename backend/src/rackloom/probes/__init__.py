"""F18 : sondes déclaratives et objectifs de disponibilité.

Sondes sur cibles et protocoles autorisés, depuis le segment réseau prévu (F16).
Trois dimensions distinctes :
  - service_state : état OS / processus (actif, inactif, inconnu)
  - probe_health  : résultat de la sonde depuis le segment
  - freshness     : âge du dernier résultat vs max_probe_age de l'objectif

Un service installé et démarré est marqué `unavailable` si la sonde échoue
depuis le segment prévu (ex. segment filtrant). Aucune écriture fournisseur.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from .. import audit, events, registry
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_project
from ..ids import uuid7

PROTOCOLS = frozenset({"http", "https", "tcp"})
DEFAULT_SEGMENT = "lab-access"
FILTERED_SEGMENT = "filtered"
DEFAULT_MAX_AGE = 900

SERVICE_STATE_LABELS = {
    "active": "Actif",
    "inactive": "Inactif",
    "unknown": "Inconnu",
}
PROBE_HEALTH_LABELS = {
    "ok": "Sonde OK",
    "failed": "Sonde en échec",
    "error": "Erreur de sonde",
    "skipped": "Sonde non exécutée",
}
FRESHNESS_LABELS = {
    "fresh": "À jour",
    "stale": "Données anciennes",
    "missing": "Jamais sondé",
}
AVAILABILITY_LABELS = {
    "available": "Disponible",
    "unavailable": "Indisponible",
    "unknown": "Inconnu",
}

BUILTIN_SEGMENTS: tuple[dict[str, Any], ...] = (
    {
        "id": UUID("01940000-f18a-7000-8000-000000000001"),
        "slug": "lab-access",
        "name": "Segment d'accès labo",
        "zone_slug": "dev",
        "network_name": "rn-lab",
        "allowed_protocols": ["http", "https", "tcp"],
        "reaches_targets": True,
    },
    {
        "id": UUID("01940000-f18a-7000-8000-000000000002"),
        "slug": "filtered",
        "name": "Segment filtrant (isolation)",
        "zone_slug": "dev",
        "network_name": "rn-filtered",
        "allowed_protocols": ["http", "https", "tcp"],
        "reaches_targets": False,
    },
)


def ensure_segments(conn: Conn) -> list[dict[str, Any]]:
    """Garantit les segments builtin (idempotent)."""
    out: list[dict[str, Any]] = []
    for s in BUILTIN_SEGMENTS:
        row = conn.execute("SELECT * FROM probe_segments WHERE slug = %s", (s["slug"],)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO probe_segments "
                "(id, slug, name, zone_slug, network_name, allowed_protocols, reaches_targets, is_builtin, simulated) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, true, true) RETURNING *",
                (
                    s["id"],
                    s["slug"],
                    s["name"],
                    s["zone_slug"],
                    s["network_name"],
                    s["allowed_protocols"],
                    s["reaches_targets"],
                ),
            ).fetchone()
            assert row is not None
        out.append(row)
    return out


def segment_by_slug(conn: Conn, slug: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM probe_segments WHERE slug = %s", (slug,)).fetchone()


def list_segments(conn: Conn) -> list[dict[str, Any]]:
    ensure_segments(conn)
    return list(conn.execute("SELECT * FROM probe_segments ORDER BY slug").fetchall())


def probe_spec_from_checks(health_checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extrait la sonde déclarative (http/https/tcp) depuis les contrôles du template."""
    for check in health_checks or []:
        kind = check.get("type")
        if kind == "http_probe":
            protocol = str(check.get("protocol") or "http")
            if protocol not in PROTOCOLS:
                protocol = "http"
            return {
                "type": "http_probe",
                "protocol": protocol,
                "port": int(check.get("port") or 80),
                "path": str(check.get("path") or "/"),
                "expect_status": int(check.get("expect_status") or 200),
                "from_segment": str(check.get("from_segment") or DEFAULT_SEGMENT),
            }
        if kind in ("tcp_probe", "https_probe"):
            protocol = "tcp" if kind == "tcp_probe" else "https"
            return {
                "type": kind,
                "protocol": protocol,
                "port": int(check.get("port") or (443 if protocol == "https" else 80)),
                "path": str(check.get("path") or "/"),
                "expect_status": int(check.get("expect_status") or 200),
                "from_segment": str(check.get("from_segment") or DEFAULT_SEGMENT),
            }
    return None


def service_state_from_checks(checks: list[dict[str, Any]]) -> str:
    """État OS déduit des contrôles service_active (indépendant de la sonde)."""
    actives = [c for c in checks if c.get("type") == "service_active"]
    if not actives:
        return "unknown"
    if all(c.get("ok") for c in actives):
        return "active"
    return "inactive"


def freshness_of(checked_at: datetime | None, max_age_seconds: int, *, now: datetime | None = None) -> str:
    if checked_at is None:
        return "missing"
    now = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    if now - checked_at > timedelta(seconds=max_age_seconds):
        return "stale"
    return "fresh"


def synthesize_availability(*, service_state: str, probe_health: str) -> str:
    """Disponibilité : service actif + sonde OK. La fraîcheur reste une dimension séparée."""
    if service_state == "unknown" and probe_health in ("skipped",):
        return "unknown"
    if probe_health == "ok" and service_state in ("active", "unknown"):
        # unknown service + probe ok (pas de service_active déclaré) → available
        return "available"
    if probe_health in ("failed", "error"):
        return "unavailable"
    if service_state == "inactive":
        return "unavailable"
    return "unknown"


def evaluate_dimensions(
    *,
    service_state: str,
    probe_health: str,
    checked_at: datetime | None,
    max_age_seconds: int = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    fresh = freshness_of(checked_at, max_age_seconds, now=now)
    availability = synthesize_availability(service_state=service_state, probe_health=probe_health)
    return {
        "service_state": service_state,
        "service_state_label": SERVICE_STATE_LABELS[service_state],
        "probe_health": probe_health,
        "probe_health_label": PROBE_HEALTH_LABELS[probe_health],
        "freshness": fresh,
        "freshness_label": FRESHNESS_LABELS[fresh],
        "availability": availability,
        "availability_label": AVAILABILITY_LABELS[availability],
        "checked_at": checked_at.isoformat() if checked_at else None,
        "max_probe_age_seconds": max_age_seconds,
    }


def deployment_service_status_from_probe(
    *, service_state: str, probe_health: str, other_checks_ok: bool
) -> tuple[str, str]:
    """Mappe les dimensions sonde → service_status déploiement.

    Recette F18 : service actif + sonde en échec depuis le segment → `unavailable`.
    """
    if probe_health in ("failed", "error") and service_state == "active":
        return "unavailable", "service actif, sonde en échec depuis le segment prévu"
    if probe_health in ("failed", "error"):
        return "created_not_ready", "sonde en échec depuis le segment prévu"
    if service_state == "inactive":
        return "created_not_ready", "service inactif"
    if not other_checks_ok:
        return "created_not_ready", "contrôles de santé incomplets"
    if probe_health == "ok":
        return "ready", "tous les contrôles de santé ont réussi"
    return "unknown", "sonde non exécutée"


def create_objective(
    conn: Conn,
    *,
    project_id: UUID,
    name: str,
    probe_segment_slug: str,
    target_pct: float = 99.0,
    max_probe_age_seconds: int = DEFAULT_MAX_AGE,
    protocols: list[str] | None = None,
    environment_id: UUID | None = None,
    deployment_id: UUID | None = None,
    created_by: UUID | None = None,
) -> dict[str, Any]:
    ensure_segments(conn)
    segment = segment_by_slug(conn, probe_segment_slug)
    if segment is None:
        raise ApiError(422, "unknown_probe_segment", f"segment de sonde inconnu : {probe_segment_slug}")
    protos = list(protocols or ["http"])
    if not protos or any(p not in PROTOCOLS for p in protos):
        raise ApiError(422, "invalid_protocol", "protocole non autorisé")
    allowed = set(segment["allowed_protocols"] or [])
    if not set(protos) <= allowed:
        raise ApiError(422, "protocol_not_allowed_on_segment", "protocole non autorisé sur ce segment")
    row = conn.execute(
        "INSERT INTO availability_objectives "
        "(id, project_id, environment_id, deployment_id, name, target_pct, max_probe_age_seconds, "
        "probe_segment_slug, protocols, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            uuid7(),
            project_id,
            environment_id,
            deployment_id,
            name,
            target_pct,
            max_probe_age_seconds,
            probe_segment_slug,
            protos,
            created_by,
        ),
    ).fetchone()
    assert row is not None
    return row


def objective_for_deployment(conn: Conn, dep: dict[str, Any]) -> dict[str, Any] | None:
    """Objectif le plus spécifique : déploiement > environnement > projet."""
    row = conn.execute(
        "SELECT * FROM availability_objectives WHERE deployment_id = %s AND active ORDER BY created_at DESC LIMIT 1",
        (dep["id"],),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT * FROM availability_objectives WHERE project_id = %s AND environment_id = %s "
        "AND deployment_id IS NULL AND active ORDER BY created_at DESC LIMIT 1",
        (dep["project_id"], dep["environment_id"]),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM availability_objectives WHERE project_id = %s AND environment_id IS NULL "
        "AND deployment_id IS NULL AND active ORDER BY created_at DESC LIMIT 1",
        (dep["project_id"],),
    ).fetchone()


def record_result(
    conn: Conn,
    *,
    project_id: UUID,
    deployment_id: UUID,
    host_id: UUID,
    protocol: str,
    from_segment: str,
    service_state: str,
    probe_health: str,
    evidence: dict[str, Any],
    objective_id: UUID | None = None,
    job_id: UUID | None = None,
    simulated_filter: bool = False,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    availability = synthesize_availability(service_state=service_state, probe_health=probe_health)
    at = checked_at or datetime.now(UTC)
    row = conn.execute(
        "INSERT INTO probe_results "
        "(id, project_id, deployment_id, host_id, objective_id, protocol, from_segment, "
        "service_state, probe_health, checked_at, availability, evidence, job_id, simulated_filter) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            uuid7(),
            project_id,
            deployment_id,
            host_id,
            objective_id,
            protocol,
            from_segment,
            service_state,
            probe_health,
            at,
            availability,
            Jsonb(evidence),
            job_id,
            simulated_filter,
        ),
    ).fetchone()
    assert row is not None
    return row


def latest_results(conn: Conn, deployment_id: UUID) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT DISTINCT ON (host_id) * FROM probe_results "
            "WHERE deployment_id = %s ORDER BY host_id, checked_at DESC",
            (deployment_id,),
        ).fetchall()
    )


def apply_segment_filter(
    *,
    segment: dict[str, Any],
    protocol: str,
    raw_probe: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], bool]:
    """Applique le filtrage de segment. Segment filtrant → échec même si la cible répondrait."""
    allowed = set(segment.get("allowed_protocols") or [])
    if protocol not in allowed:
        return (
            "error",
            {"error": "protocol_not_allowed_on_segment", "protocol": protocol, "segment": segment["slug"]},
            False,
        )
    if not segment.get("reaches_targets", True):
        return (
            "failed",
            {
                "error": "segment_filtered",
                "segment": segment["slug"],
                "message": "segment filtrant : la sonde n'atteint pas la cible",
                "ok": False,
                "status": None,
            },
            True,
        )
    if raw_probe is None:
        return "error", {"error": "probe_missing", "ok": False}, False
    if raw_probe.get("ok"):
        return "ok", dict(raw_probe), False
    return "failed", dict(raw_probe), False


def record_from_verify(
    conn: Conn,
    *,
    dep: dict[str, Any],
    host: dict[str, Any],
    checks: list[dict[str, Any]],
    raw_probe: dict[str, Any] | None,
    health_checks: list[dict[str, Any]],
    job_id: UUID | None,
) -> dict[str, Any] | None:
    """Enregistre le résultat de sonde issu d'un job verify (segment + dimensions)."""
    ensure_segments(conn)
    spec = probe_spec_from_checks(health_checks)
    if spec is None:
        return None
    objective = objective_for_deployment(conn, dep)
    segment_slug = (objective["probe_segment_slug"] if objective else None) or spec["from_segment"] or DEFAULT_SEGMENT
    segment = segment_by_slug(conn, segment_slug) or segment_by_slug(conn, DEFAULT_SEGMENT)
    assert segment is not None
    protocol = spec["protocol"]
    if objective and protocol not in (objective.get("protocols") or [protocol]):
        protocol = (objective["protocols"] or ["http"])[0]
    probe_health, evidence, simulated_filter = apply_segment_filter(
        segment=segment, protocol=protocol, raw_probe=raw_probe
    )
    service_state = service_state_from_checks(checks)
    evidence = {
        **evidence,
        "from_segment": segment["slug"],
        "protocol": protocol,
        "service_state": service_state,
        "reaches_targets": segment["reaches_targets"],
    }
    return record_result(
        conn,
        project_id=dep["project_id"],
        deployment_id=dep["id"],
        host_id=host["id"],
        objective_id=objective["id"] if objective else None,
        protocol=protocol,
        from_segment=segment["slug"],
        service_state=service_state,
        probe_health=probe_health,
        evidence=evidence,
        job_id=job_id,
        simulated_filter=simulated_filter,
    )


def run_check(
    conn: Conn,
    *,
    dep: dict[str, Any],
    actor_type: str = "user",
    actor_id: str | None = None,
    force_segment: str | None = None,
    force_probe_ok: bool | None = None,
    service_state_override: str | None = None,
) -> dict[str, Any]:
    """Exécute une évaluation de sonde synchrone (labo / tests) sans écriture fournisseur.

    En labo sans runner : `force_probe_ok` / segment filtrant pilotent le résultat.
    Les observations `services` existantes fournissent l'état OS si disponibles.
    """
    from .. import hosts as hosts_mod

    ensure_segments(conn)
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    contract = tpl["contract"]
    health_checks = list(contract.get("health_checks") or [])
    spec = probe_spec_from_checks(health_checks) or {
        "protocol": "http",
        "port": 80,
        "path": "/",
        "expect_status": 200,
        "from_segment": DEFAULT_SEGMENT,
        "type": "http_probe",
    }
    objective = objective_for_deployment(conn, dep)
    segment_slug = force_segment or (
        objective["probe_segment_slug"] if objective else spec.get("from_segment") or DEFAULT_SEGMENT
    )
    segment = segment_by_slug(conn, segment_slug)
    if segment is None:
        raise ApiError(422, "unknown_probe_segment", f"segment inconnu : {segment_slug}")
    protocol = spec["protocol"]
    allowed = set(segment["allowed_protocols"] or [])
    if protocol not in allowed:
        raise ApiError(422, "protocol_not_allowed_on_segment", "protocole non autorisé sur ce segment")

    host_rows = list(
        conn.execute("SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (dep["id"],)).fetchall()
    )
    if not host_rows:
        raise ApiError(422, "no_hosts", "aucune machine pour ce déploiement")

    results: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for host in host_rows:
        obs = hosts_mod.latest_observations(conn, host["id"])
        # État service depuis observations ou override de test.
        if service_state_override:
            service_state = service_state_override
        elif "services" in obs:
            data = obs["services"]["data"] or {}
            # Collecte Ansible : processes map ou flags.
            processes = data.get("processes") or data
            # Si au moins un processus suivi est vrai → actif.
            flags = [v for k, v in processes.items() if isinstance(v, bool)]
            if flags:
                service_state = "active" if any(flags) else "inactive"
            else:
                service_state = "active" if obs["services"]["status"] == "ok" else "unknown"
        else:
            # Sans observation services : on ne ment pas — unknown, sauf override.
            service_state = "unknown"

        if force_probe_ok is True:
            raw: dict[str, Any] | None = {"ok": True, "status": 200, "host": host["name"]}
        elif force_probe_ok is False:
            raw = {"ok": False, "status": None, "error": "forced_failure", "host": host["name"]}
        elif "probe" in obs and obs["probe"]["status"] == "ok":
            raw = dict(obs["probe"]["data"] or {"ok": True})
        elif "probe" in obs:
            raw = dict(obs["probe"]["data"] or {"ok": False})
        else:
            raw = {"ok": True, "status": 200, "host": host["name"]} if segment["reaches_targets"] else None

        probe_health, evidence, simulated_filter = apply_segment_filter(
            segment=segment, protocol=protocol, raw_probe=raw
        )
        evidence = {
            **evidence,
            "from_segment": segment["slug"],
            "protocol": protocol,
            "service_state": service_state,
            "host": host["name"],
        }
        row = record_result(
            conn,
            project_id=dep["project_id"],
            deployment_id=dep["id"],
            host_id=host["id"],
            objective_id=objective["id"] if objective else None,
            protocol=protocol,
            from_segment=segment["slug"],
            service_state=service_state,
            probe_health=probe_health,
            evidence=evidence,
            simulated_filter=simulated_filter,
            checked_at=now,
        )
        # Observation sonde pour la fiche machine.
        hosts_mod.observe(
            conn,
            host,
            "probe",
            evidence,
            "ok" if probe_health == "ok" else "error",
            f"probe:{segment['slug']}",
            None,
        )
        dims = evaluate_dimensions(
            service_state=service_state,
            probe_health=probe_health,
            checked_at=now,
            max_age_seconds=int(objective["max_probe_age_seconds"]) if objective else DEFAULT_MAX_AGE,
            now=now,
        )
        results.append({"host": host["name"], "host_id": str(host["id"]), "result_id": str(row["id"]), **dims})

    # Met à jour service_status du déploiement selon la règle F18.
    worst = results[0]
    for r in results[1:]:
        if r["availability"] == "unavailable" or (
            r["availability"] != "available" and worst["availability"] == "available"
        ):
            worst = r
    other_ok = True
    status, reason = deployment_service_status_from_probe(
        service_state=worst["service_state"],
        probe_health=worst["probe_health"],
        other_checks_ok=other_ok,
    )
    # Si tous disponibles → ready ; si unavailable → unavailable.
    if all(r["availability"] == "available" for r in results):
        status, reason = "ready", "tous les contrôles de santé ont réussi"
    elif any(r["availability"] == "unavailable" for r in results):
        # Priorité à la recette : actif + sonde KO → unavailable
        actives_failing = [
            r for r in results if r["service_state"] == "active" and r["probe_health"] in ("failed", "error")
        ]
        if actives_failing:
            status, reason = "unavailable", "service actif, sonde en échec depuis le segment prévu"
        else:
            status, reason = deployment_service_status_from_probe(
                service_state=worst["service_state"],
                probe_health=worst["probe_health"],
                other_checks_ok=True,
            )

    conn.execute(
        "UPDATE deployments SET service_status = %s, service_reason = %s, service_checked_at = %s WHERE id = %s",
        (status, reason, now, dep["id"]),
    )
    events.emit(
        conn,
        type="deployment.probe",
        source="verification",
        project_id=dep["project_id"],
        environment_id=dep["environment_id"],
        data={
            "deployment_id": str(dep["id"]),
            "service_status": status,
            "segment": segment["slug"],
            "hosts": len(results),
            "message": reason,
        },
        severity="info" if status == "ready" else "warning",
        dedupe_key=f"probe:{dep['id']}:{now.isoformat()}",
    )
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="probe.run",
        outcome="succeeded" if status == "ready" else "failed",
        project_id=dep["project_id"],
        targets={"deployment_id": str(dep["id"]), "segment": segment["slug"], "hosts": len(results)},
    )
    return {
        "deployment_id": str(dep["id"]),
        "service_status": status,
        "service_reason": reason,
        "segment": {
            "slug": segment["slug"],
            "name": segment["name"],
            "reaches_targets": segment["reaches_targets"],
            "simulated": segment["simulated"],
        },
        "hosts": results,
        "checked_at": now.isoformat(),
    }


def availability_report(
    conn: Conn,
    principal: Principal,
    deployment_id: UUID,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rapport de disponibilité : trois dimensions + objectif, périmètre projet."""
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found()
    require_project(principal, dep["project_id"])
    now = now or datetime.now(UTC)
    objective = objective_for_deployment(conn, dep)
    max_age = int(objective["max_probe_age_seconds"]) if objective else DEFAULT_MAX_AGE
    rows = latest_results(conn, dep["id"])
    hosts_out: list[dict[str, Any]] = []
    for row in rows:
        dims = evaluate_dimensions(
            service_state=row["service_state"],
            probe_health=row["probe_health"],
            checked_at=row["checked_at"],
            max_age_seconds=max_age,
            now=now,
        )
        host = conn.execute("SELECT name FROM hosts WHERE id = %s", (row["host_id"],)).fetchone()
        hosts_out.append(
            {
                "host_id": str(row["host_id"]),
                "host": host["name"] if host else None,
                "protocol": row["protocol"],
                "from_segment": row["from_segment"],
                "simulated_filter": row["simulated_filter"],
                "evidence": row["evidence"],
                **dims,
            }
        )

    # Synthèse déploiement.
    if not hosts_out:
        summary_dims = evaluate_dimensions(
            service_state="unknown",
            probe_health="skipped",
            checked_at=dep.get("service_checked_at"),
            max_age_seconds=max_age,
            now=now,
        )
    else:
        # Fraîcheur globale = pire ; disponibilité = unavailable si un hôte l'est.
        fresh_rank = {"missing": 2, "stale": 1, "fresh": 0}
        worst_fresh = max(hosts_out, key=lambda h: fresh_rank[h["freshness"]])
        if any(h["availability"] == "unavailable" for h in hosts_out):
            avail = "unavailable"
        elif all(h["availability"] == "available" for h in hosts_out):
            avail = "available"
        else:
            avail = "unknown"
        # service_state / probe_health : premier unavailable, sinon premier.
        focus = next((h for h in hosts_out if h["availability"] == "unavailable"), hosts_out[0])
        summary_dims = {
            "service_state": focus["service_state"],
            "service_state_label": focus["service_state_label"],
            "probe_health": focus["probe_health"],
            "probe_health_label": focus["probe_health_label"],
            "freshness": worst_fresh["freshness"],
            "freshness_label": worst_fresh["freshness_label"],
            "availability": avail,
            "availability_label": AVAILABILITY_LABELS[avail],
            "checked_at": focus["checked_at"],
            "max_probe_age_seconds": max_age,
        }

    return {
        "deployment_id": str(dep["id"]),
        "service_status": dep["service_status"],
        "service_status_label": {
            "unknown": "Non vérifié",
            "ready": "Prêt",
            "created_not_ready": "Créé, pas prêt",
            "failed": "Échec",
            "verification_unavailable": "Vérification indisponible",
            "unavailable": "Indisponible",
        }.get(dep["service_status"], dep["service_status"]),
        "service_reason": dep.get("service_reason"),
        "dimensions": summary_dims,
        "hosts": hosts_out,
        "objective": (
            {
                "id": str(objective["id"]),
                "name": objective["name"],
                "target_pct": float(objective["target_pct"]),
                "max_probe_age_seconds": int(objective["max_probe_age_seconds"]),
                "probe_segment_slug": objective["probe_segment_slug"],
                "protocols": list(objective["protocols"] or []),
            }
            if objective
            else None
        ),
        "notice": (
            "État du service, santé de la sonde et fraîcheur sont distincts. "
            "Un service actif reste indisponible si la sonde échoue depuis le segment prévu."
        ),
    }


def segments_view(conn: Conn) -> list[dict[str, Any]]:
    return [
        {
            "slug": s["slug"],
            "name": s["name"],
            "zone_slug": s["zone_slug"],
            "network_name": s["network_name"],
            "allowed_protocols": list(s["allowed_protocols"] or []),
            "reaches_targets": s["reaches_targets"],
            "is_builtin": s["is_builtin"],
            "simulated": s["simulated"],
        }
        for s in list_segments(conn)
    ]
