from __future__ import annotations

import logging
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path
from typing import Protocol

import httpx

from .spec import PROFILES, RunSpec, RunStatus

log = logging.getLogger(__name__)

RUNNER_UID = 10001


class LauncherBackend(Protocol):
    name: str

    def images(self) -> dict[str, str]: ...
    def start(self, spec: RunSpec) -> RunStatus: ...
    def status(self, run_id: str) -> RunStatus: ...
    def cancel(self, run_id: str) -> RunStatus: ...
    def remove(self, run_id: str) -> None: ...


class DockerBackend:
    """Pilote le moteur Docker via son API sur socket Unix. Utilisé dans Compose."""

    name = "docker"

    def __init__(self, socket_path: str = "/var/run/docker.sock") -> None:
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url="http://docker/v1.45", timeout=30
        )
        self.volumes = {
            "work": os.environ.get("RACKLOOM_VOLUME_WORKSPACES", "rackloom_workspaces"),
            "state": os.environ.get("RACKLOOM_VOLUME_TFSTATE", "rackloom_tfstate"),
            "infra": os.environ.get("RACKLOOM_VOLUME_DEMO_INFRA", "rackloom_demo_infra"),
        }
        self.local_roots = {
            "work": Path(os.environ.get("RACKLOOM_LAUNCHER_WORKSPACES", "/volumes/workspaces")),
            "state": Path(os.environ.get("RACKLOOM_LAUNCHER_TFSTATE", "/volumes/tfstate")),
            "infra": Path(os.environ.get("RACKLOOM_LAUNCHER_DEMO_INFRA", "/volumes/demo-infra")),
        }
        self.networks = {
            "none": "none",
            "rn-lab": os.environ.get("RACKLOOM_NETWORK_RN_LAB", "rackloom_rn-lab"),
        }
        self._image_ids: dict[str, str] = {}

    def images(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for kind, profile in PROFILES.items():
            ref = os.environ.get(profile.image_env)
            if not ref:
                continue
            resp = self.client.get(f"/images/{ref}/json")
            if resp.status_code == 200:
                result[kind] = resp.json()["Id"]
        self._image_ids = result
        return result

    def _subpath(self, mount: str, spec: RunSpec) -> str:
        if mount == "work":
            return spec.run_id
        if not spec.stack:
            raise ValueError("stack requis pour ce type de job")
        return spec.stack

    def start(self, spec: RunSpec) -> RunStatus:
        profile = PROFILES[spec.kind]
        image = (self._image_ids or self.images()).get(spec.kind)
        if not image:
            raise RuntimeError(f"aucune image approuvée pour {spec.kind}")
        mounts = []
        env = [f"{k}={v}" for k, v in profile.extra_env.items()]
        targets = {"work": "/work", "state": "/state", "infra": "/infra"}
        for mount in profile.mounts:
            sub = self._subpath(mount, spec)
            local = self.local_roots[mount] / sub
            local.mkdir(parents=True, exist_ok=True)
            os.chown(local, RUNNER_UID, RUNNER_UID)
            mounts.append({
                "Type": "volume", "Source": self.volumes[mount], "Target": targets[mount],
                "VolumeOptions": {"Subpath": sub},
            })
            env.append(f"RACKLOOM_{mount.upper()}={targets[mount]}")
        network = self.networks[profile.network]
        body = {
            "Image": image,
            "User": f"{RUNNER_UID}:{RUNNER_UID}",
            "Env": env,
            "WorkingDir": "/work",
            "Labels": {
                "io.rackloom.run_id": spec.run_id,
                "io.rackloom.job_id": spec.job_id,
                "io.rackloom.correlation_id": spec.correlation_id,
                "io.rackloom.kind": spec.kind,
                "io.rackloom.zone": spec.zone,
            },
            "NetworkDisabled": network == "none",
            "HostConfig": {
                "NetworkMode": network,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "ReadonlyRootfs": True,
                "Privileged": False,
                "Init": True,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
                "Memory": profile.memory_mb * 1024 * 1024,
                "MemorySwap": profile.memory_mb * 1024 * 1024,
                "NanoCpus": int(profile.cpus * 1e9),
                "PidsLimit": profile.pids,
                "Mounts": mounts,
                "AutoRemove": False,
            },
            "StopTimeout": 20,
        }
        resp = self.client.post("/containers/create", params={"name": f"rl-run-{spec.run_id}"}, json=body)
        if resp.status_code == 409:
            return self.status(spec.run_id)
        resp.raise_for_status()
        container_id = resp.json()["Id"]
        self.client.post(f"/containers/{container_id}/start").raise_for_status()
        return self.status(spec.run_id)

    def status(self, run_id: str) -> RunStatus:
        resp = self.client.get(f"/containers/rl-run-{run_id}/json")
        if resp.status_code == 404:
            return RunStatus(run_id=run_id, state="missing")
        resp.raise_for_status()
        st = resp.json()["State"]
        state = {"created": "created", "running": "running", "exited": "exited", "dead": "exited"}.get(
            st["Status"], "running"
        )
        return RunStatus(
            run_id=run_id,
            state=state,
            exit_code=st.get("ExitCode") if state == "exited" else None,
            started_at=st.get("StartedAt"),
            finished_at=st.get("FinishedAt") if state == "exited" else None,
            oom_killed=bool(st.get("OOMKilled")),
        )

    def cancel(self, run_id: str) -> RunStatus:
        self.client.post(f"/containers/rl-run-{run_id}/kill", params={"signal": "SIGTERM"})
        return self.status(run_id)

    def remove(self, run_id: str) -> None:
        self.client.delete(f"/containers/rl-run-{run_id}", params={"force": "true"})


class ProcessBackend:
    """Sous-processus local avec limites rlimit. Développement et tests uniquement.

    Refusé en profil production : il n'isole ni le réseau ni le système de fichiers.
    """

    name = "process"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.procs: dict[str, subprocess.Popen] = {}

    def images(self) -> dict[str, str]:
        return {kind: "process:local" for kind in PROFILES}

    def _dirs(self, spec: RunSpec) -> dict[str, Path]:
        dirs = {"work": self.data_dir / "workspaces" / spec.run_id}
        if spec.stack:
            dirs["state"] = self.data_dir / "tfstate" / spec.stack
            dirs["infra"] = self.data_dir / "demo-infra" / spec.stack
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    def start(self, spec: RunSpec) -> RunStatus:
        if spec.run_id in self.procs:
            return self.status(spec.run_id)
        profile = PROFILES[spec.kind]
        dirs = self._dirs(spec)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        for mount in profile.mounts:
            env[f"RACKLOOM_{mount.upper()}"] = str(dirs[mount])
        if os.environ.get("RACKLOOM_PROCESS_TF_CLI_CONFIG_FILE"):
            env["TF_CLI_CONFIG_FILE"] = os.environ["RACKLOOM_PROCESS_TF_CLI_CONFIG_FILE"]
        if os.environ.get("TF_PLUGIN_CACHE_DIR"):
            env["TF_PLUGIN_CACHE_DIR"] = os.environ["TF_PLUGIN_CACHE_DIR"]
        timeout = spec.timeout_seconds

        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 5))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
            os.setsid()

        log_file = (dirs["work"] / "runner.stderr").open("ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "rackloom.runner.main"],
            cwd=dirs["work"], env=env, stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=limits,  # noqa: PLW1509 - mode développement uniquement
        )
        self.procs[spec.run_id] = proc
        return RunStatus(run_id=spec.run_id, state="running")

    def status(self, run_id: str) -> RunStatus:
        proc = self.procs.get(run_id)
        if proc is not None:
            code = proc.poll()
            if code is None:
                return RunStatus(run_id=run_id, state="running")
            return RunStatus(run_id=run_id, state="exited", exit_code=code)
        exit_file = self.data_dir / "workspaces" / run_id / "out" / "exit_code"
        if exit_file.exists():
            return RunStatus(run_id=run_id, state="exited", exit_code=int(exit_file.read_text() or 1))
        return RunStatus(run_id=run_id, state="missing")

    def cancel(self, run_id: str) -> RunStatus:
        proc = self.procs.get(run_id)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        return self.status(run_id)

    def remove(self, run_id: str) -> None:
        proc = self.procs.pop(run_id, None)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
