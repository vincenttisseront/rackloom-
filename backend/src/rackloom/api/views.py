"""Représentations renvoyées par l'API. Aucune donnée hors périmètre, aucun secret, aucun plan brut."""


from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .. import hosts as hosts_mod
from .. import registry, resolver, workflow
from ..changes.service import change_ref
from ..changes.transitions import STATES
from ..db import Conn
from ..identity.authz import Principal
from ..projects import calendars, freezes

SERVICE_STATUS = {
    "unknown": "Non vérifié",
    "ready": "Prêt",
    "created_not_ready": "Créé, pas prêt",
    "failed": "Échec",
    "verification_unavailable": "Vérification indisponible",
    "unavailable": "Indisponible",
}


DEPLOYMENT_STATUS = {"pending": "En préparation", "active": "Actif", "failed": "Échec", "abandoned": "Abandonné"}


HOST_STATES = {
    "created": "Créée",
    "no_address": "Sans adresse",
    "unreachable": "Injoignable",
    "enrolled": "Préparation vérifiée",
    "configured": "Configurée",
    "config_failed": "Configuration en échec",
    "verified": "Vérifiée",
    "verify_failed": "Vérification en échec",
    "wave_blocked": "Vague non lancée (échec précédent)",
    "out_of_scope": "Hors de la liste approuvée",
}


SOURCE_LABELS = {
    "user": "Vous", "suggestion": "Proposé par l'assistant", "template": "Profil de service",
    "template_default": "Défaut du profil", "environment_default": "Seul environnement disponible",
    "environment_policy": "Politique de l'environnement", "environment_policy_default": "Politique de l'environnement",
    "inventory": "Inventaire", "tie_break": "Départage", "platform": "Plateforme",
    "deployment_pinned": "Version du déploiement", "template_latest_published": "Dernière version publiée",
    "template_latest_promoted": "Dernière version promue pour l'environnement",
    "project_preference": "Préférence de projet",
}


GROUP_LABELS = {"service": "Service et versions", "placement": "Placement", "reseau": "Réseau",
                "dimensionnement": "Dimensionnement", "execution": "Exécution", "gouvernance": "Gouvernance"}


SIMULATED_STEPS = {"vm_inventory": "Inventaire vCenter (connecteur fictif)",
                   "guest_customization": "Personnalisation invitée (vCenter fictif)",
                   "ip_assignment": "Attribution d'adresse (nœud de labo associé)"}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def freeze_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    s = freezes.summary(row)
    return {**s, "id": str(s["id"]), "frozen_at": iso(s["frozen_at"])} if s else None


def environment_view(conn: Conn, principal: Principal, env: dict[str, Any], *,
                     history: bool = False) -> dict[str, Any]:
    """État de gel, calendrier et actions permises ; aucune donnée d'un autre projet."""
    current = freezes.active(conn, env["id"])
    out: dict[str, Any] = {
        "id": str(env["id"]), "slug": env["slug"], "name": env["name"], "kind": env["kind"],
        "is_production": env["is_production"],
        "frozen": current is not None, "freeze": freeze_view(current),
        "calendar": calendars.summary(conn, env["id"]),
        "write_jobs": freezes.write_jobs(conn, env["id"]),
        "allowed_actions": workflow.available_actions(conn, principal, "environment", env),
    }
    if "project_slug" in env:
        out["project"] = {"id": str(env["project_id"]), "slug": env["project_slug"], "name": env["project_name"]}
    if history:
        out["history"] = [{**h, "id": str(h["id"]), "frozen_at": iso(h["frozen_at"]), "lifted_at": iso(h["lifted_at"])}
                          for h in freezes.history(conn, env["id"])]
    return out


def plan_headline(counts: dict[str, int]) -> str:
    """« 1 VM de labo à créer, 0 modification, 0 suppression, 0 remplacement » (§1.1, étape 8)."""
    def n(count: int, word: str) -> str:
        return f"{count} {word}{'s' if count > 1 else ''}"
    return (f"{counts['create']} VM de labo à créer, {n(counts['update'], 'modification')}, "
            f"{n(counts['delete'], 'suppression')}, {n(counts['replace'], 'remplacement')}")


def plan_details(resolution: dict[str, Any]) -> dict[str, Any]:
    """Panneau « Détails du plan » : valeurs résolues par thème, en lecture seule, avec provenance."""
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g in GROUP_LABELS}
    for pointer, v in sorted(resolution.get("values", {}).items()):
        groups[v["group"]].append({
            "pointer": pointer, "value": v["value"], "label": v.get("label"), "source": v["source"],
            "source_label": SOURCE_LABELS.get(v["source"], v["source"]), "ref": v.get("ref"),
            "applied": v.get("applied", True), "candidates": v.get("candidates")})
    inv = resolution.get("inventory", {})
    cap = resolution.get("capacity") or {}
    return {"groups": [{"group": g, "label": GROUP_LABELS[g], "values": vals} for g, vals in groups.items() if vals],
            "inventory": {"observed_at": inv.get("observed_at"), "simulated": inv.get("simulated", True)},
            "capacity": {
                "observed_at": cap.get("observed_at"),
                "age_seconds": (
                    int((datetime.now(UTC) - datetime.fromisoformat(cap["observed_at"])).total_seconds())
                    if cap.get("observed_at") else None
                ),
                "reservation": False,
                "notice": cap.get("notice") or "Mesure d'inventaire, pas une réservation.",
                "simulated": cap.get("simulated", True),
                "resources": [
                    {
                        "resource": r.get("resource"),
                        "target": r.get("target"),
                        "free": r.get("free"),
                        "need": r.get("need"),
                        "unit": r.get("unit"),
                        "ok": r.get("ok"),
                    }
                    for r in cap.get("resources") or []
                ],
            } if cap else None,
            "simulated_steps": [{"key": s, "label": SIMULATED_STEPS.get(s, s)}
                                for s in resolution.get("simulated_steps", [])],
            "preview_limits": resolution.get("preview_limits", [])}


def ansible_contract(resolution: dict[str, Any]) -> dict[str, Any]:
    comps = []
    for c in resolution.get("components", []):
        effects = c.get("effects", {})
        ports = [{"port": str(p["port"]).format(**c["config"]), "proto": p.get("proto", "tcp")}
                 for p in effects.get("ports", [])]
        comps.append({"slug": c["slug"], "version": c["version"], "title": c["title"],
                      "role": f"{c['role']['name']}@{c['role']['version']}", "config": c["config"],
                      "packages": effects.get("packages", []), "services": effects.get("services", []),
                      "ports": ports, "files": effects.get("files", []), "reboot": effects.get("reboot"),
                      "elevation": "rackloom-config (sudo)", "verification_level": c.get("verification_level")})
    return {"components": comps,
            "check_mode": {"covered": [], "not_covered": ["toutes les tâches : les VM n'existent pas avant "
                                                          "l'application"],
                           "limits": resolution.get("preview_limits", [])},
            "identities": {"configure": "rackloom-config (élévation)", "collect": "rackloom-collect (sans élévation)"}}


def revision_view(conn: Conn, revision: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    resolution = revision["resolved"]
    return {
        "revision_no": revision["revision_no"], "version_no": revision["version_no"],
        "outcome": revision["outcome"], "resolution_digest": revision["resolution_digest"],
        "created_at": iso(revision["created_at"]),
        "summary": resolver.summary(resolution, project),
        "blocks": revision["block_reasons"],
        "missing": resolution.get("missing", []),
        "template": resolution.get("template"),
        "environment": resolution.get("environment"),
        "inputs": resolution.get("inputs"),
        "input_sources": resolution.get("input_sources"),
        "instances": resolution.get("instances", []),
        "details": plan_details(resolution) if resolution["outcome"] != "blocked" else None,
        "provenance": revision["provenance"],
    }


def plan_view(conn: Conn, plan: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    resolution = revision["resolved"]
    summary = plan["summary"]
    counts = summary["vm_counts"]
    risks = []
    if summary.get("destructive"):
        risks.append(
            {
                "code": "destructive",
                "level": "blocking",
                "text": f"Le plan supprime ({counts['delete']}) ou remplace ({counts['replace']}) des "
                "ressources : approbation impossible au lot 1.",
            }
        )
    if summary.get("retirement"):
        risks.append(
            {
                "code": "retirement",
                "level": "warning",
                "text": (
                    f"Plan de retrait ({counts.get('delete', 0)} ressource(s)) : "
                    "aucune suppression réelle tant que non approuvé et exécuté ; "
                    "exécution locale simulée (pas de destroy Terraform)."
                ),
            }
        )
    if summary.get("interruption") and not summary.get("retirement"):
        risks.append(
            {
                "code": "interruption",
                "level": "warning",
                "text": "Redémarrage : interruption de service pendant l'exécution (jamais masquée).",
            }
        )
    elif summary.get("interruption") and summary.get("retirement"):
        risks.append(
            {
                "code": "interruption",
                "level": "warning",
                "text": "Retrait : interruption de service si exécuté (jamais masquée).",
            }
        )
    if resolution["environment"]["is_production"]:
        risks.append({"code": "production", "level": "warning", "text": "Déploiement en production."})
    risks.append(
        {
            "code": "approval",
            "level": "info",
            "text": "Approbation requise par un approbateur distinct du demandeur."
            if resolution["approval"]["distinct_approver"]
            else "Approbation requise.",
        }
    )
    return {
        "id": str(plan["id"]),
        "status": plan["status"],
        "revision_no": plan["revision_no"],
        "version_no": plan["version_no"],
        "valid_until": iso(plan["valid_until"]),
        "created_at": iso(plan["created_at"]),
        "bundle_digest": plan["bundle_digest"],
        "headline": plan_headline(counts),
        "counts": counts,
        "terraform_version": summary.get("terraform_version"),
        "vms": [
            {"name": v["name"], "action": v["action"], "attributes": v.get("attributes", {})}
            for v in summary.get("vms", [])
        ],
        "resources": [
            {"address": r["address"], "type": r["type"], "action": r["action"]} for r in summary.get("resources", [])
        ],
        "destructive": summary.get("destructive", False),
        "interruption": bool(summary.get("interruption")),
        "restart": bool(summary.get("restart")),
        "retirement": bool(summary.get("retirement")),
        "risks": risks,
        "details": plan_details(resolution),
        "ansible": ansible_contract(resolution),
        "verifications": [resolver.CHECK_LABELS[c["type"]].format(**c) for c in resolution["health_checks"]],
        "waves": (plan["bundle"].get("targets") or {}).get("waves") or [],
        "bundle": {
            "template": plan["bundle"]["template"],
            "components": [
                {"slug": c["slug"], "version": c["version"], "digest": c["digest"]}
                for c in plan["bundle"]["components"]
            ],
            "terraform": plan["bundle"]["terraform"],
            "policy_bundle": plan["bundle"]["policy_bundle"],
            "environment": plan["bundle"]["environment"],
            "request": plan["bundle"]["request"],
            "runner_images": plan["bundle"]["runner_images"],
        },
    }


def _last_transition(conn: Conn, change_id) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM state_transitions WHERE change_id = %s ORDER BY id DESC LIMIT 1",
                        (change_id,)).fetchone()


def change_summary(conn: Conn, change: dict[str, Any]) -> dict[str, Any]:
    env = conn.execute("SELECT slug, name, is_production FROM environments WHERE id = %s",
                       (change["environment_id"],)).fetchone() if change["environment_id"] else None
    last = _last_transition(conn, change["id"])
    return {"id": str(change["id"]), "ref": change_ref(change), "kind": change["kind"], "title": change["title"],
            "state": change["state"], "state_label": STATES[change["state"]],
            "reason": (last["detail"] or {}).get("message") if last else None,
            "environment": dict(env) if env else None,
            "deployment_id": str(change["deployment_id"]) if change["deployment_id"] else None,
            "current_version_no": change["current_version_no"], "current_revision_no": change["current_revision_no"],
            "created_at": iso(change["created_at"]), "updated_at": iso(change["updated_at"])}


def change_view(conn: Conn, principal: Principal, change: dict[str, Any]) -> dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (change["project_id"],)).fetchone()
    version = conn.execute("SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
                           (change["id"], change["current_version_no"])).fetchone()
    if version is None and change["kind"] == "modify":
        # Point de départ d'une modification : le besoin en vigueur sur le déploiement.
        version = conn.execute(
            "SELECT v.* FROM changes c JOIN request_versions v ON v.change_id = c.id "
            "AND v.version_no = c.current_version_no WHERE c.deployment_id = %s "
            "AND c.state IN ('succeeded', 'partially_succeeded') ORDER BY c.created_at DESC LIMIT 1",
            (change["deployment_id"],)).fetchone()
    revision = conn.execute("SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
                            (change["id"], change["current_revision_no"])).fetchone()
    plan = conn.execute("SELECT * FROM plans WHERE change_id = %s ORDER BY created_at DESC LIMIT 1",
                        (change["id"],)).fetchone()
    deployment = conn.execute("SELECT * FROM deployments WHERE id = %s",
                              (change["deployment_id"],)).fetchone() if change["deployment_id"] else None
    pending_retry = conn.execute("SELECT id, host_ids, bundle_digest, requested_at FROM retry_requests "
                                 "WHERE change_id = %s AND status = 'pending' ORDER BY requested_at DESC LIMIT 1",
                                 (change["id"],)).fetchone()
    creator = conn.execute("SELECT display_name FROM users WHERE id = %s", (change["created_by"],)).fetchone()
    return {
        **change_summary(conn, change),
        "project": {"id": str(project["id"]), "slug": project["slug"], "name": project["name"]},
        "created_by": creator["display_name"] if creator else None,
        "need": version["need"] if version else None,
        "need_provenance": version["provenance"] if version else None,
        "revision": revision_view(conn, revision, project) if revision else None,
        "plan": plan_view(conn, plan, conn.execute(
            "SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
            (change["id"], plan["revision_no"])).fetchone()) if plan else None,
        "deployment": (
            {
                **deployment_summary(conn, deployment),
                "service_card": _service_card(conn, deployment, principal),
            }
            if deployment
            else None
        ),
        "pending_retry": {"id": str(pending_retry["id"]), "host_ids": pending_retry["host_ids"],
                          "bundle_digest": pending_retry["bundle_digest"],
                          "requested_at": iso(pending_retry["requested_at"])} if pending_retry else None,
        "environment_freeze": freeze_view(freezes.active(conn, change["environment_id"]))
        if change["environment_id"] else None,
        "allowed_actions": workflow.available_actions(conn, principal, "change", change),
    }


def _composition_view(conn: Conn, dep: dict[str, Any], host_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """F02 : résumé des membres de composition et de leur état (lecture seule)."""
    from .. import composition as composition_mod

    tpl = registry.template_by_id(conn, dep["template_version_id"])
    contract = tpl["contract"]
    if not composition_mod.has_composition(contract):
        return None
    by_member: dict[str, list[dict[str, Any]]] = {}
    for h in host_rows:
        mid = h.get("composition_member")
        if mid:
            by_member.setdefault(mid, []).append(h)
    members_out = []
    for m in composition_mod.members(contract):
        hosts_m = by_member.get(m["id"]) or []
        members_out.append(
            {
                "id": m["id"],
                "title": m["title"],
                "depends_on": list(m.get("depends_on") or []),
                "hosts": [
                    {
                        "id": str(h["id"]),
                        "name": h["name"],
                        "deployment_state": h["deployment_state"],
                    }
                    for h in hosts_m
                ],
            }
        )
    return {"members": members_out}


def deployment_summary(conn: Conn, dep: dict[str, Any]) -> dict[str, Any]:
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    env = conn.execute("SELECT slug, name, is_production FROM environments WHERE id = %s",
                       (dep["environment_id"],)).fetchone()
    return {"id": str(dep["id"]), "template": {"slug": dep["template_slug"], "version": tpl["version"],
                                               "title": tpl["contract"]["title"], "status": tpl["status"]},
            "environment": dict(env), "status": dep["status"], "status_label": DEPLOYMENT_STATUS[dep["status"]],
            "instances": dep["instances"],
            "service_status": dep["service_status"], "service_status_label": SERVICE_STATUS[dep["service_status"]],
            "service_reason": dep["service_reason"], "service_checked_at": iso(dep["service_checked_at"]),
            "created_at": iso(dep["created_at"])}


def _service_card(conn: Conn, dep: dict[str, Any], principal: Principal | None = None) -> dict[str, Any]:
    from ..deployments import service_card

    return service_card.build(conn, dep, principal=principal)


def deployment_view(conn: Conn, principal: Principal, dep: dict[str, Any]) -> dict[str, Any]:
    from ..deployments import service_card

    changes = conn.execute("SELECT * FROM changes WHERE deployment_id = %s ORDER BY created_at", (dep["id"],))
    host_rows = conn.execute("SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (dep["id"],)).fetchall()
    return {
        **deployment_summary(conn, dep),
        "project_id": str(dep["project_id"]),
        "service_checks": dep["service_checks"],
        "service_card": service_card.build(conn, dep, principal=principal),
        "hosts": [
            {
                "id": str(h["id"]),
                "name": h["name"],
                "deployment_state": h["deployment_state"],
                "deployment_state_label": HOST_STATES.get(h["deployment_state"], h["deployment_state"]),
                "composition_member": h.get("composition_member"),
                "health": hosts_mod.health(conn, h),
            }
            for h in host_rows
        ],
        "composition": _composition_view(conn, dep, host_rows),
        "changes": [change_summary(conn, c) for c in changes.fetchall()],
        "allowed_actions": workflow.available_actions(conn, principal, "deployment", dep),
    }


def job_view(conn: Conn, job: dict[str, Any]) -> dict[str, Any]:
    from ..executions import quotas as fair

    steps = conn.execute("SELECT * FROM job_steps WHERE job_id = %s ORDER BY seq", (job["id"],)).fetchall()
    wait = job.get("wait_reason")
    return {"id": str(job["id"]), "kind": job["kind"], "class": job["class"], "state": job["state"],
            "attempts": job["attempts"], "blocked_reason": job["blocked_reason"],
            "wait_reason": wait,
            "wait_reason_label": fair.waiting_reason_label(wait),
            "dispatched_at": iso(job["dispatched_at"]), "cancel_requested_at": iso(job["cancel_requested_at"]),
            "created_at": iso(job["created_at"]), "finished_at": iso(job["finished_at"]),
            "steps": [{"seq": s["seq"], "name": s["name"], "state": s["state"], "simulated": s["simulated"],
                       "started_at": iso(s["started_at"]), "finished_at": iso(s["finished_at"]),
                       "summary": s["summary"]} for s in steps]}


def event_view(row: dict[str, Any]) -> dict[str, Any]:
    from ..events import scrub_event_data

    data = row["data"] if isinstance(row["data"], dict) else {}
    return {"id": row["id"], "type": row["type"], "source": row["source"], "severity": row["severity"],
            "occurred_at": iso(row["occurred_at"]), "job_id": str(row["job_id"]) if row["job_id"] else None,
            "correlation_id": str(row["correlation_id"]) if row["correlation_id"] else None,
            "data": scrub_event_data(data)}


def host_view(conn: Conn, host: dict[str, Any]) -> dict[str, Any]:
    base = hosts_mod.view(conn, host)
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (host["deployment_id"],)).fetchone()
    tpl = registry.template_by_id(conn, host["template_version_id"])
    base["deployment"] = {"id": str(dep["id"]), "service_status": dep["service_status"],
                          "service_status_label": SERVICE_STATUS[dep["service_status"]]}
    base["template"] = {"slug": tpl["slug"], "version": tpl["version"], "title": tpl["contract"]["title"]}
    return base
