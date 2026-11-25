"""Suivi : jobs, événements et flux SSE (LISTEN/NOTIFY, reprise par Last-Event-ID)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import anyio
import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row

from .. import projects, workflow
from ..changes import service
from ..config import get_settings
from ..db import Conn, get_pool
from ..errors import not_found
from ..events import CHANNEL
from ..identity import service as identity
from .deps import csrf_guard, unit_of_work
from .views import event_view, job_view

router = APIRouter(tags=["suivi"], dependencies=[Depends(csrf_guard)])

EVENT_COLUMNS = "id, type, source, severity, occurred_at, job_id, correlation_id, data"


def _job(conn: Conn, principal, job_id: UUID) -> dict[str, Any]:
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if job is None or job["project_id"] is None or not principal.can_see_project(job["project_id"]):
        raise not_found("job")
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return job_view(u.conn, _job(u.conn, u.p, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        job = _job(u.conn, u.p, job_id)
        change = service.get_change(u.conn, u.p, job["change_id"])
        workflow.require_action(u.conn, u.p, "change", change, "cancel_job")
        return service.cancel_job(u.conn, u.p, job_id, u.request_id)


@router.get("/changes/{change_id}/jobs")
def change_jobs(change_id: UUID, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        rows = u.conn.execute("SELECT * FROM jobs WHERE change_id = %s ORDER BY created_at",
                              (change["id"],)).fetchall()
        return [job_view(u.conn, j) for j in rows]


@router.get("/changes/{change_id}/events")
def change_events(change_id: UUID, request: Request, after: int = 0) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        rows = u.conn.execute(f"SELECT {EVENT_COLUMNS} FROM events WHERE correlation_id = %s AND project_id = %s "
                              "AND id > %s ORDER BY id LIMIT 500", (change["id"], change["project_id"], after))
        return [event_view(r) for r in rows.fetchall()]


def _start(request: Request, after: int | None) -> int:
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        return int(header)
    return after or 0


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _stream(where: str, params: tuple, last_id: int, session_id: UUID) -> AsyncIterator[str]:
    settings = get_settings()
    keepalive, recheck = settings.sse_keepalive_seconds, settings.sse_session_recheck_seconds
    deadline = time.monotonic() + settings.sse_max_stream_seconds
    aconn = await psycopg.AsyncConnection.connect(settings.resolved_database_url(), autocommit=True,
                                                  row_factory=dict_row)
    try:
        await aconn.execute(f"LISTEN {CHANNEL}")
        yield "retry: 3000\n\n"
        last_beat = last_check = time.monotonic()
        while True:
            cur = await aconn.execute(f"SELECT {EVENT_COLUMNS} FROM events WHERE {where} AND id > %s "
                                      "ORDER BY id LIMIT 200", (*params, last_id))
            for row in await cur.fetchall():
                last_id = row["id"]
                yield _sse(event_view(row))
            now = time.monotonic()
            if now >= deadline:
                return
            if now - last_check >= recheck:
                last_check = now
                if not await anyio.to_thread.run_sync(_session_active, session_id):
                    yield "event: session.ended\ndata: {}\n\n"
                    return
            if now - last_beat >= keepalive:
                last_beat = now
                yield ": keepalive\n\n"
            async for _ in aconn.notifies(timeout=min(1.0, keepalive), stop_after=1):
                pass
    finally:
        await aconn.close()


def _session_active(session_id: UUID) -> bool:
    with get_pool().connection() as conn:
        try:
            return identity.session_is_active(conn, session_id)
        finally:
            conn.rollback()


def _response(stream: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/changes/{change_id}/events/stream")
def change_stream(change_id: UUID, request: Request, after: int | None = None) -> StreamingResponse:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        session_id = u.p.session_id
    return _response(_stream("correlation_id = %s AND project_id = %s", (change["id"], change["project_id"]),
                             _start(request, after), session_id))


@router.get("/projects/{project}/events/stream")
def project_stream(project: str, request: Request, after: int | None = None) -> StreamingResponse:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        session_id = u.p.session_id
    return _response(_stream("project_id = %s", (p["id"],), _start(request, after), session_id))
