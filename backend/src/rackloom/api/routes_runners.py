"""F16 : administration des zones/runners et API pull initiée par le runner (mTLS)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import runners
from ..config import get_settings
from ..errors import ApiError
from ..executions import queue
from ..identity.authz import require_admin
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["runners"], dependencies=[Depends(csrf_guard)])

# Routeur runner : authentifié par empreinte mTLS, pas de CSRF cookie.
runner_router = APIRouter(tags=["runners-mtls"])


class EnrollBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=120)
    zone: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    capabilities: list[str] = Field(min_length=1, max_length=32)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str | None = Field(default=None, max_length=120)


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(default=60, ge=10, le=600)
    kinds: list[str] | None = None


def _fingerprint(request: Request) -> str | None:
    return request.headers.get(runners.CERT_HEADER) or request.headers.get("X-Client-Cert-SHA256")


@router.get("/admin/execution-zones")
def list_zones(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return runners.list_zones(u.conn, u.p)


@router.get("/admin/runners")
def list_runners(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return runners.list_runners(u.conn, u.p)


@router.post("/admin/runners/enroll")
def enroll_runner(request: Request, body: EnrollBody) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return runners.enroll(
            u.conn,
            u.p,
            slug=body.slug,
            name=body.name,
            zone_slug=body.zone,
            capabilities=body.capabilities,
            keys_dir=get_settings().keys_dir,
            request_id=u.request_id,
        )


@router.post("/admin/runners/{runner_id}/revoke")
def revoke_runner(runner_id: UUID, request: Request, body: RevokeBody) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return runners.revoke(u.conn, u.p, runner_id, reason=body.reason, request_id=u.request_id)


@runner_router.post("/runners/me/heartbeat")
def runner_heartbeat(request: Request, body: HeartbeatBody = Body(default=HeartbeatBody())) -> dict[str, Any]:
    """Le runner initie le heartbeat (connexion sortante, mTLS)."""
    with unit_of_work(request, write=True, anonymous=True) as u:
        row = runners.authenticate_runner(u.conn, _fingerprint(request))
        return runners.heartbeat(u.conn, row, worker_id=body.worker_id)


@runner_router.post("/runners/me/claim")
def runner_claim(request: Request, body: ClaimBody = Body(default=ClaimBody())) -> dict[str, Any]:
    """Le runner tire un job compatible avec sa zone et ses capacités."""
    with unit_of_work(request, write=True, anonymous=True) as u:
        row = runners.authenticate_runner(u.conn, _fingerprint(request))
        view = runners.heartbeat(u.conn, row)
        worker_id = view.get("worker_id") or f"runner-{row['slug']}"
        job = queue.claim(
            u.conn,
            worker_id,
            body.lease_seconds,
            body.kinds,
            zone=row["zone_slug"],
            capabilities=list(row["capabilities"]),
        )
        if job is None:
            return {"job": None, "runner": view}
        # Contrôle redondant (défense en profondeur) avant de rendre le job.
        env_kind = None
        if job["change_id"]:
            env = u.conn.execute(
                "SELECT e.kind FROM changes c JOIN environments e ON e.id = c.environment_id WHERE c.id = %s",
                (job["change_id"],),
            ).fetchone()
            env_kind = env["kind"] if env else None
        ok, code = runners.runner_may_take_job(
            {**row, "zone_slug": row["zone_slug"], "zone_kinds": row["zone_kinds"]},
            job,
            env_kind,
        )
        if not ok:
            # Remet en file : ne jamais laisser un job de mauvaise zone loué.
            queue.retry_later(u.conn, job, 0)
            raise ApiError(403, code, f"job refusé pour ce runner ({code})")
        return {
            "job": {
                "id": str(job["id"]),
                "kind": job["kind"],
                "class": job["class"],
                "required_zone": job.get("required_zone"),
                "lease_epoch": job["lease_epoch"],
                "change_id": str(job["change_id"]) if job["change_id"] else None,
                "project_id": str(job["project_id"]) if job["project_id"] else None,
                "payload": job["payload"],
            },
            "runner": view,
        }
