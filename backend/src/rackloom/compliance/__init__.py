"""F24 : conformité des machines.

Compare l'état observé à des profils versionnés (paquets, services, réglages
sélectionnés). Distingue non-conformité, non-applicabilité et absence de données.
Un écart produit une proposition de correction (brouillon de demande) — aucune
écriture fournisseur sans approbation.

Connecteur Rudder optionnel (RAC-36) : lecture seule ou observations fictives
explicites tant qu'aucun laboratoire Rudder n'est autorisé (D2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from .. import audit, events, hosts
from ..changes import service as changes
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_role
from ..ids import uuid7

FINDING_STATUSES = ("compliant", "noncompliant", "not_applicable", "missing_data")
SCAN_STATUSES = ("ok", "noncompliant", "partial", "no_data")
CATEGORIES = ("package", "service", "setting")


def list_profiles(conn: Conn, *, published_only: bool = True) -> list[dict[str, Any]]:
    q = "SELECT * FROM compliance_profiles"
    if published_only:
        q += " WHERE status = 'published'"
    q += " ORDER BY slug, version"
    return [_profile_view(r) for r in conn.execute(q).fetchall()]


def get_profile(conn: Conn, slug: str, version: str | None = None) -> dict[str, Any]:
    if version:
        row = conn.execute(
            "SELECT * FROM compliance_profiles WHERE slug = %s AND version = %s",
            (slug, version),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM compliance_profiles WHERE slug = %s AND status = 'published'",
            (slug,),
        ).fetchone()
    if row is None:
        raise not_found("profil de conformité")
    return row


def _profile_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "status": row["status"],
        "rules": row["rules"] or {},
        "note": row["note"],
        "published_at": row["published_at"].isoformat() if row.get("published_at") else None,
    }


def _host_visible(conn: Conn, principal: Principal, host_id: UUID) -> dict[str, Any]:
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    if host is None or not principal.can_see_project(host["project_id"]):
        raise not_found("machine")
    return host


def _package_present(packages: dict[str, Any] | None, name: str) -> bool | None:
    if packages is None:
        return None
    if name not in packages:
        return False
    val = packages[name]
    if val is None or val is False or val == "":
        return False
    return True


def _service_active(services: dict[str, Any] | None, unit: str) -> bool | None:
    if services is None:
        return None
    if unit not in services:
        return False
    entry = services[unit]
    if isinstance(entry, dict):
        return bool(entry.get("active"))
    return bool(entry)


def _setting_value(settings: dict[str, Any] | None, key: str) -> str | None:
    if settings is None:
        return None
    if key not in settings:
        return None
    val = settings[key]
    if val is None:
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _when_applies(when: dict[str, Any] | None, packages: dict[str, Any] | None) -> str:
    """Retourne 'yes', 'no' (non applicable) ou 'unknown' (données manquantes)."""
    if not when:
        return "yes"
    pkg = when.get("package")
    if pkg:
        present = _package_present(packages, str(pkg))
        if present is None:
            return "unknown"
        return "yes" if present else "no"
    return "yes"


def evaluate_host(
    conn: Conn,
    host: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Évalue un hôte contre un profil : liste de findings (pas encore persistés)."""
    obs = hosts.latest_observations(conn, host["id"])
    packages_row = obs.get("packages")
    services_row = obs.get("services")
    settings_row = obs.get("settings")
    packages = (
        packages_row["data"]
        if packages_row and packages_row["status"] == "ok"
        else (packages_row["data"] if packages_row else None)
    )
    # Absence totale du kind → missing_data ; présence avec status error → missing_data.
    if packages_row is None:
        packages = None
    elif packages_row["status"] not in ("ok",):
        packages = None if not packages_row.get("data") else packages_row["data"]

    services = None
    if services_row is not None and services_row["status"] == "ok":
        services = services_row["data"]
    elif services_row is not None:
        services = services_row.get("data")

    settings = None
    if settings_row is not None:
        settings = settings_row.get("data") if isinstance(settings_row.get("data"), dict) else {}

    rules = profile["rules"] or {}
    findings: list[dict[str, Any]] = []
    now = datetime.now(UTC)

    for rule in rules.get("packages") or []:
        if not isinstance(rule, dict) or not rule.get("name"):
            continue
        name = str(rule["name"])
        rid = str(rule.get("id") or f"pkg.{name}")
        expected = str(rule.get("state") or "installed")
        present = _package_present(packages if packages_row else None, name)
        if packages_row is None:
            status = "missing_data"
            observed = None
        elif present is None:
            status = "missing_data"
            observed = None
        elif expected == "installed":
            status = "compliant" if present else "noncompliant"
            observed = "installed" if present else "absent"
        elif expected == "absent":
            status = "compliant" if not present else "noncompliant"
            observed = "installed" if present else "absent"
        else:
            status = "missing_data"
            observed = None
        findings.append(
            {
                "rule_id": rid,
                "category": "package",
                "attribute": name,
                "expected": expected,
                "observed": observed,
                "status": status,
                "detected_at": now,
            }
        )

    for rule in rules.get("services") or []:
        if not isinstance(rule, dict) or not rule.get("unit"):
            continue
        unit = str(rule["unit"])
        rid = str(rule.get("id") or f"svc.{unit}")
        expected = str(rule.get("state") or "active")
        if services_row is None:
            status = "missing_data"
            observed = None
        else:
            active = _service_active(services, unit)
            if active is None:
                status = "missing_data"
                observed = None
            elif expected == "active":
                status = "compliant" if active else "noncompliant"
                observed = "active" if active else "inactive"
            else:
                status = "compliant" if not active else "noncompliant"
                observed = "active" if active else "inactive"
        findings.append(
            {
                "rule_id": rid,
                "category": "service",
                "attribute": unit,
                "expected": expected,
                "observed": observed,
                "status": status,
                "detected_at": now,
            }
        )

    for rule in rules.get("settings") or []:
        if not isinstance(rule, dict) or not rule.get("key"):
            continue
        key = str(rule["key"])
        rid = str(rule.get("id") or f"set.{key}")
        expected = str(rule.get("expected", ""))
        when = rule.get("when") if isinstance(rule.get("when"), dict) else None
        applicability = _when_applies(when, packages if packages_row else None)
        if applicability == "no":
            findings.append(
                {
                    "rule_id": rid,
                    "category": "setting",
                    "attribute": key,
                    "expected": expected,
                    "observed": None,
                    "status": "not_applicable",
                    "detected_at": now,
                }
            )
            continue
        if applicability == "unknown" or settings_row is None:
            findings.append(
                {
                    "rule_id": rid,
                    "category": "setting",
                    "attribute": key,
                    "expected": expected,
                    "observed": None,
                    "status": "missing_data",
                    "detected_at": now,
                }
            )
            continue
        observed = _setting_value(settings, key)
        if observed is None:
            status = "missing_data"
        elif observed == expected:
            status = "compliant"
        else:
            status = "noncompliant"
        findings.append(
            {
                "rule_id": rid,
                "category": "setting",
                "attribute": key,
                "expected": expected,
                "observed": observed,
                "status": status,
                "detected_at": now,
            }
        )

    return findings


def _scan_status(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "ok"
    statuses = {f["status"] for f in findings}
    has_gap = "noncompliant" in statuses
    has_missing = "missing_data" in statuses
    only_ok = statuses <= {"compliant", "not_applicable"}
    if only_ok:
        return "ok"
    if has_gap and has_missing:
        return "partial"
    if has_gap:
        return "noncompliant"
    if has_missing and statuses <= {"missing_data", "not_applicable"}:
        return "no_data"
    if has_missing:
        return "partial"
    return "partial"


def scan_host(
    conn: Conn,
    *,
    host_id: UUID,
    principal: Principal | None = None,
    profile_slug: str = "ubuntu-web-baseline",
    profile_version: str | None = None,
    source: str = "rackloom",
    job_id: UUID | None = None,
    actor_type: str = "system",
    actor_id: str = "compliance",
) -> dict[str, Any]:
    """Exécute un scan en lecture seule et persiste findings + scan."""
    if principal is not None:
        host = _host_visible(conn, principal, host_id)
    else:
        found = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
        if found is None:
            raise not_found("machine")
        host = found
    profile = get_profile(conn, profile_slug, profile_version)
    if profile["status"] != "published":
        raise ApiError(409, "profile_not_published", "seul un profil publié peut être évalué")
    if source not in ("rackloom", "rudder_fake", "rudder", "system"):
        raise ApiError(422, "invalid_source", "source de scan invalide", field="/source")

    findings = evaluate_host(conn, host, profile)
    status = _scan_status(findings)
    scan_id = uuid7()
    counts = {s: sum(1 for f in findings if f["status"] == s) for s in FINDING_STATUSES}
    conn.execute(
        "INSERT INTO compliance_scans (id, project_id, host_id, profile_id, status, source, "
        "finished_at, summary, job_id) VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s)",
        (
            scan_id,
            host["project_id"],
            host["id"],
            profile["id"],
            status,
            source,
            Jsonb(
                {
                    "finding_count": len(findings),
                    "counts": counts,
                    "profile": {"slug": profile["slug"], "version": profile["version"]},
                    "write_jobs": 0,
                    "correction": False,
                }
            ),
            job_id,
        ),
    )
    for f in findings:
        conn.execute(
            "INSERT INTO compliance_findings (id, scan_id, project_id, host_id, rule_id, category, "
            "attribute, expected, observed, status, detected_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid7(),
                scan_id,
                host["project_id"],
                host["id"],
                f["rule_id"],
                f["category"],
                f["attribute"],
                f.get("expected"),
                f.get("observed"),
                f["status"],
                f["detected_at"],
            ),
        )
    events.emit(
        conn,
        type="compliance.scanned",
        source="system",
        project_id=host["project_id"],
        correlation_id=host.get("change_id"),
        data={
            "message": f"Conformité : {status}",
            "host_id": str(host["id"]),
            "status": status,
            "findings": len(findings),
            "correction": False,
        },
    )
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="compliance.scan",
        outcome="ok",
        project_id=host["project_id"],
        targets={"host_id": str(host["id"]), "status": status, "scan_id": str(scan_id)},
        versions={"profile": f"{profile['slug']}@{profile['version']}"},
    )
    result = latest_for_host(conn, host["id"], scan_id=scan_id)
    assert result is not None
    return result


def latest_for_host(conn: Conn, host_id: UUID, *, scan_id: UUID | None = None) -> dict[str, Any] | None:
    if scan_id is not None:
        scan = conn.execute("SELECT * FROM compliance_scans WHERE id = %s", (scan_id,)).fetchone()
    else:
        scan = conn.execute(
            "SELECT * FROM compliance_scans WHERE host_id = %s ORDER BY started_at DESC LIMIT 1",
            (host_id,),
        ).fetchone()
    if scan is None:
        return None
    findings = conn.execute(
        "SELECT rule_id, category, attribute, expected, observed, status, detected_at "
        "FROM compliance_findings WHERE scan_id = %s ORDER BY category, attribute",
        (scan["id"],),
    ).fetchall()
    profile = conn.execute(
        "SELECT slug, version, title FROM compliance_profiles WHERE id = %s", (scan["profile_id"],)
    ).fetchone()
    return {
        "scan_id": str(scan["id"]),
        "host_id": str(scan["host_id"]),
        "project_id": str(scan["project_id"]),
        "status": scan["status"],
        "source": scan["source"],
        "profile": {
            "slug": profile["slug"],
            "version": profile["version"],
            "title": profile["title"],
        }
        if profile
        else None,
        "started_at": scan["started_at"].isoformat(),
        "finished_at": scan["finished_at"].isoformat() if scan["finished_at"] else None,
        "summary": scan["summary"] or {},
        "findings": [
            {
                "rule_id": f["rule_id"],
                "category": f["category"],
                "attribute": f["attribute"],
                "expected": f.get("expected"),
                "observed": f.get("observed"),
                "status": f["status"],
                "detected_at": f["detected_at"].isoformat(),
            }
            for f in findings
        ],
        "correction": False,
        "notice": "Lecture seule : aucune correction automatique. Remédiation = brouillon à approuver.",
    }


def propose_remediation(
    conn: Conn,
    principal: Principal,
    host_id: UUID,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Crée un brouillon de demande de correction — aucun job d'écriture."""
    host = _host_visible(conn, principal, host_id)
    require_role(principal, "operator", host["project_id"], host["environment_id"])
    if not host.get("deployment_id"):
        raise ApiError(409, "no_deployment", "la machine n'est liée à aucun déploiement")
    latest = latest_for_host(conn, host["id"])
    if latest is None:
        raise ApiError(409, "no_scan", "lancez d'abord un scan de conformité")
    gaps = [f for f in latest["findings"] if f["status"] == "noncompliant"]
    if not gaps:
        raise ApiError(
            409,
            "no_gap",
            f"aucun écart non conforme : rien à remédier (statut scan « {latest['status']} »)",
        )

    write_kinds = [
        "tf_apply",
        "ansible_configure",
        "vm_restore",
        "service_restart",
        "lease_retire",
        "tf_import",
    ]
    before_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE project_id = %s AND kind = ANY(%s)",
        (host["project_id"], write_kinds),
    ).fetchone()
    before = int(before_row["n"]) if before_row else 0

    change = changes.create_modify_change(conn, principal, host["deployment_id"], request_id)
    dep = changes.get_deployment(conn, principal, host["deployment_id"])
    env = conn.execute(
        "SELECT slug, is_production FROM environments WHERE id = %s", (dep["environment_id"],)
    ).fetchone()
    if env is None:
        raise not_found("environnement")
    current_tier = next(
        (i.get("size_tier") for i in (dep.get("instances") or []) if i.get("size_tier")),
        "test_leger",
    )
    gap_labels = ", ".join(f"{g['category']}:{g['attribute']}" for g in gaps[:8])
    profile_meta = latest.get("profile") or {}
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env["slug"],
        "inputs": {
            "quantity": max(1, len(dep.get("instances") or []) or 1),
            "size_tier": current_tier,
            "usage": (
                f"Remédiation conformité ({profile_meta.get('slug')}@{profile_meta.get('version')}) : {gap_labels}"
            )[:80],
            "lifetime_until": (date.today() + timedelta(days=14)).isoformat(),
        },
        "confirm_production": bool(env["is_production"]),
    }
    changes.add_version(
        conn,
        principal,
        change["id"],
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )
    refreshed = conn.execute("SELECT * FROM changes WHERE id = %s", (change["id"],)).fetchone()
    if refreshed is None:
        raise not_found("demande")
    change = refreshed

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="compliance.propose_remediation",
        outcome="ok",
        project_id=host["project_id"],
        correlation_id=change["id"],
        targets={
            "host_id": str(host["id"]),
            "scan_id": latest["scan_id"],
            "change_id": str(change["id"]),
            "gaps": str(len(gaps)),
        },
        request_id=request_id,
    )
    events.emit(
        conn,
        type="compliance.remediation_proposed",
        source="system",
        project_id=host["project_id"],
        correlation_id=change["id"],
        data={
            "message": "Brouillon de remédiation conformité (aucune écriture)",
            "host_id": str(host["id"]),
            "change_id": str(change["id"]),
            "gaps": len(gaps),
        },
    )

    after_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE project_id = %s AND kind = ANY(%s)",
        (host["project_id"], write_kinds),
    ).fetchone()
    after = int(after_row["n"]) if after_row else 0
    if after != before:
        raise ApiError(
            500,
            "unexpected_write_job",
            "la proposition de remédiation ne doit créer aucun job d'écriture",
        )

    return {
        "action": "propose_remediation",
        "scan_id": latest["scan_id"],
        "change_id": str(change["id"]),
        "change_state": change["state"],
        "gaps": gaps,
        "write_jobs_created": 0,
        "correction": False,
        "notice": (
            "Brouillon de demande créé : plan et approbation requis. Aucune écriture fournisseur n'a été déclenchée."
        ),
    }
