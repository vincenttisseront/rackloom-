"""Contexte d'exécution d'un job : bail et heartbeat, envoi au launcher, suivi, annulation.

Aucune transaction n'est ouverte pendant un appel au launcher ou l'attente d'un runner :
chaque écriture est une transaction courte, conditionnée par l'époque du bail.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from .. import events
from ..config import Settings
from ..db import Conn, transaction
from ..executions import queue
from ..redact import Redactor
from .launcher_client import Launcher

log = logging.getLogger("rackloom.worker")

STEP_LABELS = {
    "runner": "Runner",
    "terraform_init": "Initialisation Terraform (providers du miroir, lockfile en lecture seule)",
    "terraform_plan": "Calcul du plan Terraform",
    "terraform_show": "Lecture du plan",
    "terraform_apply": "Application du plan sauvegardé",
    "terraform_inspect": "Lecture du state",
    "ansible_play": "Exécution Ansible",
    "http_probe": "Sonde HTTP",
    "preflight_vcenter": "Préflight : accès vCenter",
    "preflight_state": "Préflight : backend de state",
    "preflight_template": "Préflight : prérequis du template",
    "preflight_repos": "Préflight : dépôts et rôles",
}


class Refused(Exception):
    """Admission refusée : la décision et ses effets sont enregistrés, le job s'arrête."""


class JobContext:
    def __init__(self, job: dict[str, Any], settings: Settings, launcher: Launcher, worker_id: str) -> None:
        self.job = job
        self.settings = settings
        self.launcher = launcher
        self.worker_id = worker_id
        self.lost = threading.Event()
        self.cancel_requested = threading.Event()
        self.redactor = Redactor()
        self._stop = threading.Event()
        self._hb: threading.Thread | None = None

    # --- Bail -----------------------------------------------------------------------------

    def start_heartbeat(self) -> None:
        self._hb = threading.Thread(target=self._heartbeat_loop, name=f"hb-{self.job['id']}", daemon=True)
        self._hb.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._hb is not None:
            self._hb.join(timeout=5)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.settings.job_heartbeat_seconds):
            try:
                with transaction() as conn:
                    row = queue.heartbeat(conn, self.job, self.settings.job_lease_seconds)
                    conn.execute("UPDATE workers SET heartbeat_at = now() WHERE id = %s", (self.worker_id,))
                if row["cancel_requested_at"] is not None:
                    self.cancel_requested.set()
            except queue.LeaseLost:
                log.warning("bail perdu pour le job %s : arrêt sans écriture", self.job["id"])
                self.lost.set()
                return
            except psycopg.Error:
                log.exception("heartbeat en échec (job %s)", self.job["id"])

    @contextmanager
    def tx(self) -> Iterator[Conn]:
        """Transaction courte. Un refus (`Refused`) valide ce qui a été écrit avant de se propager."""
        if self.lost.is_set():
            raise queue.LeaseLost(str(self.job["id"]))
        refused: Refused | None = None
        with transaction() as conn:
            try:
                yield conn
            except Refused as exc:
                refused = exc
        if refused is not None:
            raise refused

    def fenced(self, conn: Conn, sets: str, params: tuple = ()) -> dict[str, Any]:
        row = queue.fenced_update(conn, self.job, sets, params)
        self.job = row
        return row

    # --- Événements -----------------------------------------------------------------------

    def event_source(self, type_: str | None = None) -> str:
        """Source ADR de la chronologie selon le type de job (F21)."""
        if type_ == "job.blocked":
            return "admission"
        return {
            "preflight": "system",
            "tf_plan": "terraform",
            "tf_apply": "terraform",
            "tf_state_read": "terraform",
            "tf_import": "terraform",
            "guest_ready": "vcenter",
            "ansible_configure": "ansible",
            "verify": "verification",
            "reconcile": "system",
            "inventory_refresh": "vcenter",
        }.get(self.job["kind"], "job")

    def event(self, conn: Conn, type_: str, data: dict[str, Any], severity: str = "info",
              dedupe: str | None = None, source: str | None = None) -> None:
        events.emit(conn, type=type_, source=source or self.event_source(type_),
                    project_id=self.job["project_id"], correlation_id=self.job["change_id"],
                    job_id=self.job["id"], data=data, severity=severity, dedupe_key=dedupe,
                    redactor=self.redactor)

    def step(self, conn: Conn, name: str, state: str, *, simulated: bool = False,
             summary: dict[str, Any] | None = None, source: str | None = None) -> None:
        # Les résumés détaillés restent sur job_steps ; l'événement ne porte que l'essentiel (TB6 / F21).
        queue.add_step(conn, self.job["id"], name, state, simulated=simulated,
                       summary=self.redactor.data(summary or {}))
        payload: dict[str, Any] = {
            "kind": self.job["kind"], "step": name, "state": state, "simulated": simulated,
        }
        if summary and isinstance(summary.get("reason"), str):
            payload["reason"] = summary["reason"]
        self.event(conn, "job.step", payload, severity="error" if state == "failed" else "info",
                   source=source)

    # --- Espace de travail et runner -------------------------------------------------------

    def workspace(self, run_id: str) -> Path:
        path = self.settings.workspaces_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "out").mkdir(exist_ok=True)
        return path

    def cleanup(self, run_id: str) -> None:
        shutil.rmtree(self.settings.workspaces_dir / run_id, ignore_errors=True)
        try:
            self.launcher.remove(run_id)
        except Exception:  # noqa: BLE001 - ménage au mieux
            log.warning("suppression du run %s impossible", run_id)

    def run(self, spec: dict[str, Any], poll: float = 0.5) -> dict[str, Any]:
        """Démarre (ou retrouve) le run et attend sa fin en relayant étapes et annulation."""
        zone = self.job.get("required_zone") or self.settings.runner_zone or "local"
        spec = {"correlation_id": str(self.job["change_id"] or self.job["id"]), "job_id": str(self.job["id"]),
                "zone": zone, **spec}
        spec.setdefault("workspace", spec["run_id"])
        status = self.launcher.status(spec["run_id"])
        if status["state"] == "missing":
            status = self.launcher.start(spec)
        return self.wait(spec["run_id"], poll)

    def wait(self, run_id: str, poll: float = 0.5) -> dict[str, Any]:
        steps_file = self.settings.workspaces_dir / run_id / "out" / "steps.jsonl"
        offset = 0
        signalled = False
        while True:
            if self.lost.is_set():
                raise queue.LeaseLost(str(self.job["id"]))
            if self.cancel_requested.is_set() and not signalled:
                self.launcher.signal(run_id, "INT")
                signalled = True
                with self.tx() as conn:
                    self.event(conn, "job.cancel_signalled",
                               {"message": "Annulation demandée : signal transmis au runner, arrêt non garanti"},
                               severity="warning")
            offset = self._relay_steps(steps_file, offset)
            status = self.launcher.status(run_id)
            if status["state"] in ("exited", "missing"):
                self._relay_steps(steps_file, offset)
                return status
            time.sleep(poll)

    def _relay_steps(self, steps_file: Path, offset: int) -> int:
        if not steps_file.exists():
            return offset
        with steps_file.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        if not data:
            return offset
        complete = data[: data.rfind(b"\n") + 1]
        records = []
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        relevant = [r for r in records if r.get("step") != "runner" and r.get("status") in ("ok", "error")]
        if relevant:
            with self.tx() as conn:
                for rec in relevant:
                    label = STEP_LABELS.get(rec["step"], rec["step"])
                    summary = {k: v for k, v in rec.items() if k in ("exit_code", "changes", "resources",
                                                                     "playbook", "detail") and v not in ("", None)}
                    self.step(conn, label, "succeeded" if rec["status"] == "ok" else "failed", summary=summary)
        return offset + len(complete)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default
