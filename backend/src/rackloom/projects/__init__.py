"""Projets, environnements et octrois de ressources, lus à travers le périmètre du principal."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from ..errors import not_found
from ..identity.authz import Principal, require_project


def list_projects(conn: psycopg.Connection, principal: Principal) -> list[dict[str, Any]]:
    visible = principal.visible_project_ids()
    if visible is None:
        rows = conn.execute("SELECT * FROM projects ORDER BY slug").fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects WHERE id = ANY(%s) ORDER BY slug",
                            (list(visible),)).fetchall()
    return [dict(r, roles=principal.roles_for(r["id"])) for r in rows]


def get_project(conn: psycopg.Connection, principal: Principal, project_ref: str) -> dict[str, Any]:
    """Accepte un identifiant ou un slug. Hors périmètre : 404, comme une ressource absente."""
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = %s", (UUID(project_ref),)).fetchone()
    except ValueError:
        row = conn.execute("SELECT * FROM projects WHERE slug = %s", (project_ref,)).fetchone()
    if row is None:
        raise not_found("projet")
    require_project(principal, row["id"])
    return row


def environments(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM environments WHERE project_id = %s ORDER BY kind = 'prod', slug", (project_id,)
    ).fetchall()


def get_environment(conn: psycopg.Connection, environment_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM environments WHERE id = %s", (environment_id,)).fetchone()


def members(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT DISTINCT u.id, u.email, u.display_name FROM role_bindings rb JOIN users u ON u.id = rb.user_id
        WHERE rb.project_id = %s AND rb.revoked_at IS NULL AND u.status = 'active' ORDER BY u.email
        """,
        (project_id,),
    ).fetchall()
