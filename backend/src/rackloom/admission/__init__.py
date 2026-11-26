"""Admission au démarrage effectif d'un job : registre ordonné de règles, décision enregistrée.

Chaque job est réévalué au moment où un worker le réclame, quelle que soit sa date de mise en
file (étape `start`). R065 (AT01) borne la configuration aux cibles approuvées. Les règles qui
dépendent d'un état pouvant changer entre l'admission et l'envoi au fournisseur (gel, calendrier,
capacité) sont rejouées dans la transaction qui marque cet envoi (étape `dispatch`). Emplacements
réservés, non encore enregistrés : 100 (F11), 115 (F39), 120 (F03),
125 (F34), 130 (F16), 135 (F38), 140 (F10). R095 (F37) et R110 (F05) sont enregistrées.
"""


from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import clock, policy
from ..connectors import circuit
from ..db import Conn, Jsonb
from ..identity.authz import Principal, load_bindings
from ..inventory import capacity as capacity_check
from ..projects import calendars, freezes

STAGES = ("start", "dispatch")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    code: str = ""


@dataclass
class Context:
    conn: Conn
    job: dict[str, Any]
    change: dict[str, Any] | None
    environment: dict[str, Any] | None
    execution: dict[str, Any] | None
    approval: dict[str, Any] | None
    plan: dict[str, Any] | None
    revision: dict[str, Any] | None = None
    # Calcul de l'empreinte courante, fourni par le worker (déchiffre et hache plan.bin).
    recompute_digest: Callable[[], str] | None = None


Rule = Callable[[Context], Verdict]
REGISTRY: list[tuple[int, str, str, tuple[str, ...], Rule]] = []


def rule(order: int, rule_id: str, job_class: str,
         stages: tuple[str, ...] = ("start",)) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        REGISTRY.append((order, rule_id, job_class, stages, fn))
        REGISTRY.sort(key=lambda r: r[0])
        return fn
    return register


def _principal(conn: Conn, user_id) -> Principal | None:
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


RESOLVED_GRANTS = (("image", "/placement/image"), ("cluster", "/placement/cluster"),
                   ("datastore", "/placement/datastore"), ("folder", "/placement/folder"),
                   ("network", "/placement/network"))


@rule(55, "R055", "write")
def resolution_admissible(ctx: Context) -> Verdict:
    """La résolution approuvée reste admissible ; on ne la recalcule jamais."""
    rev = ctx.revision
    if rev is None:
        return Verdict(False, "révision introuvable", "resolution_no_longer_admissible")
    tpl = ctx.conn.execute("SELECT slug, version, status FROM template_versions WHERE id = %s",
                           (rev["template_version_id"],)).fetchone()
    if tpl is None or tpl["status"] == "withdrawn":
        return Verdict(False, f"la version {tpl['version'] if tpl else '?'} du template a été retirée : "
                       "une nouvelle révision explicite est nécessaire", "resolution_no_longer_admissible")
    values = rev["resolved"]["values"]
    for kind, pointer in RESOLVED_GRANTS:
        stable_id = values[pointer]["value"]
        granted = ctx.conn.execute(
            "SELECT 1 FROM resource_grants WHERE project_id = %s AND environment_id = %s AND kind = %s "
            "AND stable_id = %s AND revoked_at IS NULL",
            (ctx.change["project_id"], rev["environment_id"], kind, stable_id)).fetchone()
        if granted is None:
            return Verdict(False, f"{values[pointer].get('label', stable_id)} n'est plus octroyé à cet environnement : "
                           "une nouvelle révision explicite est nécessaire", "resolution_no_longer_admissible")
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
    if plan["valid_until"] <= clock.now() + margin:
        return Verdict(False, "le plan a expiré", "plan_expired")
    return Verdict(True)


@rule(65, "R065", "write")
def approved_targets(ctx: Context) -> Verdict:
    """Une configuration ne vise que des machines de la liste figée à l'approbation (ou de la reprise)."""
    if ctx.job["kind"] != "ansible_configure":
        return Verdict(True)
    payload = ctx.job["payload"]
    if payload.get("waves"):
        names = set(payload["waves"][payload["wave"]])
    else:
        ids = list(payload.get("host_ids") or [])
        names = {
            r["name"] for r in ctx.conn.execute("SELECT name FROM hosts WHERE id = ANY(%s::uuid[])", (ids,)).fetchall()
        }
        if len(names) != len(ids):
            return Verdict(False, "cible de configuration inconnue", "target_not_approved")
    approved = set(ctx.plan["bundle"]["targets"]["instances"]) if ctx.plan else set()
    if ctx.approval is not None and ctx.approval["kind"] == "retry":
        scope = [str(i) for i in (ctx.approval["retry_scope"] or {}).get("host_ids", [])]
        approved &= {
            r["name"]
            for r in ctx.conn.execute("SELECT name FROM hosts WHERE id = ANY(%s::uuid[])", (scope,)).fetchall()
        }
    outside = sorted(names - approved)
    if outside:
        return Verdict(
            False,
            f"cible hors de la liste approuvée : {', '.join(outside)} (nouvelle approbation requise)",
            "target_not_approved",
        )
    return Verdict(True)


@rule(70, "R070", "all")
def cancel_not_requested(ctx: Context) -> Verdict:
    if ctx.job["cancel_requested_at"] is not None:
        return Verdict(False, "annulation demandée avant le démarrage", "cancel_requested")
    return Verdict(True)


@rule(80, "R080", "write", stages=("start", "dispatch"))
def environment_not_frozen(ctx: Context) -> Verdict:
    """Le verrou partagé sur l'environnement tient jusqu'à la fin de la transaction qui admet ou envoie."""
    if ctx.environment is None:
        return Verdict(True)
    frozen = freezes.active(ctx.conn, ctx.environment["id"], lock=True)
    if frozen:
        return Verdict(False, freezes.refusal(ctx.environment, frozen), "env_frozen")
    return Verdict(True)


@rule(90, "R090", "write", stages=("start", "dispatch"))
def within_change_calendar(ctx: Context) -> Verdict:
    """Fenêtre de maintenance ouverte et hors période de gel du calendrier (F27)."""
    if ctx.environment is None:
        return Verdict(True)
    state = calendars.writable(ctx.conn, ctx.environment["id"], clock.now())
    if state["ok"]:
        return Verdict(True)
    return Verdict(False, state["reason"], state["code"])


@rule(95, "R095", "write", stages=("start", "dispatch"))
def connector_circuit_closed(ctx: Context) -> Verdict:
    """Disjoncteur fermé sur chaque connecteur requis par le job d'écriture (F37)."""
    kinds = circuit.required_kinds(ctx.job["kind"])
    if not kinds:
        return Verdict(True)
    opened = circuit.open_connectors(ctx.conn, kinds, lock=True)
    if not opened:
        return Verdict(True)
    first = opened[0]
    return Verdict(False, circuit.refusal(first), "connector_circuit_open")


@rule(110, "R110", "write", stages=("start", "dispatch"))
def capacity_still_sufficient(ctx: Context) -> Verdict:
    """Capacité encore suffisante sur le placement ; mesure rejouée, jamais une réservation (F05)."""
    if ctx.job["kind"] not in ("tf_apply",):
        return Verdict(True)
    if ctx.revision is None:
        return Verdict(False, "révision introuvable pour le contrôle de capacité", "capacity_unknown")
    report = capacity_check.evaluate_resolution(ctx.conn, ctx.revision["resolved"], now=clock.now())
    if report["ok"]:
        return Verdict(True)
    refusal = report.get("refusal") or {}
    code = refusal.get("code") or "capacity_below_threshold"
    reason = refusal.get("reason") or "capacité insuffisante sur le placement"
    if refusal.get("target"):
        reason = f"{reason} (ressource : {refusal.get('resource')}, cible : {refusal['target']})"
    elif refusal.get("resource"):
        reason = f"{reason} (ressource : {refusal['resource']})"
    return Verdict(False, reason, code)


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


def evaluate(ctx: Context, stage: str = "start") -> dict[str, Any]:
    """Évalue les règles applicables dans l'ordre ; s'arrête au premier refus. Enregistre la décision."""
    if stage not in STAGES:
        raise ValueError(stage)
    results = []
    outcome = "admitted"
    refusal: dict[str, Any] | None = None
    for order, rule_id, job_class, stages, fn in REGISTRY:
        if job_class == "write" and ctx.job["class"] != "write":
            continue
        if stage not in stages:
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
        "rules, stage) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ctx.job["id"], ctx.change["id"] if ctx.change else None, outcome, active["version"], active["digest"],
         Jsonb(results), stage))
    return {"outcome": outcome, "rules": results, "refusal": refusal, "policy": active, "stage": stage}


def release_stack(conn: Conn, job: dict[str, Any]) -> None:
    if job.get("stack_id"):
        conn.execute("UPDATE stacks SET lock_job_id = NULL WHERE id = %s AND lock_job_id = %s",
                     (job["stack_id"], job["id"]))
