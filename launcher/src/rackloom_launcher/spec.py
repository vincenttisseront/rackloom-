from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

RunnerKind = Literal["terraform", "ansible", "probe"]

SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"
RUN_ID = r"^[a-z0-9-]{8,64}$"


class RunSpec(BaseModel):
    """Spécification d'un run envoyée par le worker. Champs bornés, aucun chemin libre."""

    run_id: str = Field(pattern=RUN_ID)
    kind: RunnerKind
    correlation_id: str = Field(pattern=RUN_ID)
    job_id: str = Field(pattern=RUN_ID)
    stack: str | None = Field(default=None, pattern=SLUG)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    zone: Literal["local"] = "local"


class RunStatus(BaseModel):
    run_id: str
    state: Literal["created", "running", "exited", "missing"]
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    oom_killed: bool = False


@dataclass(frozen=True)
class KindProfile:
    image_env: str
    network: str
    memory_mb: int
    cpus: float
    pids: int
    mounts: tuple[str, ...]
    extra_env: dict[str, str] = field(default_factory=dict)


# Profils d'exécution par type de job : réseau, limites et montages fixés ici, pas par l'appelant.
PROFILES: dict[str, KindProfile] = {
    "terraform": KindProfile(
        image_env="RACKLOOM_RUNNER_IMAGE_TERRAFORM",
        network="none",
        memory_mb=1024,
        cpus=1.0,
        pids=256,
        mounts=("work", "state", "infra"),
        extra_env={"TF_CLI_CONFIG_FILE": "/opt/rackloom/terraformrc"},
    ),
    "ansible": KindProfile(
        image_env="RACKLOOM_RUNNER_IMAGE_ANSIBLE",
        network="rn-lab",
        memory_mb=768,
        cpus=1.0,
        pids=256,
        mounts=("work",),
    ),
    "probe": KindProfile(
        image_env="RACKLOOM_RUNNER_IMAGE_ANSIBLE",
        network="rn-lab",
        memory_mb=128,
        cpus=0.5,
        pids=64,
        mounts=("work",),
    ),
}
