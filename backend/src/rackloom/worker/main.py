"""Processus worker : réclame les jobs, les exécute via le launcher, fauche les baux expirés."""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from datetime import timedelta
from typing import Any

from .. import connectors, events
from ..changes.transitions import transition
from ..config import Settings, get_settings
from ..db import Jsonb, transaction
from ..executions import queue
from . import handlers
from .core import JobContext
from .launcher_client import Launcher, from_settings

log = logging.getLogger("rackloom.worker")


class Worker:
    def __init__(self, settings: Settings | None = None, launcher: Launcher | None = None,
                 worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.launcher = launcher or from_settings(self.settings)
        self.worker_id = worker_id or self.settings.worker_id or f"{socket.gethostname()}-{int(time.time())}"
        self.stop = threading.Event()

    def register(self) -> None:
        with transaction() as conn:
            connectors.refuse_simulated_in_production(conn, self.settings.profile)
        images = self.launcher.images()
        with transaction() as conn:
            conn.execute(
                "INSERT INTO workers (id, hostname, slots) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE "
                "SET heartbeat_at = now(), started_at = now()",
                (self.worker_id, socket.gethostname(), self.settings.worker_slots))
            for kind, image in images.items():
                conn.execute(
                    "INSERT INTO runner_images (job_kind, image_digest) VALUES (%s, %s) ON CONFLICT (job_kind) "
                    "DO UPDATE SET image_digest = EXCLUDED.image_digest, recorded_at = now()", (kind, image))

    # --- Exécution d'un job ---------------------------------------------------------------

    def claim(self, kinds: list[str] | None = None) -> dict[str, Any] | None:
        with transaction() as conn:
            return queue.claim(
                conn,
                self.worker_id,
                self.settings.job_lease_seconds,
                kinds,
                zone=self.settings.runner_zone,
            )

    def process(self, job: dict[str, Any]) -> None:
        ctx = JobContext(job, self.settings, self.launcher, self.worker_id)
        ctx.start_heartbeat()
        try:
            handlers.HANDLERS[job["kind"]](ctx)
        except handlers.Refused:
            pass
        except queue.LeaseLost:
            log.warning("job %s abandonné : bail perdu", job["id"])
        except Exception as exc:  # noqa: BLE001 - toute erreur est enregistrée sur le job
            log.exception("job %s en erreur", job["id"])
            self._fail(ctx, exc)
        finally:
            ctx.stop_heartbeat()

    def _fail(self, ctx: JobContext, exc: Exception) -> None:
        try:
            with transaction() as conn:
                job = ctx.job
                if job["dispatched_at"] is not None and job["class"] == "write":
                    ctx.fenced(conn, "state = 'uncertain'")
                    self._mark_uncertain(conn, job, "erreur du worker après envoi")
                    return
                ctx.fenced(conn, "state = 'failed', finished_at = now(), result = %s",
                           (Jsonb({"error": type(exc).__name__}),))
                if job["stack_id"]:
                    conn.execute("UPDATE stacks SET lock_job_id = NULL WHERE id = %s AND lock_job_id = %s",
                                 (job["stack_id"], job["id"]))
                ctx.event(conn, "job.failed", {"kind": job["kind"], "error": type(exc).__name__,
                                               "message": str(exc)[:300]}, severity="error")
                self._fail_change(conn, job, f"job {job['kind']} en échec ({type(exc).__name__})")
        except queue.LeaseLost:
            pass

    def _fail_change(self, conn, job: dict[str, Any], message: str) -> None:
        if not job["change_id"]:
            return
        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (job["change_id"],)).fetchone()
        target = {"planning": "plan_failed", "approved": "blocked", "executing": "partially_succeeded",
                  "verifying": "partially_succeeded", "reconciling": "uncertain"}.get(change["state"])
        if job["kind"] == "tf_apply" and change["state"] == "executing":
            target = "failed"
        if target:
            transition(conn, change, target, reason_code=f"{job['kind']}_error", actor_type="worker",
                       actor_id=self.worker_id, detail={"message": message}, severity="error")

    def _mark_uncertain(self, conn, job: dict[str, Any], reason: str) -> None:
        from uuid import UUID

        from .. import ipam

        change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (job["change_id"],)).fetchone()
        if change["state"] in ("executing", "verifying"):
            transition(conn, change, "uncertain", reason_code="lease_lost_after_dispatch", actor_type="worker",
                       actor_id=self.worker_id, severity="warning",
                       detail={"message": f"Résultat incertain ({reason}) : réconciliation en lecture seule, "
                                          "aucune relance automatique", "job_id": str(job["id"])})
        # F03 : ne pas libérer une IP potentiellement utilisée avant réconciliation.
        if job.get("kind") == "tf_apply":
            for reservation in ipam.for_change(conn, change["id"]):
                if reservation["state"] in ("reserved", "confirmed", "prepared"):
                    ipam.hold_uncertain(
                        conn, UUID(reservation["id"]), reason=reason, job_id=job["id"],
                    )
        # F37 : une écriture devenue incertaine compte pour le disjoncteur des connecteurs requis.
        for kind in connectors.circuit.required_kinds(job["kind"]):
            connectors.circuit.record_failure_for_kind(
                conn, kind, f"écriture {job['kind']} incertaine : {reason}",
                actor_type="worker", actor_id=self.worker_id,
            )
        queue.enqueue(conn, kind="reconcile", change=change, stack_id=None,
                      idempotency_key=f"reconcile:{job['id']}", payload={"job_id": str(job["id"])}, priority=10)

    # --- Faucheuse ------------------------------------------------------------------------

    def reap(self) -> int:
        count = 0
        with transaction() as conn:
            # F16 : runners de site sans heartbeat → stale + écritures envoyées → uncertain.
            from .. import runners as site_runners

            site_runners.mark_stale_and_uncertain(
                conn, stale_after=timedelta(seconds=self.settings.runner_stale_seconds)
            )
            for job in queue.expired_leases(conn):
                count += 1
                if job["kind"] == "reconcile":
                    queue.retry_later(conn, job, 0)
                    continue
                # max_attempts ne s'applique qu'aux jobs de lecture et aux écritures non envoyées.
                if job["dispatched_at"] is None or job["class"] == "read":
                    if job["attempts"] < job["max_attempts"]:
                        queue.retry_later(conn, job, 0)
                        events.emit(conn, type="job.requeued", source="system", project_id=job["project_id"],
                                    correlation_id=job["change_id"], job_id=job["id"], severity="warning",
                                    data={"kind": job["kind"],
                                          "message": "Bail expiré avant envoi : job remis en file"})
                        continue
                    conn.execute("UPDATE jobs SET state = 'failed', finished_at = now(), lease_owner = NULL "
                                 "WHERE id = %s", (job["id"],))
                    events.emit(conn, type="job.failed", source="system", project_id=job["project_id"],
                                correlation_id=job["change_id"], job_id=job["id"], severity="error",
                                data={"kind": job["kind"], "message": "Nombre maximal de tentatives atteint"})
                    self._fail_change(conn, job, "nombre maximal de tentatives atteint")
                    continue
                conn.execute("UPDATE jobs SET state = 'uncertain', lease_owner = NULL, updated_at = now() "
                             "WHERE id = %s", (job["id"],))
                events.emit(conn, type="job.uncertain", source="system", project_id=job["project_id"],
                            correlation_id=job["change_id"], job_id=job["id"], severity="warning",
                            data={"kind": job["kind"], "message": "Bail perdu après envoi au runner : résultat "
                                                                  "incertain, réconciliation lancée"})
                self._mark_uncertain(conn, job, "bail perdu après envoi")
        return count

    # --- Boucles ---------------------------------------------------------------------------

    def run_once(self, kinds: list[str] | None = None) -> dict[str, Any] | None:
        job = self.claim(kinds)
        if job is not None:
            self.process(job)
        return job

    def drain(self, max_jobs: int = 50, kinds: list[str] | None = None) -> list[str]:
        """Exécute les jobs disponibles jusqu'à épuisement (tests et démonstration scriptée)."""
        done = []
        for _ in range(max_jobs):
            job = self.run_once(kinds)
            if job is None:
                break
            done.append(job["kind"])
        return done

    def _slot(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.claim()
            except Exception:  # noqa: BLE001
                log.exception("réclamation impossible")
                self.stop.wait(self.settings.worker_poll_seconds * 5)
                continue
            if job is None:
                self.stop.wait(self.settings.worker_poll_seconds)
                continue
            self.process(job)

    def _reaper(self) -> None:
        while not self.stop.wait(self.settings.reaper_interval_seconds):
            try:
                self.reap()
                with transaction() as conn:
                    conn.execute("UPDATE workers SET heartbeat_at = now() WHERE id = %s", (self.worker_id,))
                    self._expire_plans(conn)
                    # F06 : rappels / expiration des baux (signalement + plan de retrait, sans destroy).
                    from .. import leases

                    leases.sweep(conn)
            except Exception:  # noqa: BLE001
                log.exception("faucheuse en erreur")

    def _expire_plans(self, conn) -> None:
        rows = conn.execute(
            "SELECT p.id, p.change_id FROM plans p JOIN changes c ON c.id = p.change_id "
            "WHERE p.status = 'ready' AND p.valid_until < now() AND c.state = 'plan_ready' FOR UPDATE OF p SKIP LOCKED"
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE plans SET status = 'expired' WHERE id = %s", (row["id"],))
            change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (row["change_id"],)).fetchone()
            transition(conn, change, "plan_expired", reason_code="plan_ttl", actor_type="system", actor_id=None,
                       detail={"message": "Plan expiré : un nouveau plan et une nouvelle approbation sont nécessaires"})

    def serve(self) -> None:
        self.register()
        threads = [threading.Thread(target=self._slot, name=f"slot-{i}", daemon=True)
                   for i in range(self.settings.worker_slots)]
        threads.append(threading.Thread(target=self._reaper, name="reaper", daemon=True))
        for t in threads:
            t.start()
        log.info("worker %s démarré (%d emplacements)", self.worker_id, self.settings.worker_slots)
        while not self.stop.wait(1):
            pass
        for t in threads:
            t.join(timeout=10)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.serve()


if __name__ == "__main__":
    main()
