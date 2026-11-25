"""Client du launcher. Le worker ne parle jamais directement au moteur de conteneurs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import httpx


class LauncherError(Exception):
    pass


class Launcher(Protocol):
    def images(self) -> dict[str, str]: ...
    def start(self, spec: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, run_id: str) -> dict[str, Any]: ...
    def list(self, job_id: str) -> list[dict[str, Any]]: ...
    def signal(self, run_id: str, sig: str) -> dict[str, Any]: ...
    def remove(self, run_id: str) -> None: ...


class SocketLauncher:
    def __init__(self, socket_path: Path) -> None:
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)),
                                   base_url="http://launcher", timeout=30)

    def _json(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            raise LauncherError(f"launcher : {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    def images(self) -> dict[str, str]:
        return self._json(self.client.get("/v1/images"))

    def start(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._json(self.client.post("/v1/runs", json=spec))

    def status(self, run_id: str) -> dict[str, Any]:
        return self._json(self.client.get(f"/v1/runs/{run_id}"))

    def list(self, job_id: str) -> list[dict[str, Any]]:
        return self._json(self.client.get("/v1/runs", params={"job_id": job_id}))

    def signal(self, run_id: str, sig: str) -> dict[str, Any]:
        return self._json(self.client.post(f"/v1/runs/{run_id}/signal", json={"signal": sig}))

    def remove(self, run_id: str) -> None:
        self._json(self.client.delete(f"/v1/runs/{run_id}"))


class InProcessLauncher:
    """Launcher en processus (backend « process »). Développement natif et tests uniquement."""

    def __init__(self, data_dir: Path) -> None:
        from rackloom_launcher.backends import ProcessBackend
        from rackloom_launcher.spec import RunSpec

        self.backend = ProcessBackend(data_dir)
        self.spec_cls = RunSpec

    def images(self) -> dict[str, str]:
        return self.backend.images()

    def start(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.backend.start(self.spec_cls(**spec)).model_dump()
        except (ValueError, RuntimeError) as exc:
            raise LauncherError(str(exc)) from exc

    def status(self, run_id: str) -> dict[str, Any]:
        return self.backend.status(run_id).model_dump()

    def list(self, job_id: str) -> list[dict[str, Any]]:
        return [s.model_dump() for s in self.backend.list(job_id)]

    def signal(self, run_id: str, sig: str) -> dict[str, Any]:
        return self.backend.signal(run_id, sig).model_dump()

    def remove(self, run_id: str) -> None:
        self.backend.remove(run_id)


def from_settings(settings) -> Launcher:
    if settings.launcher_mode == "inprocess":
        if settings.profile == "production":
            raise LauncherError("launcher en processus refusé en profil production")
        return InProcessLauncher(settings.data_dir)
    return SocketLauncher(settings.launcher_socket)
