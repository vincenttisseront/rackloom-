"""Service launcher : API HTTP sur socket Unix, sans réseau ni accès à la base.

Seul composant ayant accès au socket du moteur de conteneurs. Démarrage :
`rackloom-launcher --uds /run/rackloom/launcher.sock`
"""

from __future__ import annotations

import argparse
import logging
import os

from fastapi import FastAPI, HTTPException, Query

from .backends import DockerBackend, LauncherBackend
from .spec import RUN_ID, RunSpec, RunStatus, SignalRequest

log = logging.getLogger("rackloom.launcher")


def create_app(backend: LauncherBackend) -> FastAPI:
    app = FastAPI(title="rackloom-launcher", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/health")
    def health() -> dict:
        return {"backend": backend.name, "images": backend.images()}

    @app.get("/v1/images")
    def images() -> dict[str, str]:
        return backend.images()

    @app.post("/v1/runs", response_model=RunStatus)
    def start(spec: RunSpec) -> RunStatus:
        try:
            status = backend.start(spec)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        log.info("run %s démarré (kind=%s job=%s corr=%s)", spec.run_id, spec.kind, spec.job_id,
                 spec.correlation_id)
        return status

    @app.get("/v1/runs", response_model=list[RunStatus])
    def list_runs(job_id: str = Query(pattern=RUN_ID)) -> list[RunStatus]:
        return backend.list(job_id)

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    def status(run_id: str) -> RunStatus:
        return backend.status(run_id)

    @app.post("/v1/runs/{run_id}/signal", response_model=RunStatus)
    def send_signal(run_id: str, body: SignalRequest) -> RunStatus:
        log.info("signal %s envoyé au run %s", body.signal, run_id)
        return backend.signal(run_id, body.signal)

    @app.delete("/v1/runs/{run_id}", status_code=204)
    def remove(run_id: str) -> None:
        backend.remove(run_id)

    return app


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--uds", default="/run/rackloom/launcher.sock")
    parser.add_argument("--docker-socket", default="/var/run/docker.sock")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    backend = DockerBackend(args.docker_socket)
    app = create_app(backend)
    if os.path.exists(args.uds):
        os.unlink(args.uds)
    old_umask = os.umask(0o007)
    try:
        uvicorn.run(app, uds=args.uds, log_level="info")
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    run()
