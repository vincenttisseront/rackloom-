"""Raccourcis de parcours pour les tests (services appelés comme le ferait l'API)."""


from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from rackloom import approvals
from rackloom.changes import service


def until(days: int = 14) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def need(*, template: str = "ubuntu-web", environment: str | None = None, confirm_production: bool = False,
         **inputs: Any) -> dict[str, Any]:
    """Besoin catégorie A uniquement. Par défaut : date de fin dans 14 jours (D14)."""
    inputs.setdefault("lifetime_until", until())
    return {"schema": "rackloom.need/v1", "service": {"template": template}, "environment": environment,
            "confirm_production": confirm_production, "inputs": {k: v for k, v in inputs.items() if v is not None}}


def publish_hardened(conn, demo) -> dict[str, Any]:
    """Inès conçoit « ubuntu-web-durci » avec l'assistant fictif ; Paul le publie. Aucun job n'est créé."""
    from rackloom import registry, studio

    before = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
    ines = demo.principal("ines")
    result = studio.create(
        conn,
        ines,
        {
            "slug": "ubuntu-web-durci",
            "base": {"slug": "ubuntu-web", "version": "1.0.0"},
            "brief": "Je veux un serveur web Ubuntu durci, avec Fail2ban sur SSH, déployé une machine d'abord",
        },
    )
    assert result["turn"]["status"] == "ok", result["turn"]
    draft = result["draft"]
    for _ in range(12):
        pending = [s for s in draft["proposals"] if s["status"] == "pending"]
        if not pending:
            break
        draft = studio.decide_suggestion(
            conn, ines, draft["id"], pending[0]["id"], {"row_version": draft["row_version"], "decision": "accepted"}
        )
    assert not studio.problems(conn, draft), studio.problems(conn, draft)
    submitted = studio.submit(
        conn,
        ines,
        draft["id"],
        {"row_version": draft["row_version"], "digest": studio.content_digest(draft["content"])},
    )
    registry.publish_action(
        conn,
        demo.principal("pub"),
        submitted["slug"],
        submitted["version"],
        "publish",
        "Revue de la première tranche de l'atelier",
        submitted["digest"],
    )
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == before
    return submitted


def create_and_submit(conn, demo, *, who: str = "alice", project: str = "vitrine", **need_kw: Any) -> dict[str, Any]:
    p = demo.principal(who)
    change = service.create_change(conn, p, demo.project(project), "Serveur web de test")
    res = service.add_version(conn, p, change["id"], {"need": need(**need_kw), "base_version_no": 0})
    assert res["revision"]["outcome"] == "resolved", res["revision"]
    sub = service.submit(conn, p, change["id"], res["revision_no"])
    conn.commit()
    return {"id": change["id"], "version_no": res["version_no"], "revision_no": res["revision_no"],
            "job_id": sub["job_id"], "deployment_id": sub["deployment_id"]}


def change(conn, change_id) -> dict[str, Any]:
    conn.commit()
    return conn.execute("SELECT * FROM changes WHERE id = %s", (change_id,)).fetchone()


def deployment(conn, deployment_id) -> dict[str, Any]:
    conn.commit()
    return conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()


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
