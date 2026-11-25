"""Runner Rackloom : actions fermées sur Terraform CLI et Ansible Runner.

Chemins logiques fournis par le launcher via l'environnement :
  RACKLOOM_WORK   espace de travail jetable du job (lecture/écriture)
  RACKLOOM_STATE  dossier du state Terraform du stack (actions terraform)
  RACKLOOM_INFRA  dossier jetable des VM simulées du stack (démo)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ACTIONS = {"tf_plan", "tf_apply", "tf_inspect", "ansible_play", "http_probe"}

MAX_LOG_BYTES = 256 * 1024


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"variable {name} absente")
    return Path(value)


def _emit(out: Path, step: str, status: str, detail: str = "", **data) -> None:
    """Étapes structurées lues par le worker (jamais de sortie brute dans les événements)."""
    record = {"ts": time.time(), "step": step, "status": status, "detail": detail[:500], **data}
    with (out / "steps.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


_CURRENT: subprocess.Popen | None = None


def _forward(signum: int, _frame) -> None:
    """Relaie l'annulation à l'outil en cours (Terraform s'arrête proprement sur SIGINT)."""
    if _CURRENT is not None and _CURRENT.poll() is None:
        _CURRENT.send_signal(signum)


def _run(cmd: list[str], cwd: Path, log: Path, env: dict[str, str], timeout: int) -> int:
    global _CURRENT
    with log.open("ab") as fh:
        fh.write(f"$ {' '.join(cmd)}\n".encode())
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
        _CURRENT = proc
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            fh.write("\n[rackloom] délai d'exécution dépassé\n".encode())
            rc = 124
        finally:
            _CURRENT = None
    if log.stat().st_size > MAX_LOG_BYTES:
        data = log.read_bytes()
        log.write_bytes(data[:MAX_LOG_BYTES // 2] + "\n[...tronqué...]\n".encode() + data[-MAX_LOG_BYTES // 2:])
    return rc


def _tf_env(work: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work / "home"),
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
        "TF_DATA_DIR": str(work / "tf" / ".terraform"),
    }
    for key in ("TF_CLI_CONFIG_FILE", "TF_PLUGIN_CACHE_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    (work / "home").mkdir(exist_ok=True)
    return env


def _tf_init(job: dict, work: Path, out: Path, env: dict[str, str], timeout: int) -> int:
    state = _env_path("RACKLOOM_STATE")
    tfdir = work / "tf"
    override = tfdir / "zz_rackloom_backend_override.tf"
    override.write_text('terraform {\n  backend "local" {}\n}\n', encoding="utf-8")
    rc = _run(
        [job["terraform_bin"], "init", "-input=false", "-lockfile=readonly",
         f"-backend-config=path={state / 'terraform.tfstate'}"],
        tfdir, out / "terraform.log", env, timeout,
    )
    _emit(out, "terraform_init", "ok" if rc == 0 else "error", exit_code=rc)
    return rc


def _tf_show_state(job: dict, work: Path, out: Path, env: dict[str, str], timeout: int) -> dict | None:
    state_file = _env_path("RACKLOOM_STATE") / "terraform.tfstate"
    if not state_file.exists():
        return {"exists": False, "lineage": None, "serial": None, "resources": []}
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    resources = []
    for res in raw.get("resources", []):
        if res.get("mode") != "managed":
            continue
        for inst in res.get("instances", []):
            attrs = inst.get("attributes", {})
            address = f"{res['type']}.{res['name']}"
            if "index_key" in inst:
                address += f'["{inst["index_key"]}"]'
            resources.append({
                "address": address,
                "type": res["type"],
                "id": attrs.get("id"),
                "filename": attrs.get("filename"),
                "content_sha256": attrs.get("content_sha256"),
            })
    return {"exists": True, "lineage": raw.get("lineage"), "serial": raw.get("serial"), "resources": resources}


def action_tf_plan(job: dict, work: Path, out: Path, timeout: int, secret: dict) -> int:
    env = _tf_env(work)
    rc = _tf_init(job, work, out, env, timeout)
    if rc != 0:
        return rc
    infra = _env_path("RACKLOOM_INFRA")
    variables = dict(job["variables"])
    variables["labvms_dir"] = str(infra)
    (work / "tf" / "rackloom.auto.tfvars.json").write_text(json.dumps(variables), encoding="utf-8")
    state_before = _tf_show_state(job, work, out, env, timeout)
    (out / "state_before.json").write_text(json.dumps(state_before), encoding="utf-8")
    rc = _run(
        [job["terraform_bin"], "plan", "-input=false", "-lock-timeout=30s", "-detailed-exitcode",
         f"-out={out / 'plan.bin'}"],
        work / "tf", out / "terraform.log", env, timeout,
    )
    # -detailed-exitcode : 0 = aucun changement, 2 = changements, 1 = erreur
    if rc not in (0, 2):
        _emit(out, "terraform_plan", "error", exit_code=rc)
        return rc
    _emit(out, "terraform_plan", "ok", changes=(rc == 2))
    with (out / "plan.json").open("wb") as fh:
        show = subprocess.run(
            [job["terraform_bin"], "show", "-json", str(out / "plan.bin")],
            cwd=work / "tf", env=env, stdout=fh, stderr=subprocess.PIPE, timeout=timeout,
        )
    if show.returncode != 0:
        _emit(out, "terraform_show", "error", exit_code=show.returncode)
        return show.returncode
    _emit(out, "terraform_show", "ok")
    return 0


def action_tf_apply(job: dict, work: Path, out: Path, timeout: int, secret: dict) -> int:
    env = _tf_env(work)
    rc = _tf_init(job, work, out, env, timeout)
    if rc != 0:
        return rc
    plan = work / "in" / "plan.bin"
    if not plan.exists():
        _emit(out, "terraform_apply", "error", "fichier de plan approuvé absent")
        return 3
    _emit(out, "terraform_apply", "started")
    rc = _run(
        [job["terraform_bin"], "apply", "-input=false", "-lock-timeout=30s", str(plan)],
        work / "tf", out / "terraform.log", env, timeout,
    )
    _emit(out, "terraform_apply", "ok" if rc == 0 else "error", exit_code=rc)
    state_after = _tf_show_state(job, work, out, env, timeout)
    (out / "state_after.json").write_text(json.dumps(state_after), encoding="utf-8")
    return rc


def action_tf_inspect(job: dict, work: Path, out: Path, timeout: int, secret: dict) -> int:
    """Lecture seule : state courant et présence réelle des ressources simulées."""
    env = _tf_env(work)
    state = _tf_show_state(job, work, out, env, timeout)
    infra = _env_path("RACKLOOM_INFRA")
    observed = []
    for res in (state or {}).get("resources", []):
        filename = res.get("filename")
        present = False
        sha = None
        if filename:
            path = infra / Path(filename).name
            if path.exists():
                present = True
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
        observed.append({"address": res["address"], "present": present, "content_sha256": sha})
    (out / "inspect.json").write_text(json.dumps({"state": state, "observed": observed}), encoding="utf-8")
    _emit(out, "terraform_inspect", "ok", resources=len(observed))
    return 0


def _start_agent(private_key: str) -> tuple[dict[str, str], int]:
    """Agent SSH éphémère : la clé reçue sur stdin n'est jamais écrite sur disque."""
    sock = Path(tempfile.mkdtemp(prefix="rl-agent-")) / "agent.sock"
    proc = subprocess.run(["ssh-agent", "-s", "-a", str(sock)], capture_output=True, text=True, timeout=10,
                          check=True)
    pid = int(proc.stdout.split("SSH_AGENT_PID=", 1)[1].split(";", 1)[0])
    env = {"SSH_AUTH_SOCK": str(sock)}
    key = private_key if private_key.endswith("\n") else private_key + "\n"
    subprocess.run(["ssh-add", "-q", "-"], input=key, text=True, env={**os.environ, **env}, timeout=10,
                   check=True, capture_output=True)
    return env, pid


def action_ansible_play(job: dict, work: Path, out: Path, timeout: int, secret: dict) -> int:
    """Ansible Runner sur un playbook approuvé, inventaire limité, clé d'hôte ancrée."""
    private = work / "ansible"
    home = work / "home"
    home.mkdir(exist_ok=True)
    identity = home / "identity.pub"
    identity.write_text(job["ssh_public_key"] + "\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOCAL_TEMP": str(home / ".ansible-tmp"),
        "ANSIBLE_SSH_ARGS": (
            f"-o UserKnownHostsFile={private / 'env' / 'known_hosts'} -o StrictHostKeyChecking=yes "
            f"-o GlobalKnownHostsFile=/dev/null -o ControlMaster=no -o IdentitiesOnly=yes "
            f"-o IdentityFile={identity} -o ConnectTimeout=10"
        ),
    }
    agent_pid = None
    if secret.get("ssh_private_key"):
        agent_env, agent_pid = _start_agent(secret["ssh_private_key"])
        env.update(agent_env)
    cmd = [
        job.get("ansible_runner_bin", "ansible-runner"), "run", str(private),
        "-p", job["playbook"], "--ident", "run", "--rotate-artifacts", "1",
    ]
    if job.get("check_mode"):
        cmd += ["--cmdline", "--check --diff"]
    _emit(out, "ansible_play", "started", playbook=job["playbook"], check_mode=bool(job.get("check_mode")))
    try:
        rc = _run(cmd, work, out / "ansible.log", env, timeout)
    finally:
        if agent_pid:
            try:
                os.kill(agent_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    summary = _ansible_summary(private / "artifacts" / "run")
    (out / "ansible_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    shutil.rmtree(private / "artifacts", ignore_errors=True)
    _emit(out, "ansible_play", "ok" if rc == 0 else "error", exit_code=rc)
    return rc


def _ansible_summary(artifacts: Path) -> dict:
    hosts: dict[str, dict] = {}
    facts: dict[str, dict] = {}
    events_dir = artifacts / "job_events"
    if events_dir.exists():
        for path in sorted(events_dir.glob("*.json")):
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            data = ev.get("event_data", {})
            host = data.get("host")
            name = ev.get("event", "")
            if ev.get("event") == "playbook_on_stats":
                for key in ("ok", "changed", "failures", "dark", "skipped"):
                    for h, n in (data.get(key) or {}).items():
                        hosts.setdefault(h, {})[key] = n
            if host and name in ("runner_on_ok", "runner_on_failed", "runner_on_unreachable"):
                result = data.get("res", {}) or {}
                task = data.get("task", "")
                hosts.setdefault(host, {}).setdefault("tasks", []).append({
                    "task": task[:120],
                    "status": name.removeprefix("runner_on_"),
                    "changed": bool(result.get("changed")),
                    "msg": str(result.get("msg", ""))[:300],
                })
                rk = result.get("ansible_facts", {}).get("rackloom_report")
                if isinstance(rk, dict):
                    facts[host] = rk
    status = None
    status_file = artifacts / "status"
    if status_file.exists():
        status = status_file.read_text(encoding="utf-8").strip()
    return {"status": status, "hosts": hosts, "reports": facts}


def action_http_probe(job: dict, work: Path, out: Path, timeout: int, secret: dict) -> int:
    import urllib.error
    import urllib.request

    results = []
    for target in job["targets"]:
        url = f"http://{target['address']}:{int(target['port'])}{target.get('path', '/')}"
        started = time.time()
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - cible enrôlée, réseau rn-lab
                results.append({"host": target["host"], "status": resp.status, "ok": resp.status == 200,
                                "latency_ms": int((time.time() - started) * 1000)})
        except (urllib.error.URLError, OSError) as exc:
            results.append({"host": target["host"], "status": None, "ok": False, "error": type(exc).__name__})
    (out / "probe.json").write_text(json.dumps(results), encoding="utf-8")
    _emit(out, "http_probe", "ok" if all(r["ok"] for r in results) else "error")
    return 0 if all(r["ok"] for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    work = _env_path("RACKLOOM_WORK")
    out = work / "out"
    out.mkdir(exist_ok=True)
    secret: dict = {}
    if os.environ.get("RACKLOOM_STDIN_SECRET") == "1":
        secret = json.loads(sys.stdin.read() or "{}")
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    action = job.get("action")
    if action not in ACTIONS:
        _emit(out, "runner", "error", f"action refusée : {action!r}")
        return 64
    timeout = min(int(job.get("timeout_seconds", 600)), int(os.environ.get("RACKLOOM_TIMEOUT", "3600")))
    _emit(out, "runner", "started", action=action)
    handler = globals()[f"action_{action}"]
    try:
        rc = handler(job, work, out, timeout, secret)
    except Exception as exc:  # noqa: BLE001 - toute erreur devient un code et une étape lisible
        _emit(out, "runner", "error", f"{type(exc).__name__}")
        rc = 70
    finally:
        secret.clear()
    (out / "exit_code").write_text(str(rc), encoding="utf-8")
    _emit(out, "runner", "finished", exit_code=rc)
    # Ménage du cache provider/plugins : le dossier de travail reste jetable.
    shutil.rmtree(work / "tf" / ".terraform", ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
