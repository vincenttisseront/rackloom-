"""Admission au démarrage effectif d'un job : registre ordonné de règles, décision enregistrée.

Chaque job est réévalué au moment où un worker le réclame, quelle que soit sa date de mise en
file. Les emplacements 80 (F28), 90 (F27), 100 (F11), 110 (F05), 120 (F03), 130 (F16) et
140 (F10) sont réservés et non enregistrés au lot 1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .. import policy
from ..db import Jsonb
from ..identity.authz import Principal, load_bindings


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    code: str = ""


@dataclass
class Context:
    conn: psycopg.Connection
    job: dict[str, Any]
    change: dict[str, Any] | None
    environment: dict[str, Any] | None
    execution: dict[str, Any] | None
    approval: dict[str, Any] | None
    plan: dict[str, Any] | None
    # Calcul de l'empreinte courante, fourni par le worker (déchiffre et hache plan.bin).
    recompute_digest: Callable[[], str] | None = None


Rule = Callable[[Context], Verdict]
REGISTRY: list[tuple[int, str, str, Rule]] = []


def rule(order: int, rule_id: str, job_class: str) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        REGISTRY.append((order, rule_id, job_class, fn))
        REGISTRY.sort(key=lambda r: r[0])
        return fn
    return register


def _principal(conn: psycopg.Connection, user_id) -> Principal | None:
    user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if user is None or user["status"] != "active":
        return None
    return Principal(user_id=user["id"], email=user["email"], display_name=user["display_name"],
                     session_id=user["id"], auth_method="admission", auth_time=datetime.now(UTC),
                     bindings=load_bindings(conn, user["id"]))


@rule(10, "R010", "all")
def simulated_connector_in_production(ctx: Context) -> Verdict:
    if ctx.environment is None or ctx.environment["kind"] != "prod":
        return Verdict(True)
    simulated = ctx.conn.execute("SELECT slug FROM connectors WHERE mode = 'simulated' AND kind = 'vsphere'").fetchall()
    if simulated:
        return Verdict(False, "connecteur simulé refusé pour un environnement de production", "simulated_in_prod")
    return Verdict(True)


@rule(20, "R020", "all")
def requester_rights(ctx: Context) -> Verdict:
    if ctx.change is None:
        return Verdict(True)
    version = ctx.conn.execute(
        "SELECT created_by FROM request_versions WHERE change_id = %s AND version_no = %s",
        (ctx.change["id"], ctx.change["current_version_no"])).fetchone()
    author = version["created_by"] if version else ctx.change["created_by"]
    p = _principal(ctx.conn, author)
    env_id = ctx.environment["id"] if ctx.environment else None
    if p is None or not p.has_role("operator", ctx.change["project_id"], env_id):
        return Verdict(False, "le demandeur n'a plus le rôle opérateur sur ce projet", "requester_revoked")
    return Verdict(True)


@rule(30, "R030", "write")
def approver_rights(ctx: Context) -> Verdict:
    if ctx.approval is None:
        return Verdict(False, "aucune approbation associée", "approval_missing")
    p = _principal(ctx.conn, ctx.approval["decided_by"])
    env_id = ctx.environment["id"] if ctx.environment else None
    if p is None or not p.has_role("approver", ctx.change["project_id"], env_id):
        return Verdict(False, "l'approbateur n'a plus le rôle approbateur sur ce projet", "approver_revoked")
    author = ctx.conn.execute(
        "SELECT created_by FROM request_versions WHERE change_id = %s AND version_no = %s",
        (ctx.change["id"], ctx.plan["version_no"] if ctx.plan else ctx.change["current_version_no"])).fetchone()
    if author and author["created_by"] == ctx.approval["decided_by"]:
        return Verdict(False, "l'auteur de la version ne peut pas l'approuver", "self_approval")
    return Verdict(True)


@rule(40, "R040", "write")
def approval_valid(ctx: Context) -> Verdict:
    a = ctx.approval
    if a is None or a["decision"] != "approved":
        return Verdict(False, "approbation absente ou refusée", "approval_missing")
    if a["revoked_at"] is not None:
        return Verdict(False, f"approbation révoquée ({a['revoked_reason']})", "approval_revoked")
    if ctx.execution is None:
        return Verdict(False, "approbation non liée à cette exécution", "approval_mismatch")
    bound = ctx.execution["approval_id"] == a["id"] or (a["kind"] == "retry"
                                                        and a["execution_id"] == ctx.execution["id"])
    if not bound:
        return Verdict(False, "approbation non liée à cette exécution", "approval_mismatch")
    if ctx.job["kind"] == "tf_apply":
        other = ctx.conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'tf_apply' AND execution_id = %s AND id <> %s "
            "AND dispatched_at IS NOT NULL", (ctx.execution["id"], ctx.job["id"])).fetchone()
        if other:
            return Verdict(False, "approbation déjà consommée par une autre exécution", "approval_consumed")
    return Verdict(True)


@rule(50, "R050", "write")
def bundle_digest(ctx: Context) -> Verdict:
    if ctx.job["kind"] != "tf_apply":
        return Verdict(True)
    if ctx.plan is None or ctx.recompute_digest is None:
        return Verdict(False, "plan introuvable", "plan_missing")
    try:
        current = ctx.recompute_digest()
    except Exception as exc:  # noqa: BLE001 - toute impossibilité de recalcul bloque
        return Verdict(False, f"empreinte non recalculable : {exc}", getattr(exc, "code", "bundle_mismatch"))
    if current != ctx.approval["bundle_digest"] or current != ctx.plan["bundle_digest"]:
        return Verdict(False, "le dossier d'exécution ne correspond plus à l'empreinte approuvée", "bundle_mismatch")
    return Verdict(True)


@rule(60, "R060", "write")
def plan_valid(ctx: Context) -> Verdict:
    if ctx.job["kind"] != "tf_apply":
        return Verdict(True)
    plan = ctx.plan
    if plan is None:
        return Verdict(False, "plan introuvable", "plan_missing")
    if plan["status"] == "blocked_destructive" or plan["summary"].get("destructive"):
        return Verdict(False, "le plan contient une suppression ou un remplacement", "destructive")
    if plan["status"] != "ready":
        return Verdict(False, f"plan dans l'état {plan['status']}", "plan_not_ready")
    margin = timedelta(seconds=policy.BUILTIN_RULES["platform"]["plan_expiry_margin_seconds"])
    if plan["valid_until"] <= datetime.now(UTC) + margin:
        return Verdict(False, "le plan a expiré", "plan_expired")
    return Verdict(True)


@rule(70, "R070", "all")
def cancel_not_requested(ctx: Context) -> Verdict:
    if ctx.job["cancel_requested_at"] is not None:
        return Verdict(False, "annulation demandée avant le démarrage", "cancel_requested")
    return Verdict(True)


@rule(900, "R900", "write")
def stack_lock(ctx: Context) -> Verdict:
    if ctx.job["stack_id"] is None or ctx.job["kind"] != "tf_apply":
        return Verdict(True)
    row = ctx.conn.execute(
        "UPDATE stacks SET lock_job_id = %s, lock_epoch = lock_epoch + 1 "
        "WHERE id = %s AND (lock_job_id IS NULL OR lock_job_id = %s) RETURNING id",
        (ctx.job["id"], ctx.job["stack_id"], ctx.job["id"])).fetchone()
    if row is None:
        return Verdict(False, "stack verrouillée par un autre job", "stack_locked")
    return Verdict(True)


def evaluate(ctx: Context) -> dict[str, Any]:
    """Évalue les règles applicables dans l'ordre ; s'arrête au premier refus. Enregistre la décision."""
    results = []
    outcome = "admitted"
    refusal: dict[str, Any] | None = None
    for order, rule_id, job_class, fn in REGISTRY:
        if job_class == "write" and ctx.job["class"] != "write":
            continue
        verdict = fn(ctx)
        results.append({"id": rule_id, "order": order, "ok": verdict.ok, "reason": verdict.reason,
                        "code": verdict.code})
        if not verdict.ok:
            outcome = "refused"
            refusal = results[-1]
            break
    active = policy.active()
    ctx.conn.execute(
        "INSERT INTO admission_decisions (job_id, change_id, outcome, policy_bundle_version, policy_bundle_digest, "
        "rules) VALUES (%s, %s, %s, %s, %s, %s)",
        (ctx.job["id"], ctx.change["id"] if ctx.change else None, outcome, active["version"], active["digest"],
         Jsonb(results)))
    return {"outcome": outcome, "rules": results, "refusal": refusal, "policy": active}


def release_stack(conn: psycopg.Connection, job: dict[str, Any]) -> None:
    if job.get("stack_id"):
        conn.execute("UPDATE stacks SET lock_job_id = NULL WHERE id = %s AND lock_job_id = %s",
                     (job["stack_id"], job["id"]))
