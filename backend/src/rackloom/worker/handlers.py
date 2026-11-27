"""Gestionnaires par type de job. Enchaînement d'une exécution :
preflight → tf_plan (F31) ; après approbation :
tf_apply → guest_ready → ansible_configure → verify, chaque job suivant étant créé dans la
transaction qui enregistre le succès du précédent. Les paramètres viennent exclusivement de
la révision approuvée.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import admission, artifacts, drift, hosts, ipam, registry, secrets
from ..canonical import digest, file_tree_digest
from ..changes.transitions import transition
from ..db import Conn, Jsonb
from ..executions import bundle as bundles
from ..executions import plans as plan_summary
from ..executions import preflight, queue
from ..projects import calendars
from .core import JobContext, Refused, read_json

TERRAFORM_BIN = "terraform"


IDENTITIES = {"configure": "rackloom-config", "collect": "rackloom-collect"}


def _load(conn: Conn, job: dict[str, Any], lock: bool = True) -> dict[str, Any]:
    ctx: dict[str, Any] = {"job": job}
    change = (
        conn.execute(
            f"SELECT * FROM changes WHERE id = %s{' FOR UPDATE' if lock else ''}", (job["change_id"],)
        ).fetchone()
        if job["change_id"]
        else None
    )
    ctx["change"] = change
    ctx["env"] = (
        conn.execute("SELECT * FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
        if change
        else None
    )
    ctx["project"] = conn.execute("SELECT * FROM projects WHERE id = %s", (job["project_id"],)).fetchone()
    ctx["deployment"] = (
        conn.execute("SELECT * FROM deployments WHERE id = %s", (change["deployment_id"],)).fetchone()
        if change and change["deployment_id"]
        else None
    )
    stack_id = job["stack_id"] or (ctx["deployment"]["stack_id"] if ctx["deployment"] else None)
    ctx["stack"] = conn.execute("SELECT * FROM stacks WHERE id = %s", (stack_id,)).fetchone() if stack_id else None
    ctx["execution"] = (
        conn.execute("SELECT * FROM executions WHERE id = %s", (job["execution_id"],)).fetchone()
        if job["execution_id"]
        else None
    )
    ctx["plan"] = (
        conn.execute("SELECT * FROM plans WHERE id = %s", (ctx["execution"]["plan_id"],)).fetchone()
        if ctx["execution"]
        else None
    )
    approval_id = job["payload"].get("retry_approval_id") or (
        ctx["execution"]["approval_id"] if ctx["execution"] else None
    )
    ctx["approval"] = (
        conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone() if approval_id else None
    )
    revision_no = ctx["plan"]["revision_no"] if ctx["plan"] else job["payload"].get("revision_no")
    if revision_no is None and change is not None:
        revision_no = change["current_revision_no"]
    ctx["revision"] = (
        conn.execute(
            "SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s", (change["id"], revision_no)
        ).fetchone()
        if change
        else None
    )
    ctx["version"] = (
        conn.execute(
            "SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
            (change["id"], ctx["revision"]["version_no"]),
        ).fetchone()
        if ctx["revision"]
        else None
    )
    ctx["resolution"] = ctx["revision"]["resolved"] if ctx["revision"] else None
    return ctx


def _admit(r: JobContext, conn: Conn, ctx: dict[str, Any], recompute=None, stage: str = "start") -> None:
    decision = admission.evaluate(
        admission.Context(
            conn=conn,
            job=r.job,
            change=ctx["change"],
            environment=ctx["env"],
            execution=ctx["execution"],
            approval=ctx["approval"],
            plan=ctx["plan"],
            revision=ctx["revision"],
            recompute_digest=recompute,
        ),
        stage=stage,
    )
    label = (
        f"Admission (règles {decision['policy']['version']})"
        if stage == "start"
        else "Contrôle avant envoi au fournisseur"
    )
    if decision["outcome"] == "admitted":
        if stage == "start":
            r.fenced(conn, "state = 'running', blocked_reason = NULL")
        r.step(
            conn,
            label,
            "succeeded",
            summary={"rules": [x["id"] for x in decision["rules"]], "policy": decision["policy"]["version"]},
        )
        return
    refusal = decision["refusal"]
    r.fenced(
        conn, "state = 'blocked', blocked_reason = %s, finished_at = now(), lease_owner = NULL", (refusal["code"],)
    )
    admission.release_stack(conn, r.job)
    r.step(conn, label, "failed", summary={"rule": refusal["id"], "reason": refusal["reason"]})
    field = refusal.get("field")
    r.event(
        conn,
        "job.blocked",
        {
            "kind": r.job["kind"],
            "rule": refusal["id"],
            "code": refusal["code"],
            "field": field,
            "stage": stage,
            "message": f"Admission refusée ({refusal['id']}"
            + (f", champ {field}" if field else "")
            + f") : {refusal['reason']}",
        },
        severity="error",
    )
    change = ctx["change"]
    if change is not None:
        target = {
            "planning": "plan_failed",
            "approved": "plan_expired"
            if refusal["code"] == "plan_expired"
            else ("cancelled" if refusal["code"] == "cancel_requested" else "blocked"),
        }.get(change["state"])
        if change["state"] in ("executing", "verifying"):
            target = (
                "cancelled"
                if refusal["code"] == "cancel_requested" and r.job["kind"] == "tf_apply"
                else "partially_succeeded"
            )
        if target:
            if target == "plan_expired" and ctx["plan"]:
                conn.execute(
                    "UPDATE plans SET status = 'expired' WHERE id = %s AND status = 'ready'", (ctx["plan"]["id"],)
                )
            transition(
                conn,
                change,
                target,
                reason_code=f"admission_{refusal['code']}",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"rule": refusal["id"], "message": refusal["reason"]},
                severity="error",
            )
    raise Refused(refusal["code"])


def _prepare_tf(r: JobContext, run_id: str, resolution: dict[str, Any], job_json: dict[str, Any]) -> Path:
    work = r.workspace(run_id)
    tf = resolution["terraform"]
    src = r.settings.catalog_dir / "terraform" / tf["module"] / tf["version"]
    if file_tree_digest(src, ("*.tf", ".terraform.lock.hcl")) != tf["digest"]:
        raise RuntimeError("module Terraform modifié sur disque depuis son chargement")
    tfdir = work / "tf"
    tfdir.mkdir(exist_ok=True)
    for f in list(src.glob("*.tf")) + [src / ".terraform.lock.hcl"]:
        shutil.copy2(f, tfdir / f.name)
    (work / "job.json").write_text(json.dumps({"terraform_bin": TERRAFORM_BIN, **job_json}), encoding="utf-8")
    return work


def _lab_delay(conn: Conn) -> int:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_delay_seconds'").fetchone()
    return int(row["value"]) if row else 0


def _store_log(r: JobContext, conn: Conn, work: Path, name: str) -> dict[str, Any] | None:
    path = work / "out" / name
    if not path.exists():
        return None
    return artifacts.store(
        conn,
        project_id=r.job["project_id"],
        change_id=r.job["change_id"],
        kind=name.replace(".", "_"),
        key_class="A",
        plaintext=r.redactor.bytes(path.read_bytes()),
    )


def handle_preflight(r: JobContext) -> None:
    """F31 : contrôles en lecture seule avant le plan ; aucun changement sur la cible."""
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["resolution"]
        stack = ctx["stack"]
        result = preflight.run(conn, resolution=resolution, stack=stack, settings=r.settings)
        step_names = {
            "vcenter": "Préflight : accès vCenter",
            "state": "Préflight : backend de state",
            "template": "Préflight : prérequis du template",
            "repos": "Préflight : dépôts et rôles",
        }
        for check in result.checks:
            r.step(
                conn,
                step_names.get(check.name, f"Préflight : {check.name}"),
                "succeeded" if check.ok else "failed",
                simulated=check.simulated,
                summary={"code": check.code, "reason": check.message} if not check.ok else {"ok": True},
            )
        if not result.ok:
            code = result.code or "template_prereq_missing"
            r.fenced(
                conn,
                "state = 'failed', finished_at = now(), result = %s",
                (Jsonb({"code": code, "checks": result.as_dict()["checks"]}),),
            )
            r.event(
                conn,
                "job.failed",
                {"kind": "preflight", "code": code, "message": result.message},
                severity="error",
            )
            change = ctx["change"]
            transition(
                conn,
                change,
                "plan_failed",
                reason_code=code,
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"message": result.message, "code": code},
                severity="error",
            )
            return
        # Enfiler tf_plan seulement après un préflight réussi (même révision / payload).
        payload = dict(r.job["payload"] or {})
        plan_job = queue.enqueue(
            conn,
            kind="tf_plan",
            change=ctx["change"],
            stack_id=stack["id"],
            idempotency_key=(
                f"tf_plan:{r.job['change_id']}:{payload.get('revision_no')}:{ctx['change']['row_version']}"
            ),
            payload=payload,
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({"ok": True, "tf_plan_job_id": str(plan_job["id"]), "checks": result.as_dict()["checks"]}),),
        )
        r.event(
            conn,
            "job.succeeded",
            {"kind": "preflight", "message": result.message, "next": "tf_plan"},
        )


def handle_tf_plan(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["resolution"]
        variables = bundles.tf_variables(resolution, ctx["stack"]["slug"], _lab_delay(conn))
    run_id = f"{r.job['id']}-a{r.job['attempts']}"
    work = _prepare_tf(r, run_id, resolution, {"action": "tf_plan", "variables": variables, "timeout_seconds": 600})
    status = r.run({"run_id": run_id, "kind": "terraform", "stack": ctx["stack"]["slug"], "timeout_seconds": 600})
    out = work / "out"
    rc = status.get("exit_code")
    plan_json = read_json(out / "plan.json")
    if rc != 0 or plan_json is None or not (out / "plan.bin").exists():
        with r.tx() as conn:
            _store_log(r, conn, work, "terraform.log")
            r.fenced(conn, "state = 'failed', finished_at = now(), result = %s", (Jsonb({"exit_code": rc}),))
            change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
            transition(
                conn,
                change,
                "plan_failed",
                reason_code="terraform_plan_failed",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"exit_code": rc},
                severity="error",
            )
        r.cleanup(run_id)
        return
    plan_bytes = (out / "plan.bin").read_bytes()
    state_before = read_json(out / "state_before.json", {}) or {}
    summary = plan_summary.summarize(plan_json)
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        art = artifacts.store(
            conn,
            project_id=r.job["project_id"],
            change_id=r.job["change_id"],
            kind="tf_plan_bin",
            key_class="A",
            plaintext=plan_bytes,
        )
        _store_log(r, conn, work, "terraform.log")
        valid_until = datetime.now(UTC) + timedelta(seconds=ctx["env"]["plan_ttl_seconds"])
        bundle = bundles.build(
            conn,
            change=ctx["change"],
            version=ctx["version"],
            revision=ctx["revision"],
            stack=ctx["stack"],
            plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            state_lineage=state_before.get("lineage"),
            state_serial=state_before.get("serial"),
            valid_until=valid_until.isoformat(),
            terraform_version=summary["terraform_version"],
        )
        status_ = (
            "blocked_destructive" if summary["destructive"] else ("no_changes" if summary["no_changes"] else "ready")
        )
        plan_id = queue.new_id()
        conn.execute(
            """
            INSERT INTO plans (id, change_id, version_no, revision_no, stack_id, job_id, status, plan_sha256,
                               artifact_id, summary, counts, valid_until, bundle, bundle_digest, state_lineage,
                               state_serial)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                plan_id,
                r.job["change_id"],
                ctx["revision"]["version_no"],
                ctx["revision"]["revision_no"],
                ctx["stack"]["id"],
                r.job["id"],
                status_,
                art["sha256"],
                art["id"],
                Jsonb(summary),
                Jsonb(summary["vm_counts"]),
                valid_until,
                Jsonb(bundle),
                digest(bundle),
                state_before.get("lineage"),
                state_before.get("serial"),
            ),
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({"plan_id": str(plan_id), "status": status_}),),
        )
        counts = summary["vm_counts"]
        if r.job["payload"].get("replan") and summary["no_changes"]:
            transition(
                conn,
                ctx["change"],
                "succeeded",
                reason_code="no_changes",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"message": "Aucun changement : l'infrastructure correspond déjà à la demande"},
            )
        else:
            transition(
                conn,
                ctx["change"],
                "plan_ready",
                reason_code="plan_ready",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={
                    "plan_id": str(plan_id),
                    "status": status_,
                    "counts": counts,
                    "bundle_digest": digest(bundle)[:12],
                },
                severity="warning" if status_ == "blocked_destructive" else "info",
            )
    r.cleanup(run_id)


def handle_tf_apply(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        plan = ctx["plan"]
        plan_art = conn.execute("SELECT * FROM artifacts WHERE id = %s", (plan["artifact_id"],)).fetchone()
        plan_bytes = artifacts.read(plan_art)
        plan_sha = hashlib.sha256(plan_bytes).hexdigest()

        def recompute() -> str:
            if plan_sha != plan["plan_sha256"]:
                raise bundles.BundleError("plan_altered", "le plan binaire ne correspond plus à son empreinte")
            return bundles.recompute_digest(
                conn, plan, ctx["change"], ctx["version"], ctx["revision"], ctx["stack"], plan_sha
            )

        _admit(r, conn, ctx, recompute)
        resolution = ctx["resolution"]
    run_id = str(r.job["id"])
    work = _prepare_tf(r, run_id, resolution, {"action": "tf_apply", "timeout_seconds": 1800})
    (work / "in").mkdir(exist_ok=True)
    (work / "in" / "plan.bin").write_bytes(plan_bytes)
    if hashlib.sha256((work / "in" / "plan.bin").read_bytes()).hexdigest() != plan["plan_sha256"]:
        raise RuntimeError("copie du plan altérée")
    try:
        with r.tx() as conn:
            current = _load(conn, r.job)
            _admit(r, conn, current, stage="dispatch")
            r.fenced(conn, "dispatched_at = now(), provider_ref = %s", (run_id,))
            conn.execute(
                "UPDATE executions SET started_at = coalesce(started_at, now()), state = 'running' WHERE id = %s",
                (r.job["execution_id"],),
            )
            transition(
                conn,
                current["change"],
                "executing",
                reason_code="apply_dispatched",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"plan_id": str(plan["id"])},
            )
    except Refused:
        r.cleanup(run_id)
        raise
    status = r.run({"run_id": run_id, "kind": "terraform", "stack": ctx["stack"]["slug"], "timeout_seconds": 1800})
    finish_apply(r, run_id, status)


def _vms_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    vms: dict[str, dict[str, Any]] = {}
    for res in (state or {}).get("resources", []):
        address = res["address"]
        if "[" not in address:
            continue
        name = address.split('["', 1)[1].rstrip('"]')
        vm = vms.setdefault(name, {"name": name})
        if res["type"] == "random_uuid":
            vm["uuid"] = res["id"]
        if res["type"] == "local_file":
            vm["file_sha256"] = res.get("content_sha256")
    return [vms[k] for k in sorted(vms) if "uuid" in vms[k]]


def _record_instances(
    conn: Conn, change: dict[str, Any], resolution: dict[str, Any], vms: list[dict[str, Any]]
) -> None:
    """Le déploiement reflète l'état observé (state), pas l'intention : seules les instances présentes."""
    from ..deployments import outputs as deployment_outputs

    present = {v["name"]: v for v in vms}
    instances = []
    for i in resolution["instances"]:
        if i["name"] not in present:
            continue
        row = {
            "name": i["name"],
            "size_tier": i["size_tier"],
            "vcpu": i["vcpu"],
            "memory_mb": i["memory_mb"],
            "disk_gb": i["disk_gb"],
            "vm_uuid": present[i["name"]]["uuid"],
        }
        if i.get("member"):
            row["member"] = i["member"]
        instances.append(row)
    if change["deployment_id"]:
        conn.execute(
            "UPDATE deployments SET instances = %s, status = CASE WHEN %s THEN 'active' ELSE status END WHERE id = %s",
            (Jsonb(instances), bool(instances), change["deployment_id"]),
        )
        # F33 : publie les sorties allowlistées (jamais le state brut exposé à l'API).
        if instances:
            dep = conn.execute(
                "SELECT project_id, template_version_id FROM deployments WHERE id = %s",
                (change["deployment_id"],),
            ).fetchone()
            if dep is not None:
                deployment_outputs.publish(
                    conn,
                    deployment_id=change["deployment_id"],
                    project_id=dep["project_id"],
                    template_version_id=dep["template_version_id"],
                    instances=instances,
                )


def finish_apply(r: JobContext, run_id: str, status: dict[str, Any], *, job: dict[str, Any] | None = None) -> str:
    """Conclut un tf_apply à partir des sorties du runner. `job` : job incertain repris par réconciliation."""
    target = job or r.job
    work = r.settings.workspaces_dir / run_id
    out = work / "out"
    rc = status.get("exit_code")
    state_after = read_json(out / "state_after.json")
    log_path = out / "terraform.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    stale = "Saved plan is stale" in log_text
    vms = _vms_from_state(state_after)
    outcome = "applied" if rc == 0 else ("stale" if stale else "failed")
    with r.tx() as conn:
        _store_log(r, conn, work, "terraform.log")
        cur = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (target["id"],)).fetchone()
        cancel = cur["cancel_requested_at"] is not None
        if outcome != "applied" and cancel:
            outcome = "cancelled" if not vms else "cancelled_partial"
        result = {"exit_code": rc, "vms": vms, "outcome": outcome}
        if job is None:
            r.fenced(
                conn,
                "state = %s, finished_at = now(), result = %s",
                ("succeeded" if outcome == "applied" else "failed", Jsonb(result)),
            )
        else:
            conn.execute(
                "UPDATE jobs SET state = %s, finished_at = now(), result = %s, updated_at = now() "
                "WHERE id = %s AND state = 'uncertain'",
                ("succeeded" if outcome == "applied" else "failed", Jsonb(result), target["id"]),
            )
        admission.release_stack(conn, target)
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (target["change_id"],)).fetchone()
        ctx = _load(conn, target, lock=False)
        plan_id = ctx["plan"]["id"]
        if state_after and state_after.get("exists"):
            conn.execute(
                "UPDATE stacks SET last_lineage = %s, last_serial = %s WHERE id = %s",
                (state_after.get("lineage"), state_after.get("serial"), target["stack_id"]),
            )
        if outcome in ("applied", "failed", "cancelled_partial"):
            _record_instances(conn, change, ctx["resolution"], vms)
        if change["state"] == "reconciling":
            change = transition(
                conn,
                change,
                "executing",
                reason_code="reconciled",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"outcome": outcome},
            )
        if outcome == "applied":
            conn.execute("UPDATE plans SET status = 'applied' WHERE id = %s", (plan_id,))
            # F03 : confirmer les réservations IPAM après application réussie.
            for reservation in ipam.for_change(conn, change["id"]):
                if reservation["state"] in ("reserved", "held_uncertain"):
                    ipam.confirm(conn, UUID(reservation["id"]), job_id=target["id"])
            r.event(
                conn,
                "execution.applied",
                {
                    "message": f"Plan appliqué : {len(vms)} ressource(s) de labo présentes",
                    "vms": [v["name"] for v in vms],
                },
            )
            queue.enqueue(
                conn,
                kind="guest_ready",
                change=change,
                execution_id=target["execution_id"],
                stack_id=target["stack_id"],
                idempotency_key=f"guest_ready:{target['execution_id']}",
                payload={"vms": vms},
            )
        elif outcome == "stale":
            conn.execute("UPDATE plans SET status = 'expired' WHERE id = %s", (plan_id,))
            transition(
                conn,
                change,
                "plan_expired",
                reason_code="saved_plan_stale",
                actor_type="worker",
                actor_id=r.worker_id,
                severity="error",
                detail={
                    "message": "Terraform a refusé le plan : le state a changé depuis le calcul du "
                    "plan. Nouveau plan et nouvelle approbation nécessaires."
                },
            )
        elif outcome == "cancelled":
            conn.execute("UPDATE plans SET status = 'failed' WHERE id = %s", (plan_id,))
            transition(
                conn,
                change,
                "cancelled",
                reason_code="cancelled_before_resources",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"message": "Annulation effective : aucune ressource créée"},
            )
        else:
            conn.execute("UPDATE plans SET status = 'failed' WHERE id = %s", (plan_id,))
            message = (
                (
                    f"Annulation demandée : application interrompue, {len(vms)} ressource(s) présentes, "
                    "aucune suppression"
                )
                if outcome == "cancelled_partial"
                else f"Échec de l'application (code {rc}) : {len(vms)} ressource(s) présentes, aucune suppression"
            )
            transition(
                conn,
                change,
                "failed",
                reason_code=f"apply_{outcome}",
                actor_type="worker",
                actor_id=r.worker_id,
                severity="error",
                detail={"message": message, "vms": len(vms)},
            )
            if change["deployment_id"]:
                conn.execute(
                    "UPDATE deployments SET service_status = 'failed', service_reason = %s, "
                    "service_checked_at = now() WHERE id = %s AND status <> 'active'",
                    (message, change["deployment_id"]),
                )
        conn.execute("UPDATE executions SET state = %s WHERE id = %s", (outcome, target["execution_id"]))
    r.cleanup(run_id)
    return outcome


def _lab_keys(conn: Conn, purpose: str) -> tuple[str, str]:
    identity = IDENTITIES[purpose]
    pubs = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_ssh_public_keys'").fetchone()
    if pubs is None or identity not in pubs["value"]:
        raise RuntimeError(f"clé SSH de l'identité {identity} non importée (make lab-keys)")
    private = secrets.reveal(conn, f"secret://lab/ssh/{identity}").decode()
    return pubs["value"][identity], private


def _prepare_ansible(
    r: JobContext, conn: Conn, run_id: str, resolution: dict[str, Any], purpose: str, targets: list[dict[str, Any]]
) -> tuple[Path, str]:
    work = r.workspace(run_id)
    private_dir = work / "ansible"
    for sub in ("env", "inventory", "project"):
        (private_dir / sub).mkdir(parents=True, exist_ok=True)
    (private_dir / "env" / "known_hosts").write_text(hosts.known_hosts(conn, [h["address"] for h in targets]))
    identity = IDENTITIES[purpose]
    inventory = {
        "all": {
            "children": {
                "enrolled": {
                    "hosts": {
                        h["name"]: {
                            "ansible_host": h["address"],
                            "ansible_user": identity,
                            "ansible_python_interpreter": "/usr/bin/python3",
                        }
                        for h in targets
                    }
                }
            }
        }
    }
    (private_dir / "inventory" / "hosts.json").write_text(json.dumps(inventory))
    pb = resolution["ansible"]["playbooks"][purpose]
    pb_dir = r.settings.catalog_dir / "ansible" / "playbooks" / purpose / pb["version"]
    if file_tree_digest(pb_dir) != pb["digest"]:
        raise RuntimeError("playbook modifié sur disque depuis son chargement")
    shutil.copy2(pb_dir / f"{purpose}.yml", private_dir / "project" / f"{purpose}.yml")
    from .. import composition as composition_mod

    host_comps = composition_mod.components_for_hosts(resolution, targets)
    if purpose == "configure":
        roles = [c["role"] for c in host_comps]
        # Déduplique les rôles tout en conservant ceux du membre ciblé uniquement.
        seen_roles: set[tuple[str, str]] = set()
        for role in roles:
            key = (role["name"], role["version"])
            if key in seen_roles:
                continue
            seen_roles.add(key)
            src = r.settings.catalog_dir / "ansible" / "roles" / role["name"] / role["version"]
            if file_tree_digest(src) != role["digest"]:
                raise RuntimeError(f"rôle {role['name']} modifié sur disque depuis son chargement")
            shutil.copytree(src, private_dir / "project" / "roles" / role["name"], dirs_exist_ok=True)
        (private_dir / "env" / "extravars").write_text(
            json.dumps({"rackloom_components": bundles.ansible_components_list(host_comps)})
        )
    else:
        # F02 : union des contrôles de tous les membres du lot (vague courante + antérieures).
        host_checks = composition_mod.collect_checks_for_hosts(resolution, targets)
        (private_dir / "env" / "extravars").write_text(
            json.dumps({"rackloom_collect": registry.collect_spec(host_comps, host_checks)})
        )
    (private_dir / "project" / "ansible.cfg").write_text(
        "[defaults]\nhost_key_checking = True\nroles_path = roles\nretry_files_enabled = False\n"
        "interpreter_python = /usr/bin/python3\n[ssh_connection]\npipelining = True\n"
    )
    pub, private = _lab_keys(conn, purpose)
    r.redactor = type(r.redactor)([private, *r.redactor.known])
    (work / "job.json").write_text(
        json.dumps(
            {"action": "ansible_play", "playbook": f"{purpose}.yml", "ssh_public_key": pub, "timeout_seconds": 900}
        )
    )
    return work, json.dumps({"ssh_private_key": private})


def _ansible_run(r: JobContext, run_id: str, kind: str, stdin: str) -> tuple[int | None, dict[str, Any]]:
    status = r.run({"run_id": run_id, "kind": kind, "timeout_seconds": 900, "stdin_secret": stdin})
    summary = read_json(r.settings.workspaces_dir / run_id / "out" / "ansible_summary.json", {}) or {}
    return status.get("exit_code"), summary


def _store_ansible_report(
    r: JobContext, conn: Conn, run: str, rc: int | None, summary: dict[str, Any]
) -> dict[str, Any]:
    """Rapport filtré de classe B (téléchargeable par un membre) : compteurs et tâches, messages masqués."""
    hosts_report = {
        name: {
            **{k: h.get(k) for k in ("ok", "changed", "failures", "dark", "skipped")},
            "tasks": [{k: t.get(k) for k in ("name", "status", "msg")} for t in h.get("tasks", [])],
        }
        for name, h in summary.get("hosts", {}).items()
    }
    report = {"run": run, "exit_code": rc, "hosts": hosts_report}
    return artifacts.store(
        conn,
        project_id=r.job["project_id"],
        change_id=r.job["change_id"],
        kind=f"ansible_{run}_report",
        key_class="B",
        plaintext=r.redactor.bytes(json.dumps(report, ensure_ascii=False).encode()),
    )


def _host_ok(summary: dict[str, Any], name: str) -> tuple[bool, str]:
    h = summary.get("hosts", {}).get(name)
    if h is None:
        return False, "aucun résultat"
    if h.get("dark"):
        msgs = [t["msg"] for t in h.get("tasks", []) if t["status"] == "unreachable"]
        reason = msgs[-1] if msgs else "hôte injoignable"
        if "host key" in reason.lower() or "REMOTE HOST IDENTIFICATION" in reason:
            reason = "clé d'hôte SSH différente de l'ancre de confiance : connexion refusée"
        return False, reason[:300]
    if h.get("failures"):
        msgs = [t["msg"] for t in h.get("tasks", []) if t["status"] == "failed"]
        return False, (msgs[-1] if msgs else "échec d'une tâche")[:300]
    return True, ""


def handle_guest_ready(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["resolution"]
        dep = ctx["deployment"]
        nodes = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_nodes'").fetchone()
        nodes = nodes["value"] if nodes else []
        used = {row["address"] for row in conn.execute("SELECT address FROM hosts WHERE address IS NOT NULL")}
        free = [n for n in nodes if n["address"] not in used]
        targets = []
        mapping = []
        approved = set(_approved_names(ctx))
        for vm in r.job["payload"]["vms"]:
            if vm["name"] not in approved:
                r.step(
                    conn,
                    f"VM hors de la liste approuvée ignorée ({vm['name']})",
                    "skipped",
                    summary={"host": vm["name"], "reason": "nouvelle approbation requise"},
                )
                continue
            host = conn.execute(
                "SELECT * FROM hosts WHERE deployment_id = %s AND name = %s", (dep["id"], vm["name"])
            ).fetchone()
            if host is not None and host["deployment_state"] in ("configured", "verified", "verify_failed"):
                continue
            if host is None:
                node = free.pop(0) if free else None
                member = next(
                    (i.get("member") for i in resolution.get("instances") or [] if i.get("name") == vm["name"]),
                    None,
                )
                host = conn.execute(
                    "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
                    "stack_id, vm_stable_id, name, address, deployment_state, composition_member) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        queue.new_id(),
                        r.job["project_id"],
                        dep["environment_id"],
                        dep["id"],
                        dep["template_version_id"],
                        r.job["change_id"],
                        dep["stack_id"],
                        f"vm-lab-{vm['uuid'][:8]}",
                        vm["name"],
                        node["address"] if node else None,
                        "created" if node else "no_address",
                        member,
                    ),
                ).fetchone()
            mapping.append({"vm": vm["name"], "node": host["address"]})
            if host["address"]:
                targets.append(host)
        r.step(
            conn,
            "Personnalisation invitée (vCenter fictif)",
            "succeeded",
            simulated=True,
            summary={"vms": [m["vm"] for m in mapping]},
        )
        r.step(
            conn,
            "Attribution d'adresse : association à un nœud de labo",
            "succeeded",
            simulated=True,
            summary={"mapping": mapping},
        )
        anchored = [h for h in targets if hosts.anchor_for(conn, h["address"])]
        for h in targets:
            if h not in anchored:
                conn.execute("UPDATE hosts SET deployment_state = 'unreachable' WHERE id = %s", (h["id"],))
                hosts.observe(
                    conn,
                    h,
                    "ssh",
                    {"error": "aucune ancre de clé d'hôte importée pour cette adresse"},
                    "unreachable",
                    "worker",
                    r.job["id"],
                )
        stdin = None
        if anchored:
            work, stdin = _prepare_ansible(
                r, conn, f"{r.job['id']}-a{r.job['attempts']}", resolution, "collect", anchored
            )
    enrolled: list[dict[str, Any]] = []
    if anchored:
        run_id = f"{r.job['id']}-a{r.job['attempts']}"
        rc, summary = _ansible_run(r, run_id, "ansible", stdin)
        with r.tx() as conn:
            for h in anchored:
                ok, reason = _host_ok(summary, h["name"])
                if ok:
                    anchor = hosts.anchor_for(conn, h["address"])
                    conn.execute(
                        "UPDATE hosts SET deployment_state = 'enrolled', enrolled_at = now(), "
                        "host_key_fingerprint = %s WHERE id = %s",
                        (anchor["fingerprint"], h["id"]),
                    )
                    report = summary.get("reports", {}).get(h["name"], {})
                    hosts.observe(
                        conn, h, "ssh", {"fingerprint": anchor["fingerprint"]}, "ok", "rackloom-collect", r.job["id"]
                    )
                    hosts.observe(
                        conn,
                        h,
                        "facts",
                        {"os": report.get("os"), "kernel": report.get("kernel"), "init": report.get("init")},
                        "ok",
                        "rackloom-collect",
                        r.job["id"],
                        r.redactor,
                    )
                    enrolled.append(h)
                else:
                    conn.execute("UPDATE hosts SET deployment_state = 'unreachable' WHERE id = %s", (h["id"],))
                    hosts.observe(
                        conn, h, "ssh", {"error": reason}, "unreachable", "rackloom-collect", r.job["id"], r.redactor
                    )
                r.step(
                    conn,
                    f"Connexion SSH vérifiée ({h['name']})",
                    "succeeded" if ok else "failed",
                    summary={"host": h["name"], **({"reason": reason} if not ok else {})},
                )
        r.cleanup(run_id)
    with r.tx() as conn:
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({"enrolled": [h["name"] for h in enrolled]}),),
        )
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        _exclude_unapproved(r, conn, ctx)
        _start_wave(r, conn, change, _approved_waves(ctx), 0, {})


def _approved_names(ctx: dict[str, Any]) -> list[str]:
    """Cibles figées par l'approbation (dossier d'exécution), à défaut la révision approuvée."""
    if ctx["plan"] is not None:
        return list(ctx["plan"]["bundle"]["targets"]["instances"])
    return [i["name"] for i in ctx["resolution"]["instances"]]


def _approved_waves(ctx: dict[str, Any]) -> list[list[str]]:
    if ctx["plan"] is not None and "waves" in ctx["plan"]["bundle"]["targets"]:
        return [list(w) for w in ctx["plan"]["bundle"]["targets"]["waves"]]
    return [_approved_names(ctx)]


def _exclude_unapproved(r: JobContext, conn: Conn, ctx: dict[str, Any]) -> None:
    """Une machine rattachée au déploiement après l'approbation n'est jamais configurée en silence."""
    approved = _approved_names(ctx)
    rows = conn.execute(
        "UPDATE hosts SET deployment_state = 'out_of_scope' WHERE deployment_id = %s "
        "AND NOT (name = ANY(%s)) AND deployment_state NOT IN ('configured', 'verified', 'verify_failed', "
        "'out_of_scope') RETURNING name",
        (ctx["deployment"]["id"], approved),
    ).fetchall()
    for row in rows:
        r.step(
            conn,
            f"Hors de la liste approuvée : {row['name']} non configurée",
            "skipped",
            summary={"host": row["name"], "reason": "nouvelle approbation requise"},
        )
        r.event(
            conn,
            "deployment.target_excluded",
            {
                "host": row["name"],
                "message": f"{row['name']} n'appartient pas à la liste approuvée : aucune "
                "configuration sans nouvelle approbation",
            },
            severity="warning",
        )


CONFIGURABLE = ("enrolled", "config_failed", "configured", "verified", "verify_failed", "wave_blocked")


def _wave_label(waves: list[list[str]], index: int) -> str:
    return "pilote" if index == 0 else f"vague {index + 1}"


def _wave_of(index: int) -> str:
    return "du pilote" if index == 0 else f"de la vague {index + 1}"


def _wave_verified(index: int) -> str:
    return "Pilote vérifié" if index == 0 else f"Vague {index + 1} vérifiée"


def _start_wave(
    r: JobContext, conn: Conn, change: dict[str, Any], waves: list[list[str]], index: int, extra: dict[str, Any]
) -> None:
    """Configure la vague `index` ; sans machine joignable dans la vague, passe à sa vérification."""
    dep_id = change["deployment_id"]
    names = waves[index] if waves else []
    ready = conn.execute(
        "SELECT name FROM hosts WHERE deployment_id = %s AND name = ANY(%s) AND deployment_state = ANY(%s)",
        (dep_id, names, list(CONFIGURABLE)),
    ).fetchall()
    payload = {"waves": waves, "wave": index, **extra}
    suffix = "" if index == 0 else f":w{index}"
    if extra.get("retry_approval_id"):
        suffix = f":retry:{extra['retry_approval_id']}:w{index}"
    if len(waves) > 1:
        r.event(
            conn,
            "deployment.wave_started",
            {
                "wave": index + 1,
                "hosts": names,
                "message": f"{_wave_label(waves, index).capitalize()} : {', '.join(names)}",
            },
        )
    if ready:
        run_after = calendars.schedule_run_after(conn, change["environment_id"])
        queue.enqueue(
            conn,
            kind="ansible_configure",
            change=change,
            execution_id=r.job["execution_id"],
            idempotency_key=f"ansible_configure:{r.job['execution_id']}{suffix}",
            payload=payload,
            run_after=run_after,
        )
    else:
        queue.enqueue(
            conn,
            kind="verify",
            change=change,
            execution_id=r.job["execution_id"],
            idempotency_key=f"verify:{r.job['id']}{suffix}",
            payload=payload,
        )


def _configure_targets(conn: Conn, job: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    payload = job["payload"]
    if payload.get("waves"):
        return list(
            conn.execute(
                "SELECT * FROM hosts WHERE deployment_id = %s AND name = ANY(%s) "
                "AND deployment_state = ANY(%s) ORDER BY name",
                (ctx["deployment"]["id"], payload["waves"][payload["wave"]], list(CONFIGURABLE)),
            ).fetchall()
        )
    ids = [UUID(i) for i in payload["host_ids"]]
    return list(
        conn.execute(
            "SELECT * FROM hosts WHERE id = ANY(%s) AND deployment_id = %s ORDER BY name",
            (ids, ctx["deployment"]["id"]),
        ).fetchall()
    )


def _record_configure(
    r: JobContext,
    conn: Conn,
    job: dict[str, Any],
    targets: list[dict[str, Any]],
    resolution: dict[str, Any],
    rc: int | None,
    summary: dict[str, Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """Enregistre le rapport configure et met à jour les hôtes. Retourne ceux configurés avec succès."""
    _store_ansible_report(r, conn, "configure", rc, summary)
    payload = job["payload"]
    waves = payload.get("waves") or []
    prefix = f"{_wave_label(waves, payload['wave']).capitalize()} · " if len(waves) > 1 else ""
    configured: list[dict[str, Any]] = []
    for h in targets:
        ok, reason = _host_ok(summary, h["name"])
        conn.execute(
            "UPDATE hosts SET deployment_state = %s WHERE id = %s", ("configured" if ok else "config_failed", h["id"])
        )
        from .. import composition as composition_mod

        host_comps = composition_mod.components_for_hosts(resolution, [h])
        comps = ", ".join(f"{c['slug']} {c['version']}" for c in host_comps)
        member_label = f" [{h['composition_member']}]" if h.get("composition_member") else ""
        host_stats = summary.get("hosts", {}).get(h["name"]) or {}
        # Ansible omet l'hôte du compteur `changed` quand rien n'a changé.
        changed = host_stats.get("changed", 0)
        r.step(
            conn,
            f"{prefix}Configuration {comps} ({h['name']}{member_label})",
            "succeeded" if ok else "failed",
            summary={
                "host": h["name"],
                **({"member": h["composition_member"]} if h.get("composition_member") else {}),
                **({"changed": changed} if ok else {}),
                **({"reason": reason} if not ok else {}),
            },
        )
        if ok:
            configured.append(h)
    result = {"configured": [h["name"] for h in configured], "exit_code": rc, "outcome": "configured", "source": source}
    if source == "workspace":
        conn.execute(
            "UPDATE jobs SET state = 'succeeded', finished_at = now(), result = %s, updated_at = now() "
            "WHERE id = %s AND state = 'uncertain'",
            (Jsonb(result), job["id"]),
        )
    else:
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s", (Jsonb(result),))
    return configured


def handle_ansible_configure(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["resolution"]
        payload = r.job["payload"]
        targets = _configure_targets(conn, r.job, ctx)
        targets = [h for h in targets if h["address"] and hosts.anchor_for(conn, h["address"])]
        run_id = str(r.job["id"])
        work, stdin = _prepare_ansible(r, conn, run_id, resolution, "configure", targets)
        r.fenced(conn, "dispatched_at = now(), provider_ref = %s", (run_id,))
    rc, summary = _ansible_run(r, run_id, "ansible", stdin)
    with r.tx() as conn:
        _record_configure(r, conn, r.job, targets, resolution, rc, summary, source="runner")
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        queue.enqueue(
            conn,
            kind="verify",
            change=change,
            execution_id=r.job["execution_id"],
            idempotency_key=f"verify:{r.job['id']}",
            payload={k: v for k, v in payload.items() if k in ("waves", "wave", "retry_approval_id")},
        )
    r.cleanup(run_id)


def finish_configure(r: JobContext, run_id: str, status: dict[str, Any], *, job: dict[str, Any]) -> str:
    """Conclut un ansible_configure à partir des sorties du runner (réconciliation, aucune relance)."""
    work = r.settings.workspaces_dir / run_id
    summary = read_json(work / "out" / "ansible_summary.json", {}) or {}
    rc = status.get("exit_code")
    if rc is None and (work / "out" / "exit_code").exists():
        try:
            rc = int((work / "out" / "exit_code").read_text(encoding="utf-8").strip())
        except ValueError:
            rc = None
    with r.tx() as conn:
        ctx = _load(conn, job)
        targets = _configure_targets(conn, job, ctx)
        configured = _record_configure(r, conn, job, targets, ctx["resolution"], rc, summary, source="workspace")
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (job["change_id"],)).fetchone()
        if change["state"] == "reconciling":
            change = transition(
                conn,
                change,
                "executing",
                reason_code="reconciled_configure",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"outcome": "configured", "source": "workspace"},
            )
        payload = job["payload"]
        queue.enqueue(
            conn,
            kind="verify",
            change=change,
            execution_id=job["execution_id"],
            idempotency_key=f"verify:reconcile:{job['id']}",
            payload={k: v for k, v in payload.items() if k in ("waves", "wave", "retry_approval_id")},
        )
    r.cleanup(run_id)
    return "configured" if configured else "configure_failed"


CHECK_FAILURES = {
    "package_installed": "paquet {package} absent",
    "service_active": "service {unit} inactif",
    "http_probe": "sonde HTTP en échec",
    "jail_active": "jail {jail} inactive",
}


def _report_package(report: dict[str, Any] | None, name: str) -> str | None:
    report = report or {}
    if "packages" in report:
        return (report.get("packages") or {}).get(name)
    return report.get(f"package_{name}")


def _report_process(report: dict[str, Any] | None, name: str) -> bool:
    report = report or {}
    if "processes" in report:
        return bool((report.get("processes") or {}).get(name))
    return str(report.get(f"process_{name}")).lower() == "true"


def _jail_line(line: str, jail: str, verb: str) -> bool:
    compact = line.replace("'", "").replace('"', "")
    return f"Jail {jail} {verb}" in compact


def _jail_started(journal: str, jail: str) -> tuple[bool, str]:
    """Dernier démarrage du service fail2ban dans son journal : la jail y a-t-elle démarré ?"""
    lines = journal.splitlines()
    starts = [i for i, line in enumerate(lines) if "Starting Fail2ban" in line]
    if not starts:
        return False, "démarrage de fail2ban absent des 50 dernières lignes du journal"
    tail = lines[starts[-1] :]
    if any(_jail_line(line, jail, "stopped") for line in tail) or any("Exiting Fail2ban" in line for line in tail):
        return False, f"jail {jail} arrêtée depuis le dernier démarrage"
    if any(_jail_line(line, jail, "started") for line in tail):
        return True, f"journal fail2ban : jail « {jail} » démarrée"
    return False, f"jail « {jail} » non démarrée (journal fail2ban)"


def evaluate_checks(
    checks: list[dict[str, Any]], report: dict[str, Any] | None, probe: dict[str, Any] | None, at: str
) -> list[dict[str, Any]]:
    """Contrôles de santé du template sur un hôte. Les données de collecte sont non fiables."""
    results = []
    for check in checks:
        kind = check["type"]
        entry: dict[str, Any] = {"type": kind, "at": at}
        subject = check.get("package") or check.get("unit") or check.get("jail")
        if subject:
            entry["subject"] = str(subject)
        if kind == "package_installed":
            version = _report_package(report, check["package"])
            entry.update(ok=bool(version), evidence=f"dpkg : {version}" if version else "dpkg : absent")
        elif kind == "service_active":
            process = str(check.get("process", check["unit"]))
            active = _report_process(report, process)
            entry.update(
                ok=active,
                evidence=f"processus {process} {'actif' if active else 'absent'}",
                systemd="non collecté (nœud de labo sans systemd, hypothèse H4 infirmée)",
            )
        elif kind == "http_probe":
            ok = bool(probe and probe.get("ok"))
            entry.update(
                ok=ok,
                evidence=f"HTTP {probe.get('status')}"
                if probe and probe.get("status")
                else f"sonde : {(probe or {}).get('error', 'non jointe')}",
            )
        elif kind == "jail_active":
            log = ((report or {}).get("journals") or {}).get("fail2ban") or {}
            if not log.get("readable"):
                entry.update(ok=False, evidence="journal fail2ban illisible par l'identité de collecte")
            else:
                ok, evidence = _jail_started(str(log.get("text") or ""), str(check["jail"]))
                entry.update(ok=ok, evidence=evidence)
        else:
            entry.update(ok=False, evidence="contrôle non pris en charge")
        if not entry["ok"]:
            entry["failure"] = CHECK_FAILURES.get(kind, "contrôle en échec").format(**check)
        results.append(entry)
    return results


def service_status(host_results: list[dict[str, Any]]) -> tuple[str, str]:
    """État du service calculé par le serveur, distinct du succès des jobs (docs/architecture.md §8)."""
    if not host_results:
        return "failed", "aucune ressource attendue n'a été créée"
    incomplete = [h for h in host_results if h["state"] == "incomplete"]
    failing = [h for h in host_results if h["state"] == "checked" and not all(c["ok"] for c in h["checks"])]
    unavailable = [h for h in host_results if h["state"] == "unavailable"]
    if incomplete:
        blocked = [h for h in incomplete if h.get("blocked")]
        return "created_not_ready", "VM créée, configuration incomplète" + (
            f" : {blocked[0]['reason']}" if blocked else ""
        )
    if failing:
        # La sonde HTTP est le symptôme visible par l'utilisateur : elle mène la raison affichée.
        failed = sorted(
            (c for h in failing for c in h["checks"] if not c["ok"]), key=lambda c: c["type"] != "http_probe"
        )
        reasons = list(dict.fromkeys(c["failure"] for c in failed))
        return "created_not_ready", "VM créée, " + " ; ".join(reasons)
    if unavailable:
        return "verification_unavailable", "vérification indisponible"
    return "ready", "tous les contrôles de santé ont réussi"


def _observe_report(
    r: JobContext, conn: Conn, h: dict[str, Any], report: dict[str, Any], results: list[dict[str, Any]]
) -> None:
    """Observations de la fiche machine : collecte 1.1.0 (déclarée par les composants) ou 1.0.0 (nginx)."""
    hosts.observe(
        conn,
        h,
        "facts",
        {"os": report.get("os"), "kernel": report.get("kernel"), "init": report.get("init")},
        "ok",
        "rackloom-collect",
        r.job["id"],
    )
    packages = dict(report.get("packages") or {}) if "packages" in report else {"nginx": report.get("package_nginx")}
    hosts.observe(
        conn, h, "packages", packages, "ok" if all(packages.values()) else "error", "rackloom-collect", r.job["id"]
    )
    services = {
        x["subject"]: {"active": x["ok"], "manager": "sysv", "systemd": "non collecté"}
        for x in results
        if x["type"] == "service_active"
    }
    hosts.observe(
        conn,
        h,
        "services",
        services,
        "ok" if all(s["active"] for s in services.values()) else "error",
        "rackloom-collect",
        r.job["id"],
    )
    if "journals" in report:
        logs = [
            (name, j.get("path"), str(j.get("text") or "") if j.get("readable") else "")
            for name, j in (report.get("journals") or {}).items()
        ]
    else:
        logs = [("nginx", report.get("journal_source"), str(report.get("journal") or ""))]
    for name, path, text in logs:
        hosts.observe(
            conn,
            h,
            "journal",
            {"name": name, "source": path, "lines": text[:16384].splitlines()[-50:]},
            "ok",
            "rackloom-collect",
            r.job["id"],
            r.redactor,
        )


def _record_evidence(
    conn: Conn, ctx: dict[str, Any], r: JobContext, h: dict[str, Any], results: list[dict[str, Any]]
) -> None:
    """Preuve par composant : ses contrôles ont réussi sur une machine réellement configurée."""
    mode = conn.execute("SELECT mode FROM connectors WHERE kind = 'vsphere'").fetchone()
    connector_mode = mode["mode"] if mode else "simulated"
    level = "lab_validated" if connector_mode == "real" else "tested_locally"
    for c in ctx["resolution"]["components"]:
        row = registry.component(conn, c["slug"], c["version"])
        if row is None:
            continue
        expected = {
            str(s)
            for x in registry.component_checks(row["contract"], c.get("config") or {})
            if (s := x.get("package") or x.get("unit") or x.get("jail"))
        }
        mine = [
            {"type": x["type"], "subject": x.get("subject"), "ok": x["ok"], "evidence": x["evidence"]}
            for x in results
            if x.get("subject") in expected
        ]
        if mine and all(x["ok"] for x in mine):
            registry.record_evidence(
                conn,
                row["id"],
                level=level,
                job_id=r.job["id"],
                deployment_id=h["deployment_id"],
                host_id=h["id"],
                connector_mode=connector_mode,
                checks=mine,
            )


def handle_verify(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        if ctx["change"]["state"] == "executing":
            transition(
                conn,
                ctx["change"],
                "verifying",
                reason_code="verify_started",
                actor_type="worker",
                actor_id=r.worker_id,
            )
        resolution = ctx["resolution"]
        dep = ctx["deployment"]
        payload = r.job["payload"]
        waves: list[list[str]] = payload.get("waves") or []
        index = int(payload.get("wave", 0))
        all_hosts = conn.execute("SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (dep["id"],)).fetchall()
        scope = {n for w in waves[: index + 1] for n in w} if waves else {h["name"] for h in all_hosts}
        later = [n for w in waves[index + 1 :] for n in w]
        targets = [
            h
            for h in all_hosts
            if h["name"] in scope and h["deployment_state"] in ("configured", "verified", "verify_failed")
        ]
        run_id = f"{r.job['id']}-a{r.job['attempts']}"
        stdin = None
        if targets:
            work, stdin = _prepare_ansible(r, conn, run_id, resolution, "collect", targets)
    reports: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    reachable: set[str] = set()
    if targets:
        rc, summary = _ansible_run(r, run_id, "ansible", stdin)
        reports = summary.get("reports", {})
        reachable = {h["name"] for h in targets if _host_ok(summary, h["name"])[0]}
        r.cleanup(run_id)
        probe_id = f"{r.job['id']}-p{r.job['attempts']}"
        pwork = r.workspace(probe_id)
        http = next((c for c in resolution["health_checks"] if c["type"] == "http_probe"), None)
        (pwork / "job.json").write_text(
            json.dumps(
                {
                    "action": "http_probe",
                    "targets": [
                        {
                            "host": h["name"],
                            "address": h["address"],
                            "port": int(http["port"]) if http else 80,
                            "path": http["path"] if http else "/",
                        }
                        for h in targets
                    ],
                }
            )
        )
        r.run({"run_id": probe_id, "kind": "probe", "timeout_seconds": 60})
        probes = {p["host"]: p for p in (read_json(pwork / "out" / "probe.json", []) or [])}
        r.cleanup(probe_id)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    host_results: list[dict[str, Any]] = []
    with r.tx() as conn:
        for h in all_hosts:
            if h["name"] not in scope:
                continue
            if h not in targets:
                host_results.append(
                    {
                        "host": h["name"],
                        "state": "incomplete",
                        "checks": [],
                        "reason": f"état du déploiement : {h['deployment_state']}",
                    }
                )
                continue
            report = reports.get(h["name"])
            if h["name"] not in reachable or report is None:
                hosts.observe(
                    conn,
                    h,
                    "facts",
                    {"error": "collecte impossible : hôte injoignable"},
                    "unreachable",
                    "rackloom-collect",
                    r.job["id"],
                )
                host_results.append(
                    {"host": h["name"], "state": "unavailable", "checks": [], "reason": "hôte injoignable"}
                )
                r.step(conn, f"Vérification ({h['name']})", "failed", summary={"reason": "hôte injoignable"})
                continue
            member_checks = resolution["health_checks"]
            if h.get("composition_member"):
                for m in (resolution.get("composition") or {}).get("members") or []:
                    if m["id"] == h["composition_member"]:
                        member_checks = m.get("health_checks") or m.get("readiness") or member_checks
                        break
            results = evaluate_checks(member_checks, report, probes.get(h["name"]), at)
            ok = all(x["ok"] for x in results)
            _observe_report(r, conn, h, report, results)
            probe = probes.get(h["name"])
            if probe is not None:
                hosts.observe(conn, h, "probe", probe, "ok" if probe.get("ok") else "error", "probe", r.job["id"])
            conn.execute(
                "UPDATE hosts SET deployment_state = %s WHERE id = %s", ("verified" if ok else "verify_failed", h["id"])
            )
            r.step(
                conn,
                f"Vérification ({h['name']})",
                "succeeded" if ok else "failed",
                summary={"checks": [{"type": x["type"], "ok": x["ok"], "evidence": x["evidence"]} for x in results]},
            )
            host_results.append({"host": h["name"], "state": "checked", "checks": results})
            if ok:
                _record_evidence(conn, ctx, r, h, results)
        wave_names = set(waves[index]) if waves else scope
        wave_results = [x for x in host_results if x["host"] in wave_names]
        wave_ok = bool(wave_results) and all(
            x["state"] == "checked" and all(c["ok"] for c in x["checks"]) for x in wave_results
        )
        if later and wave_ok:
            message = f"{_wave_verified(index)} : vague {index + 2} lancée"
            r.event(conn, "deployment.wave_passed", {"wave": index + 1, "message": message})
            r.fenced(
                conn,
                "state = 'succeeded', finished_at = now(), result = %s",
                (Jsonb({"wave": index + 1, "next_wave": index + 2}),),
            )
            change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
            if change["state"] == "verifying":
                change = transition(
                    conn,
                    change,
                    "executing",
                    reason_code="wave_next",
                    actor_type="worker",
                    actor_id=r.worker_id,
                    detail={"message": message},
                )
            _start_wave(
                r, conn, change, waves, index + 1, {k: payload[k] for k in ("retry_approval_id",) if k in payload}
            )
            return
        if later:
            conn.execute(
                "UPDATE hosts SET deployment_state = 'wave_blocked' WHERE deployment_id = %s "
                "AND name = ANY(%s) AND deployment_state IN ('enrolled', 'wave_blocked')",
                (dep["id"], later),
            )
            for w in range(index + 1, len(waves)):
                why = f"vague {w + 1} bloquée après l'échec {_wave_of(index)}"
                r.step(conn, f"Vague {w + 1} bloquée ({', '.join(waves[w])})", "skipped", summary={"reason": why})
                for name in waves[w]:
                    host_results.append(
                        {"host": name, "state": "incomplete", "checks": [], "reason": why, "blocked": True}
                    )
            r.event(
                conn,
                "deployment.wave_blocked",
                {
                    "wave": index + 1,
                    "blocked": later,
                    "message": f"Échec {_wave_of(index)} : aucune configuration lancée sur {', '.join(later)}",
                },
                severity="error",
            )
        status, reason = service_status(host_results)
        conn.execute(
            "UPDATE deployments SET service_status = %s, service_reason = %s, service_checked_at = now(), "
            "service_checks = %s WHERE id = %s",
            (status, reason, Jsonb(host_results), dep["id"]),
        )
        r.event(
            conn,
            "deployment.service_status",
            {"status": status, "message": reason, "deployment_id": str(dep["id"])},
            severity="info" if status == "ready" else "warning",
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({"service_status": status, "hosts": len(all_hosts)}),),
        )
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        if change["state"] == "executing":
            change = transition(
                conn, change, "verifying", reason_code="verify_started", actor_type="worker", actor_id=r.worker_id
            )
        conn.execute(
            "UPDATE executions SET finished_at = now(), state = %s WHERE id = %s",
            ("succeeded" if status == "ready" else "partially_succeeded", r.job["execution_id"]),
        )
        if status == "ready":
            transition(
                conn,
                change,
                "succeeded",
                reason_code="service_ready",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"message": f"Succès : service prêt ({len(all_hosts)} hôte(s))", "service_status": status},
            )
        else:
            transition(
                conn,
                change,
                "partially_succeeded",
                reason_code=f"service_{status}",
                actor_type="worker",
                actor_id=r.worker_id,
                severity="error",
                detail={"message": reason, "service_status": status},
            )


def handle_reconcile(r: JobContext) -> None:
    """Lecture seule : statut du runner auprès du launcher, puis state Terraform. Jamais de relance."""
    target_id = UUID(r.job["payload"]["job_id"])
    with r.tx() as conn:
        target = conn.execute("SELECT * FROM jobs WHERE id = %s", (target_id,)).fetchone()
        r.fenced(conn, "state = 'running'")
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        if change["state"] == "uncertain":
            transition(
                conn, change, "reconciling", reason_code="reconcile_started", actor_type="worker", actor_id=r.worker_id
            )
        r.step(conn, "Interrogation du launcher", "running")
    run_id = target["provider_ref"] or str(target_id)
    status = r.launcher.status(run_id)
    if status["state"] in ("running", "created"):
        status = r.wait(run_id)
    if target["kind"] != "tf_apply":
        _reconcile_other(r, target, status)
        return
    if status["state"] == "exited" and (r.settings.workspaces_dir / run_id / "out" / "exit_code").exists():
        outcome = finish_apply(r, run_id, status, job=target)
        with r.tx() as conn:
            r.step(conn, "Conclusion : sorties du runner retrouvées", "succeeded", summary={"outcome": outcome})
            r.fenced(
                conn,
                "state = 'succeeded', finished_at = now(), result = %s",
                (Jsonb({"source": "launcher", "outcome": outcome}),),
            )
        return
    with r.tx() as conn:
        ctx = _load(conn, target, lock=False)
    resolution = ctx["resolution"]
    inspect_id = f"{r.job['id']}-i{r.job['attempts']}"
    work = _prepare_tf(r, inspect_id, resolution, {"action": "tf_inspect", "timeout_seconds": 120})
    r.run({"run_id": inspect_id, "kind": "terraform", "stack": ctx["stack"]["slug"], "timeout_seconds": 120})
    inspect = read_json(work / "out" / "inspect.json", {}) or {}
    r.cleanup(inspect_id)
    state = inspect.get("state") or {}
    planned = {i["name"] for i in resolution["instances"]}
    vms = _vms_from_state(state)
    present = {vm["name"] for vm in vms}
    plan = ctx["plan"]
    if state.get("exists") and state.get("serial") != plan["state_serial"] and planned <= present:
        conclusion = "applied"
    elif (not state.get("exists") and plan["state_serial"] is None) or (state.get("serial") == plan["state_serial"]):
        conclusion = "not_applied"
    else:
        conclusion = "indeterminate"
    with r.tx() as conn:
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        r.step(
            conn,
            "Conclusion à partir du state",
            "succeeded",
            summary={"conclusion": conclusion, "vms": sorted(present)},
        )
        if conclusion == "applied":
            conn.execute(
                "UPDATE jobs SET state = 'succeeded', finished_at = now(), result = %s WHERE id = %s",
                (Jsonb({"vms": vms, "outcome": "applied", "source": "state"}), target_id),
            )
            admission.release_stack(conn, target)
            conn.execute("UPDATE plans SET status = 'applied' WHERE id = %s", (plan["id"],))
            _record_instances(conn, change, resolution, vms)
            change = transition(
                conn,
                change,
                "executing",
                reason_code="reconciled_applied",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"message": "Réconciliation : plan appliqué (state)"},
            )
            queue.enqueue(
                conn,
                kind="guest_ready",
                change=change,
                execution_id=target["execution_id"],
                stack_id=target["stack_id"],
                idempotency_key=f"guest_ready:{target['execution_id']}",
                payload={"vms": vms},
            )
        elif conclusion == "not_applied":
            conn.execute(
                "UPDATE jobs SET state = 'failed', finished_at = now(), result = %s WHERE id = %s",
                (Jsonb({"outcome": "not_applied", "source": "state"}), target_id),
            )
            admission.release_stack(conn, target)
            transition(
                conn,
                change,
                "failed",
                reason_code="reconciled_not_applied",
                actor_type="worker",
                actor_id=r.worker_id,
                severity="error",
                detail={"message": "Réconciliation : plan non appliqué, aucune relance automatique"},
            )
        else:
            transition(
                conn,
                change,
                "uncertain",
                reason_code="reconciled_indeterminate",
                actor_type="worker",
                actor_id=r.worker_id,
                severity="error",
                detail={"message": "Réconciliation indéterminée : intervention manuelle requise"},
            )
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s", (Jsonb({"conclusion": conclusion}),))


def _reconcile_other(r: JobContext, target: dict[str, Any], status: dict[str, Any]) -> None:
    """Ansible : conclure depuis les sorties si présentes ; sinon interrompu, aucune relance automatique."""
    run_id = target["provider_ref"] or str(target["id"])
    out = r.settings.workspaces_dir / run_id / "out"
    if (
        target["kind"] == "ansible_configure"
        and status["state"] == "exited"
        and (out / "exit_code").exists()
        and (out / "ansible_summary.json").exists()
    ):
        outcome = finish_configure(r, run_id, status, job=target)
        with r.tx() as conn:
            r.step(conn, "Conclusion : sorties du runner retrouvées", "succeeded", summary={"outcome": outcome})
            r.fenced(
                conn,
                "state = 'succeeded', finished_at = now(), result = %s",
                (Jsonb({"source": "launcher", "outcome": outcome}),),
            )
        return
    with r.tx() as conn:
        conn.execute(
            "UPDATE jobs SET state = 'failed', finished_at = now(), result = %s WHERE id = %s AND state = 'uncertain'",
            (Jsonb({"outcome": "interrupted", "launcher": status["state"]}), target["id"]),
        )
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        conn.execute(
            "UPDATE hosts SET deployment_state = 'config_failed' WHERE deployment_id = %s "
            "AND deployment_state = 'enrolled'",
            (change["deployment_id"],),
        )
        transition(
            conn, change, "executing", reason_code="reconciled_configure", actor_type="worker", actor_id=r.worker_id
        )
        queue.enqueue(
            conn,
            kind="verify",
            change=change,
            execution_id=target["execution_id"],
            idempotency_key=f"verify:reconcile:{target['id']}",
            payload={},
        )
        r.fenced(conn, "state = 'succeeded', finished_at = now()")


def handle_drift_scan(r: JobContext) -> None:
    """F09 : scan de dérive en lecture seule — aucune correction."""
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        deployment_id = r.job["payload"].get("deployment_id") or (
            str(ctx["deployment"]["id"]) if ctx.get("deployment") else None
        )
        if not deployment_id:
            raise Refused("deployment_id manquant pour le scan de dérive", "drift_target_missing")
        result = drift.scan_deployment(
            conn,
            deployment_id=UUID(str(deployment_id)),
            workspaces_parent=r.settings.data_dir,
            job_id=r.job["id"],
            actor_type="worker",
            actor_id=r.worker_id,
            observation_unavailable=bool(r.job["payload"].get("observation_unavailable")),
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({**result, "correction": False}),),
        )
        r.event(
            conn,
            "drift.scanned",
            {
                "message": f"Dérive : {result['status']} ({len(result['findings'])} écart(s))",
                "status": result["status"],
                "correction": False,
            },
        )


def handle_vm_restore(r: JobContext) -> None:
    """F40 : restauration simulée en réseau de quarantaine (pas de labo réel)."""
    from .. import restore as restore_mod

    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolved = ctx["resolution"] or {}
        restore_meta = resolved.get("restore") or {}
        source = restore_meta.get("source_instance")
        if not source:
            raise Refused("restore_missing_source", "instance source absente de la révision")
        restore_mod.verify_plan(resolved, source)
        network = (resolved.get("placement") or {}).get("network") or {}
        names = [i["name"] for i in (resolved.get("instances") or [])]
        r.step(
            conn,
            "Restauration en quarantaine (simulée)",
            "succeeded",
            simulated=True,
            summary={
                "source_instance": source,
                "restored_names": names,
                "network": network.get("name"),
                "network_role": network.get("role"),
                "attach_production_network": False,
                "simulated": True,
                "notice": "Aucune restauration réelle : observations fictives (D2).",
            },
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (
                Jsonb(
                    {
                        "simulated": True,
                        "source_instance": source,
                        "restored_names": names,
                        "network_role": network.get("role"),
                    }
                ),
            ),
        )
        change = ctx["change"]
        if change["state"] == "approved":
            change = transition(
                conn,
                change,
                "executing",
                reason_code="restore_started",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True},
            )
        if change["state"] == "executing":
            change = transition(
                conn,
                change,
                "verifying",
                reason_code="restore_verify",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True, "network_role": network.get("role")},
            )
        transition(
            conn,
            change,
            "succeeded",
            reason_code="restore_simulated",
            actor_type="worker",
            actor_id=r.worker_id,
            detail={"simulated": True, "network_role": network.get("role")},
        )


def handle_service_restart(r: JobContext) -> None:
    """F58 : redémarrage simulé (interruption annoncée ; pas de labo réel)."""
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolved = ctx["resolution"] or {}
        names = [i["name"] for i in (resolved.get("instances") or [])]
        r.step(
            conn,
            "Redémarrage (simulé)",
            "succeeded",
            simulated=True,
            summary={
                "hosts": names,
                "impact": "interruption",
                "simulated": True,
                "notice": "Aucun redémarrage réel : observations fictives (D2).",
            },
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (Jsonb({"simulated": True, "hosts": names, "impact": "interruption"}),),
        )
        change = ctx["change"]
        if change["state"] == "approved":
            change = transition(
                conn,
                change,
                "executing",
                reason_code="restart_started",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True},
            )
        if change["state"] == "executing":
            change = transition(
                conn,
                change,
                "verifying",
                reason_code="restart_verify",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True},
            )
        transition(
            conn,
            change,
            "succeeded",
            reason_code="restart_simulated",
            actor_type="worker",
            actor_id=r.worker_id,
            detail={"simulated": True, "hosts": names},
        )


def handle_lease_retire(r: JobContext) -> None:
    """F06 : retrait de labo après approbation — simulé, aucun destroy Terraform."""
    from .. import leases

    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        change = ctx["change"]
        dep = ctx["deployment"]
        lease = leases.get_by_deployment(conn, dep["id"]) if dep else None
        names = [i["name"] for i in ((dep or {}).get("instances") or []) if i.get("name")]
        # Preuve : les ressources existent encore avant clôture du bail.
        host_before = conn.execute(
            "SELECT count(*) AS n FROM hosts WHERE deployment_id = %s",
            (dep["id"],),
        ).fetchone()["n"] if dep else 0
        r.step(
            conn,
            "Retrait enregistré (simulé)",
            "succeeded",
            simulated=True,
            summary={
                "hosts": names,
                "host_count_before": host_before,
                "simulated": True,
                "no_provider_destroy": True,
                "notice": "Aucune suppression réelle de VM (D2) ; destroy Terraform réservé au laboratoire.",
            },
        )
        if lease is not None:
            leases.close_after_retirement(
                conn,
                lease_id=lease["id"],
                change_id=change["id"],
                reason="Retrait exécuté (simulé) — aucune suppression réelle de VM",
            )
        # Les hôtes et fichiers labo restent ; seul le statut déploiement passe à abandoned.
        still_hosts = conn.execute(
            "SELECT count(*) AS n FROM hosts WHERE deployment_id = %s",
            (dep["id"],),
        ).fetchone()["n"] if dep else 0
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (
                Jsonb(
                    {
                        "simulated": True,
                        "hosts": names,
                        "hosts_intact": still_hosts == host_before,
                        "no_provider_destroy": True,
                    }
                ),
            ),
        )
        if change["state"] == "approved":
            change = transition(
                conn,
                change,
                "executing",
                reason_code="retire_started",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True},
            )
        if change["state"] == "executing":
            change = transition(
                conn,
                change,
                "verifying",
                reason_code="retire_verify",
                actor_type="worker",
                actor_id=r.worker_id,
                detail={"simulated": True, "hosts_intact": still_hosts == host_before},
            )
        transition(
            conn,
            change,
            "succeeded",
            reason_code="retire_simulated",
            actor_type="worker",
            actor_id=r.worker_id,
            detail={"simulated": True, "hosts_intact": still_hosts == host_before},
        )


def handle_tf_import(r: JobContext) -> None:
    """F08 : import Terraform d'adoption — simulé, aucune écriture vSphere réelle."""
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        payload = r.job.get("payload") or {}
        adoption_id = payload.get("adoption_id")
        if adoption_id:
            adoption = conn.execute(
                "SELECT * FROM vm_adoptions WHERE id = %s", (UUID(str(adoption_id)),)
            ).fetchone()
            if adoption is not None:
                ctx["env"] = conn.execute(
                    "SELECT * FROM environments WHERE id = %s", (adoption["environment_id"],)
                ).fetchone()
        _admit(r, conn, ctx)
        r.step(
            conn,
            "Import Terraform (simulé)",
            "succeeded",
            simulated=True,
            summary={
                "adoption_id": payload.get("adoption_id"),
                "vm_stable_id": payload.get("vm_stable_id"),
                "simulated": True,
                "notice": "Aucun import réel : revendication locale seulement (D2).",
            },
            source="terraform",
        )
        r.fenced(
            conn,
            "state = 'succeeded', finished_at = now(), result = %s",
            (
                Jsonb(
                    {
                        "simulated": True,
                        "adoption_id": payload.get("adoption_id"),
                        "vm_stable_id": payload.get("vm_stable_id"),
                        "proposal_sha256": payload.get("proposal_sha256"),
                    }
                ),
            ),
        )
        r.event(
            conn,
            "adoption.import_job_done",
            {
                "message": "Job d'import d'adoption terminé (simulé)",
                "adoption_id": payload.get("adoption_id"),
                "simulated": True,
            },
            source="terraform",
        )


HANDLERS = {
    "preflight": handle_preflight,
    "tf_plan": handle_tf_plan,
    "tf_apply": handle_tf_apply,
    "guest_ready": handle_guest_ready,
    "ansible_configure": handle_ansible_configure,
    "verify": handle_verify,
    "reconcile": handle_reconcile,
    "drift_scan": handle_drift_scan,
    "vm_restore": handle_vm_restore,
    "service_restart": handle_service_restart,
    "lease_retire": handle_lease_retire,
    "tf_import": handle_tf_import,
}


def catalog_ok(conn: Conn) -> bool:
    return registry.latest_published(conn, "ubuntu-web") is not None
