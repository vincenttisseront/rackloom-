"""Raccourcis de parcours pour les tests (services appelés comme le ferait l'API)."""

from __future__ import annotations

from typing import Any

from rackloom import approvals
from rackloom.changes import service


def need(**kw: Any) -> dict[str, Any]:
    base = {"schema": "rackloom.need/v1", "template": "ubuntu-web", "usage": "test"}
    base.update(kw)
    return base


def create_and_submit(conn, demo, *, who: str = "alice", project: str = "vitrine", **need_kw: Any) -> dict[str, Any]:
    p = demo.principal(who)
    change = service.create_change(conn, p, demo.project(project), "Serveur web de test")
    res = service.add_version(conn, p, change["id"], {"need": need(**need_kw), "base_version_no": 0})
    sub = service.submit(conn, p, change["id"], res["version_no"])
    conn.commit()
    return {"id": change["id"], "version_no": res["version_no"], "job_id": sub["job_id"]}


def change(conn, change_id) -> dict[str, Any]:
    conn.commit()
    return conn.execute("SELECT * FROM changes WHERE id = %s", (change_id,)).fetchone()


def latest_plan(conn, change_id) -> dict[str, Any]:
    conn.commit()
    return conn.execute("SELECT * FROM plans WHERE change_id = %s ORDER BY created_at DESC LIMIT 1",
                        (change_id,)).fetchone()


def approve(conn, demo, change_id, plan, who: str = "bruno", digest: str | None = None) -> dict[str, Any]:
    res = approvals.decide(conn, demo.principal(who), change_id, {
        "plan_id": str(plan["id"]), "bundle_digest": digest or plan["bundle_digest"], "decision": "approved"})
    conn.commit()
    return res


def job(conn, job_id) -> dict[str, Any]:
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def jobs_of(conn, change_id, kind: str | None = None) -> list[dict[str, Any]]:
    conn.commit()
    q = "SELECT * FROM jobs WHERE change_id = %s"
    params: list[Any] = [change_id]
    if kind:
        q += " AND kind = %s"
        params.append(kind)
    return conn.execute(q + " ORDER BY created_at", params).fetchall()
