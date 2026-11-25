from __future__ import annotations

import json
import logging
import os
import resource
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Protocol

import httpx

from .spec import PROFILES, RunSpec, RunStatus

log = logging.getLogger(__name__)

RUNNER_UID = 10001
SIGNALS = {"INT": signal.SIGINT, "TERM": signal.SIGTERM}


class LauncherBackend(Protocol):
    name: str

    def images(self) -> dict[str, str]: ...
    def start(self, spec: RunSpec) -> RunStatus: ...
    def status(self, run_id: str) -> RunStatus: ...
    def list(self, job_id: str) -> list[RunStatus]: ...
    def signal(self, run_id: str, sig: str) -> RunStatus: ...
    def remove(self, run_id: str) -> None: ...


def _timeout(spec: RunSpec) -> int:
    return min(spec.timeout_seconds, PROFILES[spec.kind].max_timeout)


class DockerBackend:
    """Pilote le moteur de conteneurs via son API sur socket Unix. Utilisé dans Compose."""

    name = "docker"
    api = "/v1.45"

    def __init__(self, socket_path: str = "/var/run/docker.sock") -> None:
        self.socket_path = socket_path
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url=f"http://docker{self.api}", timeout=30
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
            return spec.workspace
        if not spec.stack:
            raise ValueError("stack requis pour ce type de job")
        return spec.stack

    def _attach_stdin(self, container_id: str) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(self.socket_path)
        request = (f"POST {self.api}/containers/{container_id}/attach?stream=1&stdin=1 HTTP/1.1\r\n"
                   "Host: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n")
        sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("attache stdin refusée")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line and b" 200 " not in status_line:
            raise RuntimeError("attache stdin refusée")
        return sock

    def start(self, spec: RunSpec) -> RunStatus:
        profile = PROFILES[spec.kind]
        image = (self._image_ids or self.images()).get(spec.kind)
        if not image:
            raise RuntimeError(f"aucune image approuvée pour {spec.kind}")
        mounts = []
        env = [f"{k}={v}" for k, v in profile.extra_env.items()]
        env.append(f"RACKLOOM_TIMEOUT={_timeout(spec)}")
        targets = {"work": "/work", "state": "/state", "infra": "/labvms"}
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
        if spec.stdin_secret is not None:
            env.append("RACKLOOM_STDIN_SECRET=1")
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
            "OpenStdin": spec.stdin_secret is not None,
            "StdinOnce": spec.stdin_secret is not None,
            "AttachStdin": spec.stdin_secret is not None,
            "HostConfig": {
                "NetworkMode": network,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "ReadonlyRootfs": True,
                "Privileged": False,
                "Init": True,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m", "/run/secrets": "rw,noexec,nosuid,size=1m"},  # noqa: S108
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
        sock = self._attach_stdin(container_id) if spec.stdin_secret is not None else None
        try:
            self.client.post(f"/containers/{container_id}/start").raise_for_status()
            if sock is not None:
                sock.sendall(spec.stdin_secret.encode())
                sock.shutdown(socket.SHUT_WR)
        finally:
            if sock is not None:
                sock.close()
        return self.status(spec.run_id)

    def _to_status(self, run_id: str, info: dict) -> RunStatus:
        st = info["State"]
        state = {"created": "created", "running": "running", "exited": "exited", "dead": "exited"}.get(
            st["Status"], "running"
        )
        return RunStatus(
            run_id=run_id,
            job_id=info.get("Config", {}).get("Labels", {}).get("io.rackloom.job_id"),
            state=state,
            exit_code=st.get("ExitCode") if state == "exited" else None,
            started_at=st.get("StartedAt"),
            finished_at=st.get("FinishedAt") if state == "exited" else None,
            oom_killed=bool(st.get("OOMKilled")),
        )

    def status(self, run_id: str) -> RunStatus:
        resp = self.client.get(f"/containers/rl-run-{run_id}/json")
        if resp.status_code == 404:
            return RunStatus(run_id=run_id, state="missing")
        resp.raise_for_status()
        return self._to_status(run_id, resp.json())

    def list(self, job_id: str) -> list[RunStatus]:
        resp = self.client.get("/containers/json", params={
            "all": "1", "filters": json.dumps({"label": [f"io.rackloom.job_id={job_id}"]})})
        resp.raise_for_status()
        return [self.status(c["Labels"]["io.rackloom.run_id"]) for c in resp.json()]

    def signal(self, run_id: str, sig: str) -> RunStatus:
        self.client.post(f"/containers/rl-run-{run_id}/kill", params={"signal": f"SIG{sig}"})
        return self.status(run_id)

    def remove(self, run_id: str) -> None:
        self.client.delete(f"/containers/rl-run-{run_id}", params={"force": "true"})


class ProcessBackend:
    """Sous-processus local avec limites rlimit. Développement et tests uniquement.

    Refusé en profil production : il n'isole ni le réseau ni le système de fichiers.
    """

    name = "process"

    def __init__(self, data_dir: Path, runner_script: Path | None = None) -> None:
        self.data_dir = data_dir
        self.runner_script = runner_script or Path(
            os.environ.get("RACKLOOM_RUNNER_SCRIPT")
            or Path(__file__).resolve().parents[3] / "runners" / "common" / "rackloom_runner.py")
        self.procs: dict[str, subprocess.Popen] = {}

    def images(self) -> dict[str, str]:
        return {kind: "process:local" for kind in PROFILES}

    def _work(self, run_id: str) -> Path:
        return self.data_dir / "workspaces" / run_id

    def _dirs(self, spec: RunSpec) -> dict[str, Path]:
        dirs = {"work": self.data_dir / "workspaces" / spec.workspace}
        if spec.stack:
            dirs["state"] = self.data_dir / "tfstate" / spec.stack
            dirs["infra"] = self.data_dir / "demo-infra" / spec.stack
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    def start(self, spec: RunSpec) -> RunStatus:
        if spec.run_id in self.procs or (self._work(spec.workspace) / ".runner.pid").exists():
            return self.status(spec.run_id)
        profile = PROFILES[spec.kind]
        dirs = self._dirs(spec)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RACKLOOM_TIMEOUT": str(_timeout(spec))}
        for mount in profile.mounts:
            env[f"RACKLOOM_{mount.upper()}"] = str(dirs[mount])
        for key in ("TF_CLI_CONFIG_FILE", "TF_PLUGIN_CACHE_DIR"):
            if os.environ.get(f"RACKLOOM_PROCESS_{key}"):
                env[key] = os.environ[f"RACKLOOM_PROCESS_{key}"]
        if spec.stdin_secret is not None:
            env["RACKLOOM_STDIN_SECRET"] = "1"  # noqa: S105
        cpu = _timeout(spec)

        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
            os.setsid()

        (dirs["work"] / ".run.json").write_text(json.dumps({"run_id": spec.run_id, "job_id": spec.job_id}))
        log_file = (dirs["work"] / "runner.stderr").open("ab")
        proc = subprocess.Popen(
            [sys.executable, str(self.runner_script)],
            cwd=dirs["work"], env=env, stdout=log_file, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if spec.stdin_secret is not None else subprocess.DEVNULL,
            preexec_fn=limits,  # noqa: PLW1509 - mode développement uniquement
        )
        (dirs["work"] / ".runner.pid").write_text(str(proc.pid))
        if spec.stdin_secret is not None and proc.stdin is not None:
            proc.stdin.write(spec.stdin_secret.encode())
            proc.stdin.close()
        self.procs[spec.run_id] = proc
        return RunStatus(run_id=spec.run_id, job_id=spec.job_id, state="running")

    def _pid_alive(self, run_id: str) -> bool:
        pid_file = self._work(run_id) / ".runner.pid"
        if not pid_file.exists():
            return False
        pid = int(pid_file.read_text() or 0)
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return b"rackloom_runner" in cmdline and stat.split(") ", 1)[1][:1] != "Z"

    def status(self, run_id: str) -> RunStatus:
        work = self._work(run_id)
        job_id = None
        if (work / ".run.json").exists():
            job_id = json.loads((work / ".run.json").read_text()).get("job_id")
        proc = self.procs.get(run_id)
        if proc is not None:
            code = proc.poll()
            if code is None:
                return RunStatus(run_id=run_id, job_id=job_id, state="running")
            return RunStatus(run_id=run_id, job_id=job_id, state="exited", exit_code=code)
        if self._pid_alive(run_id):
            return RunStatus(run_id=run_id, job_id=job_id, state="running")
        exit_file = work / "out" / "exit_code"
        if exit_file.exists():
            code = int(exit_file.read_text() or 1)
            return RunStatus(run_id=run_id, job_id=job_id, state="exited", exit_code=code)
        if (work / ".runner.pid").exists():
            return RunStatus(run_id=run_id, job_id=job_id, state="exited", exit_code=None)
        return RunStatus(run_id=run_id, state="missing")

    def list(self, job_id: str) -> list[RunStatus]:
        result = []
        root = self.data_dir / "workspaces"
        if root.exists():
            for meta in sorted(root.glob("*/.run.json")):
                info = json.loads(meta.read_text())
                if info.get("job_id") == job_id:
                    result.append(self.status(info["run_id"]))
        return result

    def signal(self, run_id: str, sig: str) -> RunStatus:
        proc = self.procs.get(run_id)
        pid = proc.pid if proc is not None else None
        if pid is None and self._pid_alive(run_id):
            pid = int((self._work(run_id) / ".runner.pid").read_text())
        if pid is not None:
            # Au runner seul : il relaie à l'outil. Un second SIGINT ferait arrêter Terraform brutalement.
            try:
                os.kill(pid, SIGNALS[sig])
            except ProcessLookupError:
                pass
        return self.status(run_id)

    def remove(self, run_id: str) -> None:
        proc = self.procs.pop(run_id, None)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
