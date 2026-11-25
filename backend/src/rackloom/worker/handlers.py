"""Gestionnaires par type de job. Enchaînement d'une exécution :
tf_apply → guest_ready → ansible_configure → verify, chaque job suivant étant créé dans la
transaction qui enregistre le succès du précédent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from .. import admission, artifacts, catalog, hosts, secrets
from ..canonical import digest, file_tree_digest
from ..changes.transitions import transition
from ..db import Jsonb
from ..executions import bundle as bundles
from ..executions import plans as plan_summary
from ..executions import queue
from .core import JobContext, Refused, read_json

TERRAFORM_BIN = "terraform"
IDENTITIES = {"configure": "rackloom-config", "collect": "rackloom-collect"}


# --- Chargement du contexte ---------------------------------------------------------------

def _load(conn: psycopg.Connection, job: dict[str, Any], lock: bool = True) -> dict[str, Any]:
    ctx: dict[str, Any] = {"job": job}
    change = conn.execute(f"SELECT * FROM changes WHERE id = %s{' FOR UPDATE' if lock else ''}",
                          (job["change_id"],)).fetchone() if job["change_id"] else None
    ctx["change"] = change
    ctx["env"] = conn.execute("SELECT * FROM environments WHERE id = %s",
                              (change["environment_id"],)).fetchone() if change else None
    ctx["project"] = conn.execute("SELECT * FROM projects WHERE id = %s", (job["project_id"],)).fetchone()
    ctx["stack"] = conn.execute("SELECT * FROM stacks WHERE id = %s", (job["stack_id"],)).fetchone() \
        if job["stack_id"] else (conn.execute("SELECT * FROM stacks WHERE change_id = %s", (change["id"],)).fetchone()
                                 if change else None)
    ctx["execution"] = conn.execute("SELECT * FROM executions WHERE id = %s",
                                    (job["execution_id"],)).fetchone() if job["execution_id"] else None
    ctx["plan"] = conn.execute("SELECT * FROM plans WHERE id = %s", (ctx["execution"]["plan_id"],)).fetchone() \
        if ctx["execution"] else None
    approval_id = job["payload"].get("retry_approval_id") or (ctx["execution"]["approval_id"]
                                                              if ctx["execution"] else None)
    ctx["approval"] = conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone() \
        if approval_id else None
    version_no = ctx["plan"]["version_no"] if ctx["plan"] else (job["payload"].get("version_no")
                                                                or (change["current_version_no"] if change else None))
    ctx["version"] = conn.execute("SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
                                  (change["id"], version_no)).fetchone() if change else None
    return ctx


def _admit(r: JobContext, conn: psycopg.Connection, ctx: dict[str, Any], recompute=None) -> None:
    decision = admission.evaluate(admission.Context(
        conn=conn, job=r.job, change=ctx["change"], environment=ctx["env"], execution=ctx["execution"],
        approval=ctx["approval"], plan=ctx["plan"], recompute_digest=recompute))
    if decision["outcome"] == "admitted":
        r.fenced(conn, "state = 'running', blocked_reason = NULL")
        r.step(conn, "Admission (règles builtin@1)", "succeeded",
               summary={"rules": [x["id"] for x in decision["rules"]], "policy": decision["policy"]["version"]})
        return
    refusal = decision["refusal"]
    r.fenced(conn, "state = 'blocked', blocked_reason = %s, finished_at = now(), lease_owner = NULL",
             (refusal["code"],))
    admission.release_stack(conn, r.job)
    r.step(conn, "Admission (règles builtin@1)", "failed", summary={"rule": refusal["id"], "reason": refusal["reason"]})
    r.event(conn, "job.blocked", {"kind": r.job["kind"], "rule": refusal["id"], "code": refusal["code"],
                                  "message": f"Admission refusée ({refusal['id']}) : {refusal['reason']}"},
            severity="error")
    change = ctx["change"]
    if change is not None:
        target = {"planning": "plan_failed", "approved": "plan_expired" if refusal["code"] == "plan_expired"
                  else ("cancelled" if refusal["code"] == "cancel_requested" else "blocked")}.get(change["state"])
        if change["state"] in ("executing", "verifying"):
            target = "cancelled" if refusal["code"] == "cancel_requested" and r.job["kind"] == "tf_apply" else \
                "partially_succeeded"
        if target:
            if target == "plan_expired" and ctx["plan"]:
                conn.execute("UPDATE plans SET status = 'expired' WHERE id = %s AND status = 'ready'",
                             (ctx["plan"]["id"],))
            transition(conn, change, target, reason_code=f"admission_{refusal['code']}", actor_type="worker",
                       actor_id=r.worker_id, detail={"rule": refusal["id"], "message": refusal["reason"]},
                       severity="error")
    raise Refused(refusal["code"])


# --- Terraform ---------------------------------------------------------------------------

def _module_dir(r: JobContext, resolution: dict[str, Any]) -> Path:
    tf = resolution["terraform"]
    return r.settings.catalog_dir / "terraform" / tf["module"] / tf["version"]


def _prepare_tf(r: JobContext, run_id: str, resolution: dict[str, Any], job_json: dict[str, Any]) -> Path:
    work = r.workspace(run_id)
    src = _module_dir(r, resolution)
    if file_tree_digest(src, ("*.tf", ".terraform.lock.hcl")) != resolution["terraform"]["digest"]:
        raise RuntimeError("module Terraform modifié sur disque depuis son chargement")
    tfdir = work / "tf"
    tfdir.mkdir(exist_ok=True)
    for f in list(src.glob("*.tf")) + [src / ".terraform.lock.hcl"]:
        shutil.copy2(f, tfdir / f.name)
    (work / "job.json").write_text(json.dumps({"terraform_bin": TERRAFORM_BIN, **job_json}), encoding="utf-8")
    return work


def _tf_variables(conn: psycopg.Connection, stack: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    delay = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_delay_seconds'").fetchone()
    return {
        "stack_id": stack["slug"],
        "lab_delay_seconds": int(delay["value"]) if delay else 0,
        "vms": [{"name": vm["name"], "size": resolution["size"]["key"], "cpu": vm["cpu"],
                 "memory_mb": vm["memory_mb"], "template_ref": resolution["image"]["stable_id"],
                 "network_ref": resolution["network"]["stable_id"]} for vm in resolution["vms"]],
    }


def _store_log(r: JobContext, conn: psycopg.Connection, work: Path, name: str) -> dict[str, Any] | None:
    path = work / "out" / name
    if not path.exists():
        return None
    return artifacts.store(conn, project_id=r.job["project_id"], change_id=r.job["change_id"],
                           kind=name.replace(".", "_"), key_class="A", plaintext=r.redactor.bytes(path.read_bytes()))


def handle_tf_plan(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["version"]["resolution"]
        variables = _tf_variables(conn, ctx["stack"], resolution)
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
            transition(conn, change, "plan_failed", reason_code="terraform_plan_failed", actor_type="worker",
                       actor_id=r.worker_id, detail={"exit_code": rc}, severity="error")
        r.cleanup(run_id)
        return
    plan_bytes = (out / "plan.bin").read_bytes()
    state_before = read_json(out / "state_before.json", {}) or {}
    summary = plan_summary.summarize(plan_json)
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        art = artifacts.store(conn, project_id=r.job["project_id"], change_id=r.job["change_id"], kind="tf_plan_bin",
                              key_class="A", plaintext=plan_bytes)
        _store_log(r, conn, work, "terraform.log")
        valid_until = datetime.now(UTC) + timedelta(seconds=ctx["env"]["plan_ttl_seconds"])
        bundle = bundles.build(conn, change=ctx["change"], version=ctx["version"], stack=ctx["stack"],
                               plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
                               state_lineage=state_before.get("lineage"), state_serial=state_before.get("serial"),
                               valid_until=valid_until.isoformat(), terraform_version=summary["terraform_version"])
        status_ = "blocked_destructive" if summary["destructive"] else ("no_changes" if summary["no_changes"]
                                                                       else "ready")
        plan_id = queue.new_id()
        conn.execute(
            """
            INSERT INTO plans (id, change_id, version_no, stack_id, job_id, status, plan_sha256, artifact_id, summary,
                               counts, valid_until, bundle, bundle_digest, state_lineage, state_serial)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (plan_id, r.job["change_id"], ctx["version"]["version_no"], ctx["stack"]["id"], r.job["id"], status_,
             art["sha256"], art["id"], Jsonb(summary), Jsonb(summary["vm_counts"]), valid_until, Jsonb(bundle),
             digest(bundle), state_before.get("lineage"), state_before.get("serial")))
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                 (Jsonb({"plan_id": str(plan_id), "status": status_}),))
        counts = summary["vm_counts"]
        if r.job["payload"].get("replan") and summary["no_changes"]:
            transition(conn, ctx["change"], "succeeded", reason_code="no_changes", actor_type="worker",
                       actor_id=r.worker_id,
                       detail={"message": "Aucun changement : l'infrastructure correspond déjà à la demande"})
        else:
            transition(conn, ctx["change"], "plan_ready", reason_code="plan_ready", actor_type="worker",
                       actor_id=r.worker_id,
                       detail={"plan_id": str(plan_id), "status": status_, "counts": counts,
                               "bundle_digest": digest(bundle)[:12]},
                       severity="warning" if status_ == "blocked_destructive" else "info")
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
            return bundles.recompute_digest(conn, plan, ctx["change"], ctx["version"], ctx["stack"], plan_sha)

        _admit(r, conn, ctx, recompute)
        resolution = ctx["version"]["resolution"]
    run_id = str(r.job["id"])
    work = _prepare_tf(r, run_id, resolution, {"action": "tf_apply", "timeout_seconds": 1800})
    (work / "in").mkdir(exist_ok=True)
    (work / "in" / "plan.bin").write_bytes(plan_bytes)
    if hashlib.sha256((work / "in" / "plan.bin").read_bytes()).hexdigest() != plan["plan_sha256"]:
        raise RuntimeError("copie du plan altérée")
    with r.tx() as conn:
        r.fenced(conn, "dispatched_at = now(), provider_ref = %s", (run_id,))
        conn.execute("UPDATE executions SET started_at = coalesce(started_at, now()), state = 'running' WHERE id = %s",
                     (r.job["execution_id"],))
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        transition(conn, change, "executing", reason_code="apply_dispatched", actor_type="worker",
                   actor_id=r.worker_id, detail={"plan_id": str(plan["id"])})
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
            r.fenced(conn, "state = %s, finished_at = now(), result = %s",
                     ("succeeded" if outcome == "applied" else "failed", Jsonb(result)))
        else:
            conn.execute("UPDATE jobs SET state = %s, finished_at = now(), result = %s, updated_at = now() "
                         "WHERE id = %s AND state = 'uncertain'",
                         ("succeeded" if outcome == "applied" else "failed", Jsonb(result), target["id"]))
        admission.release_stack(conn, target)
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (target["change_id"],)).fetchone()
        plan_id = conn.execute("SELECT plan_id FROM executions WHERE id = %s",
                               (target["execution_id"],)).fetchone()["plan_id"]
        if state_after and state_after.get("exists"):
            conn.execute("UPDATE stacks SET last_lineage = %s, last_serial = %s WHERE id = %s",
                         (state_after.get("lineage"), state_after.get("serial"), target["stack_id"]))
        if change["state"] == "reconciling":
            change = transition(conn, change, "executing", reason_code="reconciled", actor_type="worker",
                                actor_id=r.worker_id, detail={"outcome": outcome})
        if outcome == "applied":
            conn.execute("UPDATE plans SET status = 'applied' WHERE id = %s", (plan_id,))
            r.event(conn, "execution.applied", {"message": f"Plan appliqué : {len(vms)} VM de labo créée(s)",
                                                "vms": [v["name"] for v in vms]})
            queue.enqueue(conn, kind="guest_ready", change=change, execution_id=target["execution_id"],
                          stack_id=target["stack_id"], idempotency_key=f"guest_ready:{target['execution_id']}",
                          payload={"vms": vms})
        elif outcome == "stale":
            conn.execute("UPDATE plans SET status = 'expired' WHERE id = %s", (plan_id,))
            transition(conn, change, "plan_expired", reason_code="saved_plan_stale", actor_type="worker",
                       actor_id=r.worker_id, severity="error",
                       detail={"message": "Terraform a refusé le plan : le state a changé depuis le calcul du "
                                          "plan. Nouveau plan et nouvelle approbation nécessaires."})
        elif outcome == "cancelled":
            conn.execute("UPDATE plans SET status = 'failed' WHERE id = %s", (plan_id,))
            transition(conn, change, "cancelled", reason_code="cancelled_before_resources", actor_type="worker",
                       actor_id=r.worker_id, detail={"message": "Annulation effective : aucune ressource créée"})
        else:
            conn.execute("UPDATE plans SET status = 'failed' WHERE id = %s", (plan_id,))
            message = (f"Annulation demandée : application interrompue, {len(vms)} VM de labo présentes, "
                       "aucune suppression") if outcome == "cancelled_partial" else \
                f"Échec de l'application (code {rc}) : {len(vms)} VM de labo présentes, aucune suppression"
            transition(conn, change, "failed", reason_code=f"apply_{outcome}", actor_type="worker",
                       actor_id=r.worker_id, severity="error", detail={"message": message, "vms": len(vms)})
        conn.execute("UPDATE executions SET state = %s WHERE id = %s", (outcome, target["execution_id"]))
    r.cleanup(run_id)
    return outcome


# --- Invités et Ansible ------------------------------------------------------------------

def _lab_keys(conn: psycopg.Connection, purpose: str) -> tuple[str, str]:
    identity = IDENTITIES[purpose]
    pubs = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_ssh_public_keys'").fetchone()
    if pubs is None or identity not in pubs["value"]:
        raise RuntimeError(f"clé SSH de l'identité {identity} non importée (make lab-keys)")
    private = secrets.reveal(conn, f"secret://lab/ssh/{identity}").decode()
    return pubs["value"][identity], private


def _prepare_ansible(r: JobContext, conn: psycopg.Connection, run_id: str, resolution: dict[str, Any],
                     purpose: str, targets: list[dict[str, Any]]) -> tuple[Path, str]:
    work = r.workspace(run_id)
    private_dir = work / "ansible"
    for sub in ("env", "inventory", "project"):
        (private_dir / sub).mkdir(parents=True, exist_ok=True)
    (private_dir / "env" / "known_hosts").write_text(hosts.known_hosts(conn, [h["address"] for h in targets]))
    identity = IDENTITIES[purpose]
    inventory = {"all": {"children": {"enrolled": {"hosts": {
        h["name"]: {"ansible_host": h["address"], "ansible_user": identity,
                    "ansible_python_interpreter": "/usr/bin/python3"} for h in targets}}}}}
    (private_dir / "inventory" / "hosts.json").write_text(json.dumps(inventory))
    pb = resolution["ansible"][purpose]
    pb_dir = r.settings.catalog_dir / "ansible" / "playbooks" / pb["playbook"] / pb["version"]
    if file_tree_digest(pb_dir) != pb["digest"]:
        raise RuntimeError("playbook modifié sur disque depuis son chargement")
    shutil.copy2(pb_dir / f"{pb['playbook']}.yml", private_dir / "project" / f"{pb['playbook']}.yml")
    for role in resolution["ansible"]["roles"]:
        src = r.settings.catalog_dir / "ansible" / "roles" / role["name"] / role["version"]
        if file_tree_digest(src) != role["digest"]:
            raise RuntimeError(f"rôle {role['name']} modifié sur disque depuis son chargement")
        shutil.copytree(src, private_dir / "project" / "roles" / role["name"], dirs_exist_ok=True)
    (private_dir / "project" / "ansible.cfg").write_text(
        "[defaults]\nhost_key_checking = True\nroles_path = roles\nretry_files_enabled = False\n"
        "interpreter_python = /usr/bin/python3\n[ssh_connection]\npipelining = True\n")
    pub, private = _lab_keys(conn, purpose)
    r.redactor = type(r.redactor)([private, *r.redactor.known])
    (work / "job.json").write_text(json.dumps({"action": "ansible_play", "playbook": f"{pb['playbook']}.yml",
                                               "ssh_public_key": pub, "timeout_seconds": 900}))
    return work, json.dumps({"ssh_private_key": private})


def _ansible_run(r: JobContext, run_id: str, kind: str, stdin: str) -> tuple[int | None, dict[str, Any]]:
    status = r.run({"run_id": run_id, "kind": kind, "timeout_seconds": 900, "stdin_secret": stdin})
    summary = read_json(r.settings.workspaces_dir / run_id / "out" / "ansible_summary.json", {}) or {}
    return status.get("exit_code"), summary


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
        resolution = ctx["version"]["resolution"]
        nodes = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_nodes'").fetchone()
        nodes = nodes["value"] if nodes else []
        used = {row["address"] for row in conn.execute(
            "SELECT address FROM hosts WHERE address IS NOT NULL AND change_id <> %s", (r.job["change_id"],))}
        free = [n for n in nodes if n["address"] not in used]
        targets = []
        mapping = []
        for vm in r.job["payload"]["vms"]:
            host = conn.execute("SELECT * FROM hosts WHERE change_id = %s AND name = %s",
                                (r.job["change_id"], vm["name"])).fetchone()
            if host is None:
                node = free.pop(0) if free else None
                host = conn.execute(
                    "INSERT INTO hosts (id, project_id, environment_id, change_id, stack_id, vm_stable_id, name, "
                    "address, deployment_state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (queue.new_id(), r.job["project_id"], ctx["change"]["environment_id"], r.job["change_id"],
                     r.job["stack_id"], f"vm-lab-{vm['uuid'][:8]}", vm["name"], node["address"] if node else None,
                     "created" if node else "no_address")).fetchone()
            mapping.append({"vm": vm["name"], "node": host["address"]})
            if host["address"]:
                targets.append(host)
        r.step(conn, "Personnalisation invitée (vCenter fictif)", "succeeded", simulated=True,
               summary={"vms": [m["vm"] for m in mapping]})
        r.step(conn, "Attribution d'adresse : association à un nœud de labo", "succeeded", simulated=True,
               summary={"mapping": mapping})
        anchored = [h for h in targets if hosts.anchor_for(conn, h["address"])]
        for h in targets:
            if h not in anchored:
                conn.execute("UPDATE hosts SET deployment_state = 'unreachable' WHERE id = %s", (h["id"],))
                hosts.observe(conn, h, "ssh", {"error": "aucune ancre de clé d'hôte importée pour cette adresse"},
                              "unreachable", "worker", r.job["id"])
        stdin = None
        if anchored:
            work, stdin = _prepare_ansible(r, conn, f"{r.job['id']}-a{r.job['attempts']}", resolution, "collect",
                                           anchored)
    enrolled: list[dict[str, Any]] = []
    if anchored:
        run_id = f"{r.job['id']}-a{r.job['attempts']}"
        rc, summary = _ansible_run(r, run_id, "ansible", stdin)
        with r.tx() as conn:
            for h in anchored:
                ok, reason = _host_ok(summary, h["name"])
                if ok:
                    anchor = hosts.anchor_for(conn, h["address"])
                    conn.execute("UPDATE hosts SET deployment_state = 'enrolled', enrolled_at = now(), "
                                 "host_key_fingerprint = %s WHERE id = %s", (anchor["fingerprint"], h["id"]))
                    report = summary.get("reports", {}).get(h["name"], {})
                    hosts.observe(conn, h, "ssh", {"fingerprint": anchor["fingerprint"]}, "ok", "rackloom-collect",
                                  r.job["id"])
                    hosts.observe(conn, h, "facts", {"os": report.get("os"), "kernel": report.get("kernel"),
                                                     "init": report.get("init")}, "ok", "rackloom-collect", r.job["id"],
                                  r.redactor)
                    enrolled.append(h)
                else:
                    conn.execute("UPDATE hosts SET deployment_state = 'unreachable' WHERE id = %s", (h["id"],))
                    hosts.observe(conn, h, "ssh", {"error": reason}, "unreachable", "rackloom-collect", r.job["id"],
                                  r.redactor)
                r.step(conn, f"Connexion SSH vérifiée ({h['name']})", "succeeded" if ok else "failed",
                       summary={"host": h["name"], **({"reason": reason} if not ok else {})})
        r.cleanup(run_id)
    with r.tx() as conn:
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                 (Jsonb({"enrolled": [h["name"] for h in enrolled]}),))
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        if enrolled:
            queue.enqueue(conn, kind="ansible_configure", change=change, execution_id=r.job["execution_id"],
                          idempotency_key=f"ansible_configure:{r.job['execution_id']}",
                          payload={"host_ids": [str(h["id"]) for h in enrolled]})
        else:
            transition(conn, change, "partially_succeeded", reason_code="guest_unreachable", actor_type="worker",
                       actor_id=r.worker_id, severity="error",
                       detail={"message": "VM créée, configuration incomplète : aucun hôte joignable"})


def handle_ansible_configure(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        resolution = ctx["version"]["resolution"]
        ids = [UUID(i) for i in r.job["payload"]["host_ids"]]
        targets = conn.execute("SELECT * FROM hosts WHERE id = ANY(%s) AND change_id = %s ORDER BY name",
                               (ids, r.job["change_id"])).fetchall()
        targets = [h for h in targets if h["address"] and hosts.anchor_for(conn, h["address"])]
        run_id = str(r.job["id"])
        work, stdin = _prepare_ansible(r, conn, run_id, resolution, "configure", targets)
        r.fenced(conn, "dispatched_at = now(), provider_ref = %s", (run_id,))
    rc, summary = _ansible_run(r, run_id, "ansible", stdin)
    configured = []
    with r.tx() as conn:
        for h in targets:
            ok, reason = _host_ok(summary, h["name"])
            conn.execute("UPDATE hosts SET deployment_state = %s WHERE id = %s",
                         ("configured" if ok else "config_failed", h["id"]))
            r.step(conn, f"Configuration Nginx ({h['name']})", "succeeded" if ok else "failed",
                   summary={"host": h["name"], **({"reason": reason} if not ok else {})})
            if ok:
                configured.append(h)
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                 (Jsonb({"configured": [h["name"] for h in configured], "exit_code": rc}),))
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        queue.enqueue(conn, kind="verify", change=change, execution_id=r.job["execution_id"],
                      idempotency_key=f"verify:{r.job['id']}", payload={})
    r.cleanup(run_id)


def _evaluate_checks(checks: list[dict[str, Any]], report: dict[str, Any] | None,
                     probe: dict[str, Any] | None) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        kind = check["type"]
        if kind == "package_installed":
            ok = bool(report and report.get("package_nginx"))
            results.append({"type": kind, "ok": ok, "detail": (report or {}).get("package_nginx") or "absent"})
        elif kind == "service_active":
            ok = bool(report and str(report.get("process_nginx")).lower() == "true")
            results.append({"type": kind, "ok": ok, "detail": "processus nginx actif" if ok else "nginx arrêté",
                            "systemd": "non collecté (nœud de labo sans systemd)"})
        elif kind == "http_probe":
            ok = bool(probe and probe.get("ok"))
            results.append({"type": kind, "ok": ok, "detail": f"HTTP {probe.get('status')}" if probe else "non joint"})
        else:
            results.append({"type": kind, "ok": False, "detail": "contrôle non pris en charge"})
    return results


def handle_verify(r: JobContext) -> None:
    with r.tx() as conn:
        ctx = _load(conn, r.job)
        _admit(r, conn, ctx)
        if ctx["change"]["state"] == "executing":
            transition(conn, ctx["change"], "verifying", reason_code="verify_started", actor_type="worker",
                       actor_id=r.worker_id)
        resolution = ctx["version"]["resolution"]
        all_hosts = conn.execute("SELECT * FROM hosts WHERE change_id = %s ORDER BY name",
                                 (r.job["change_id"],)).fetchall()
        targets = [h for h in all_hosts if h["deployment_state"] in ("configured", "verified", "verify_failed")]
        run_id = f"{r.job['id']}-a{r.job['attempts']}"
        stdin = None
        if targets:
            work, stdin = _prepare_ansible(r, conn, run_id, resolution, "collect", targets)
    reports: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    if targets:
        rc, summary = _ansible_run(r, run_id, "ansible", stdin)
        reports = summary.get("reports", {})
        r.cleanup(run_id)
        probe_id = f"{r.job['id']}-p{r.job['attempts']}"
        pwork = r.workspace(probe_id)
        (pwork / "job.json").write_text(json.dumps({"action": "http_probe", "targets": [
            {"host": h["name"], "address": h["address"], "port": 80, "path": "/"} for h in targets]}))
        r.run({"run_id": probe_id, "kind": "probe", "timeout_seconds": 60})
        probes = {p["host"]: p for p in (read_json(pwork / "out" / "probe.json", []) or [])}
        r.cleanup(probe_id)
    verified = []
    with r.tx() as conn:
        for h in targets:
            report = reports.get(h["name"])
            results = _evaluate_checks(resolution["verification"], report, probes.get(h["name"]))
            ok = all(x["ok"] for x in results)
            if report:
                hosts.observe(conn, h, "facts", {"os": report.get("os"), "kernel": report.get("kernel"),
                                                 "init": report.get("init")}, "ok", "rackloom-collect", r.job["id"])
                hosts.observe(conn, h, "packages", {"nginx": report.get("package_nginx")},
                              "ok" if report.get("package_nginx") else "error", "rackloom-collect", r.job["id"])
                service_ok = next(x for x in results if x["type"] == "service_active")["ok"]
                hosts.observe(conn, h, "services", {"nginx": {"active": service_ok, "manager": "sysv",
                                                              "systemd": "non collecté"}},
                              "ok" if service_ok else "error", "rackloom-collect", r.job["id"])
                journal = str(report.get("journal") or "")[:16384]
                hosts.observe(conn, h, "journal", {"source": report.get("journal_source"),
                                                   "lines": journal.splitlines()[-50:]}, "ok", "rackloom-collect",
                              r.job["id"], r.redactor)
            else:
                hosts.observe(conn, h, "facts", {"error": "collecte impossible"}, "unreachable", "rackloom-collect",
                              r.job["id"])
            probe = probes.get(h["name"])
            if probe is not None:
                hosts.observe(conn, h, "probe", probe, "ok" if probe.get("ok") else "error", "probe", r.job["id"])
            conn.execute("UPDATE hosts SET deployment_state = %s WHERE id = %s",
                         ("verified" if ok else "verify_failed", h["id"]))
            r.step(conn, f"Vérification ({h['name']})", "succeeded" if ok else "failed",
                   summary={"checks": [{"type": x["type"], "ok": x["ok"], "detail": x["detail"]} for x in results]})
            if ok:
                verified.append(h)
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                 (Jsonb({"verified": [h["name"] for h in verified], "hosts": len(all_hosts)}),))
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        if change["state"] == "executing":
            change = transition(conn, change, "verifying", reason_code="verify_started", actor_type="worker",
                                actor_id=r.worker_id)
        complete = len(verified) == len(all_hosts) and all_hosts
        conn.execute("UPDATE executions SET finished_at = now(), state = %s WHERE id = %s",
                     ("succeeded" if complete else "partially_succeeded", r.job["execution_id"]))
        if complete:
            transition(conn, change, "succeeded", reason_code="verified", actor_type="worker", actor_id=r.worker_id,
                       detail={"message": f"Succès : {len(verified)} hôte(s) sain(s)"})
        else:
            transition(conn, change, "partially_succeeded", reason_code="verify_incomplete", actor_type="worker",
                       actor_id=r.worker_id, severity="error",
                       detail={"message": "VM créée, configuration incomplète",
                               "verified": [h["name"] for h in verified],
                               "failed": [h["name"] for h in all_hosts if h not in verified]})


# --- Réconciliation ----------------------------------------------------------------------

def handle_reconcile(r: JobContext) -> None:
    """Lecture seule : statut du runner auprès du launcher, puis state Terraform. Jamais de relance."""
    target_id = UUID(r.job["payload"]["job_id"])
    with r.tx() as conn:
        target = conn.execute("SELECT * FROM jobs WHERE id = %s", (target_id,)).fetchone()
        r.fenced(conn, "state = 'running'")
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        if change["state"] == "uncertain":
            transition(conn, change, "reconciling", reason_code="reconcile_started", actor_type="worker",
                       actor_id=r.worker_id)
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
            r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                     (Jsonb({"source": "launcher", "outcome": outcome}),))
        return
    # Runner introuvable : lecture du state par un runner en lecture seule.
    with r.tx() as conn:
        ctx = _load(conn, target, lock=False)
        resolution = ctx["version"]["resolution"]
    inspect_id = f"{r.job['id']}-i{r.job['attempts']}"
    work = _prepare_tf(r, inspect_id, resolution, {"action": "tf_inspect", "timeout_seconds": 120})
    r.run({"run_id": inspect_id, "kind": "terraform", "stack": ctx["stack"]["slug"], "timeout_seconds": 120})
    inspect = read_json(work / "out" / "inspect.json", {}) or {}
    r.cleanup(inspect_id)
    state = inspect.get("state") or {}
    planned = {vm["name"] for vm in resolution["vms"]}
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
        r.step(conn, "Conclusion à partir du state", "succeeded", summary={"conclusion": conclusion,
                                                                          "vms": sorted(present)})
        if conclusion == "applied":
            conn.execute("UPDATE jobs SET state = 'succeeded', finished_at = now(), result = %s WHERE id = %s",
                         (Jsonb({"vms": vms, "outcome": "applied", "source": "state"}), target_id))
            admission.release_stack(conn, target)
            conn.execute("UPDATE plans SET status = 'applied' WHERE id = %s", (plan["id"],))
            change = transition(conn, change, "executing", reason_code="reconciled_applied", actor_type="worker",
                                actor_id=r.worker_id, detail={"message": "Réconciliation : plan appliqué (state)"})
            queue.enqueue(conn, kind="guest_ready", change=change, execution_id=target["execution_id"],
                          stack_id=target["stack_id"], idempotency_key=f"guest_ready:{target['execution_id']}",
                          payload={"vms": vms})
        elif conclusion == "not_applied":
            conn.execute("UPDATE jobs SET state = 'failed', finished_at = now(), result = %s WHERE id = %s",
                         (Jsonb({"outcome": "not_applied", "source": "state"}), target_id))
            admission.release_stack(conn, target)
            transition(conn, change, "failed", reason_code="reconciled_not_applied", actor_type="worker",
                       actor_id=r.worker_id, severity="error",
                       detail={"message": "Réconciliation : plan non appliqué, aucune relance automatique"})
        else:
            transition(conn, change, "uncertain", reason_code="reconciled_indeterminate", actor_type="worker",
                       actor_id=r.worker_id, severity="error",
                       detail={"message": "Réconciliation indéterminée : intervention manuelle requise"})
        r.fenced(conn, "state = 'succeeded', finished_at = now(), result = %s",
                 (Jsonb({"conclusion": conclusion}),))


def _reconcile_other(r: JobContext, target: dict[str, Any], status: dict[str, Any]) -> None:
    """Configuration Ansible interrompue : aucune relance, l'opérateur demande une reprise approuvée."""
    with r.tx() as conn:
        conn.execute("UPDATE jobs SET state = 'failed', finished_at = now(), result = %s WHERE id = %s "
                     "AND state = 'uncertain'", (Jsonb({"outcome": "interrupted", "launcher": status["state"]}),
                                                  target["id"]))
        conn.execute("UPDATE hosts SET deployment_state = 'config_failed' WHERE change_id = %s "
                     "AND deployment_state = 'enrolled'", (r.job["change_id"],))
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (r.job["change_id"],)).fetchone()
        transition(conn, change, "executing", reason_code="reconciled_configure", actor_type="worker",
                   actor_id=r.worker_id)
        queue.enqueue(conn, kind="verify", change=change, execution_id=target["execution_id"],
                      idempotency_key=f"verify:reconcile:{target['id']}", payload={})
        r.fenced(conn, "state = 'succeeded', finished_at = now()")


HANDLERS = {
    "tf_plan": handle_tf_plan,
    "tf_apply": handle_tf_apply,
    "guest_ready": handle_guest_ready,
    "ansible_configure": handle_ansible_configure,
    "verify": handle_verify,
    "reconcile": handle_reconcile,
}


def catalog_ok(conn: psycopg.Connection) -> bool:
    return catalog.latest_template(conn, "ubuntu-web") is not None
