"""Parcours D1 de bout en bout contre la pile Compose démarrée par `make demo`.

Passe uniquement par https://localhost:8443 (CA locale `.lab/ca.crt`, vérification TLS active) :
Alice décrit le besoin et soumet, Bruno approuve l'empreinte du plan, puis le script attend
le résultat du service. Lancer depuis la racine : `uv run --project backend python scripts/smoke-d1.py`.

`--restart-during KIND` (T25) : dès qu'un job de ce type est en cours, `docker compose down` des services
de l'appliance sans suppression de volumes, puis `up`. Si les sorties Ansible sont sur disque, la
réconciliation conclut sans relance ; sinon un succès partiel peut être repris (Alice demande, Bruno
approuve). Le script vérifie ensuite que les données sont conservées, que la modification aboutit
(reprise jusqu'à succès, ou réconciliation sans doublon) et qu'aucun job `tf_apply` ni hôte n'est
en double.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://localhost:8443"
API = f"{BASE}/api/v1"
TIMEOUT_S = 600
APPLIANCE = ["caddy", "api", "worker", "launcher", "postgres"]


def compose(*args: str) -> None:
    files = ["compose/compose.yaml", "compose/compose.demo.yaml"]
    if os.environ.get("RACKLOOM_COMPOSE_EXTRA"):
        files.append(os.environ["RACKLOOM_COMPOSE_EXTRA"])
    cmd = ["docker", "compose", *[a for f in files for a in ("-f", f)], *args]
    print("$", " ".join(cmd[2 + 2 * len(files):]))
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def credentials() -> dict[str, str]:
    lines = (ROOT / ".lab" / "demo-credentials.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split()[:2] for line in lines if line.strip() and not line.startswith("#"))


def session(email: str, password: str) -> httpx.Client:
    c = httpx.Client(verify=str(ROOT / ".lab" / "ca.crt"), headers={"Origin": BASE}, timeout=30)
    r = c.post(f"{API}/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def post(c: httpx.Client, path: str, body: dict) -> dict:
    r = c.post(f"{API}{path}", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    if r.status_code != 200:
        sys.exit(f"POST {path} → {r.status_code} {r.text}")
    return r.json()


def wait(c: httpx.Client, cid: str, done, label: str) -> dict:
    deadline = time.monotonic() + TIMEOUT_S
    view: dict = {"state": "inconnu"}
    while time.monotonic() < deadline:
        try:
            r = c.get(f"{API}/changes/{cid}")
        except httpx.TransportError:
            r = None
        if r is not None and r.status_code == 200:
            view = r.json()
            if done(view):
                return view
        time.sleep(2)
    sys.exit(f"délai dépassé en attendant : {label} (état {view['state']})")


def restart_during(alice: httpx.Client, cid: str, kind: str) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
        if any(j["kind"] == kind and j["state"] == "running" for j in jobs):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"aucun job {kind} en cours")
    print(f"T25 : job {kind} en cours, arrêt de l'appliance (volumes conservés)")
    compose("down", *APPLIANCE)
    compose("up", "-d", "--wait", *APPLIANCE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-during", metavar="KIND", help="T25 : redémarre l'appliance pendant ce job")
    args = parser.parse_args()
    creds = credentials()
    alice = session("alice@demo.invalid", creds["alice@demo.invalid"])
    bruno = session("bruno@demo.invalid", creds["bruno@demo.invalid"])

    need = {"schema": "rackloom.need/v1", "service": {"template": "ubuntu-web"}, "environment": None,
            "confirm_production": False,
            "inputs": {"lifetime_until": (date.today() + timedelta(days=14)).isoformat()}}
    change = post(alice, "/projects/vitrine/changes", {"title": "Smoke D1 (Compose)"})
    cid = change["id"]
    v = post(alice, f"/changes/{cid}/versions", {"need": need, "base_version_no": 0, "source": "webui"})
    print("révision :", v["change"]["revision"]["outcome"], "—", v["change"]["revision"]["summary"]["headline"])
    post(alice, f"/changes/{cid}/submit", {"revision_no": v["revision_no"]})

    view = wait(bruno, cid, lambda x: x["state"] in ("plan_ready", "plan_failed", "failed", "cancelled"), "plan prêt")
    if view["state"] != "plan_ready":
        jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
        sys.exit(f"plan non prêt ({view['state']}) : {[(j['kind'], j['state'], j.get('result')) for j in jobs]}")
    plan = view["plan"]
    print("plan :", plan["counts"], plan["vms"][0]["name"], plan["bundle_digest"][:19] + "…")
    post(bruno, f"/changes/{cid}/approvals",
         {"plan_id": plan["id"], "bundle_digest": plan["bundle_digest"], "decision": "approved"})

    changes_before = alice.get(f"{API}/projects/vitrine/changes").json()
    if args.restart_during:
        restart_during(alice, cid, args.restart_during)

    terminal = ("succeeded", "partially_succeeded", "failed", "cancelled")
    view = wait(alice, cid, lambda x: x["state"] in terminal, "fin de la modification")
    jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
    # Interrompu sans sorties exploitables : reprise humaine. Si la réconciliation a déjà conclu
    # depuis le disque (F28), on n'ajoute pas de reprise (hôtes peut-être hors états retryables).
    reconciled_already = (
        view["state"] == "partially_succeeded"
        and any(j["kind"] == "reconcile" and j["state"] == "succeeded" for j in jobs)
        and any(j["kind"] == args.restart_during and j["state"] in ("failed", "succeeded")
                for j in jobs)
    ) if args.restart_during else False
    if args.restart_during and view["state"] == "partially_succeeded" and not reconciled_already:
        print("T25 : succès partiel après redémarrage — demande de reprise ansible_configure")
        retry = post(alice, f"/changes/{cid}/retries", {"step": "ansible_configure"})
        post(bruno, f"/changes/{cid}/approvals",
             {"retry_id": retry["retry_id"], "bundle_digest": retry["bundle_digest"], "decision": "approved"})
        view = wait(alice, cid, lambda x: x["state"] in terminal, "fin après reprise")
        jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
    dep = view["deployment"]
    print("modification :", view["state"], "| service :", dep["service_status"], "—", dep.get("service_reason"))
    for job in jobs:
        simulated = [s["name"] for s in job.get("steps", []) if s["simulated"]]
        print(f"  {job['kind']:<18} {job['state']:<10} tentatives={job['attempts']}",
              f"SIMULÉ : {', '.join(simulated)}" if simulated else "")
    resumed = view["state"] == "succeeded" and dep["service_status"] == "ready"
    print("D1 (Compose) :", "OK" if resumed else "ÉCHEC" if not args.restart_during else
          ("OK (reprise)" if resumed else "réconciliation attendue"))
    if args.restart_during:
        hosts = alice.get(f"{API}/deployments/{dep['id']}").json()["hosts"]
        changes_after = alice.get(f"{API}/projects/vitrine/changes").json()
        kept = {c["id"] for c in changes_before} <= {c["id"] for c in changes_after}
        applies = Counter(j["kind"] for j in jobs)["tf_apply"]
        names = [h["name"] for h in hosts]
        # lot-1-plan T25 : reprise *ou* réconciliation, données conservées, aucun doublon.
        reconciled = (
            view["state"] == "partially_succeeded"
            and any(j["kind"] == "reconcile" and j["state"] == "succeeded" for j in jobs)
            and any(j["kind"] == args.restart_during and j["state"] in ("failed", "succeeded") for j in jobs)
        )
        print(f"T25 : données conservées={kept}, jobs tf_apply={applies}, hôtes={names}",
              f"reprise={resumed} réconcilié={reconciled}")
        unique = len(names) == len(set(names)) == plan["counts"]["create"]
        ok = (resumed or reconciled) and kept and applies == 1 and unique
        print("T25 (redémarrage) :", "OK" if ok else "ÉCHEC")
    else:
        ok = resumed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
