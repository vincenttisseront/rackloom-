"""Simulation de politique sur une demande historique (F10).

Réévalue les règles d'admission (ou un overlay candidat) sans aucune écriture :
ni admission_decisions, ni modification de plan, ni transition d'état.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from .. import admission, policy
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal


def _load_historical(conn: Conn, change_id: UUID) -> dict[str, Any]:
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (change_id,)).fetchone()
    if change is None:
        raise not_found("demande")
    plan = conn.execute(
        "SELECT * FROM plans WHERE change_id = %s ORDER BY created_at DESC LIMIT 1", (change_id,)
    ).fetchone()
    revision = None
    if change["current_revision_no"]:
        revision = conn.execute(
            "SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
            (change_id, change["current_revision_no"]),
        ).fetchone()
    environment = None
    if change["environment_id"]:
        environment = conn.execute("SELECT * FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
    approval = conn.execute(
        "SELECT * FROM approvals WHERE change_id = %s AND decision = 'approved' ORDER BY decided_at DESC LIMIT 1",
        (change_id,),
    ).fetchone()
    execution = None
    if approval and approval.get("execution_id"):
        execution = conn.execute("SELECT * FROM executions WHERE id = %s", (approval["execution_id"],)).fetchone()
    return {
        "change": change,
        "plan": plan,
        "revision": revision,
        "environment": environment,
        "approval": approval,
        "execution": execution,
    }


def simulate(
    conn: Conn,
    principal: Principal,
    change_id: UUID,
    *,
    candidate_organization: dict[str, Any] | None = None,
    job_kind: str = "tf_apply",
) -> dict[str, Any]:
    """Évalue sans écrire. Un overlay candidat n'altère pas le bundle actif ni les plans."""
    hist = _load_historical(conn, change_id)
    change = hist["change"]
    if not (
        principal.is_platform_admin
        or principal.is_global_auditor
        or principal.can_see_project(change["project_id"])
    ):
        raise not_found("demande")
    if not (
        principal.is_platform_admin
        or principal.has_role("project_admin", change["project_id"])
        or principal.has_role("auditor", change["project_id"])
        or principal.is_global_auditor
    ):
        raise forbidden("rôle project_admin, auditeur ou administrateur requis pour simuler")

    active = policy.active()
    candidate_bundle = deepcopy(policy.BUILTIN_RULES)
    if candidate_organization is not None:
        if not isinstance(candidate_organization, dict):
            raise ApiError(422, "invalid_overlay", "objet organization attendu", field="/candidate_organization")
        candidate_bundle["organization"] = candidate_organization

    # Contexte d'admission fictif (job write) — aucune ligne jobs n'est touchée.
    fake_job = {
        "id": change_id,
        "kind": job_kind,
        "class": "write",
        "cancel_requested_at": None,
        "stack_id": hist["plan"]["stack_id"] if hist["plan"] else None,
        "payload": {"plan_id": str(hist["plan"]["id"])} if hist["plan"] else {},
        "project_id": change["project_id"],
        "change_id": change_id,
    }
    ctx = admission.Context(
        conn=conn,
        job=fake_job,
        change=change,
        environment=hist["environment"],
        execution=hist["execution"],
        approval=hist["approval"],
        plan=hist["plan"],
        revision=hist["revision"],
        recompute_digest=None,
        organization_overlay=candidate_bundle.get("organization"),
        dry_run=True,
    )
    results: list[dict[str, Any]] = []
    outcome = "admitted"
    refusal: dict[str, Any] | None = None
    for order, rule_id, job_class, stages, fn in admission.REGISTRY:
        if job_class == "write" and fake_job["class"] != "write":
            continue
        if "start" not in stages:
            continue
        # En simulation : pas d'écriture ni d'artefact ; règles d'approbation sautées
        # si le dossier historique n'en a pas (on teste surtout l'overlay d'organisation).
        skip = rule_id in ("R050", "R900")
        if rule_id in ("R030", "R040") and hist["approval"] is None:
            skip = True
        if rule_id in ("R060",) and hist["plan"] is None:
            skip = True
        if skip:
            results.append(
                {
                    "id": rule_id,
                    "order": order,
                    "ok": True,
                    "reason": "sauté en simulation",
                    "code": "",
                    "field": None,
                    "skipped": True,
                }
            )
            continue
        verdict = fn(ctx)
        entry = {
            "id": rule_id,
            "order": order,
            "ok": verdict.ok,
            "reason": verdict.reason,
            "code": verdict.code,
            "field": verdict.field or None,
        }
        results.append(entry)
        if not verdict.ok:
            outcome = "refused"
            refusal = entry
            break

    return {
        "change_id": str(change_id),
        "outcome": outcome,
        "refusal": refusal,
        "rules": results,
        "active_policy": active,
        "candidate_organization": candidate_organization,
        "wrote": False,
        "plans_unchanged": True,
    }
