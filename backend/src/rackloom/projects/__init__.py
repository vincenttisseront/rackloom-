"""Projets, environnements et octrois de ressources, lus à travers le périmètre du principal."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from .. import audit
from ..canonical import digest
from ..db import Jsonb
from ..errors import not_found
from ..identity.authz import Principal, require_project
from ..ids import uuid7


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


def current_policy(conn: psycopg.Connection, environment_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM environment_policies WHERE environment_id = %s ORDER BY version DESC LIMIT 1",
        (environment_id,),
    ).fetchone()


def policy_version(conn: psycopg.Connection, environment_id: UUID, version: int) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM environment_policies WHERE environment_id = %s AND version = %s",
                        (environment_id, version)).fetchone()


def add_policy_version(conn: psycopg.Connection, environment_id: UUID, rules: dict[str, Any], actor_id: UUID | None,
                       reason: str) -> dict[str, Any]:
    """Une politique modifiée crée une nouvelle version ; les anciennes restent lisibles."""
    current = current_policy(conn, environment_id)
    version = (current["version"] + 1) if current else 1
    row = conn.execute(
        "INSERT INTO environment_policies (id, environment_id, version, digest, rules, created_by, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), environment_id, version, digest(rules), Jsonb(rules), actor_id, reason),
    ).fetchone()
    audit.record(conn, actor_type="user" if actor_id else "system", actor_id=str(actor_id) if actor_id else "seed",
                 action="policy.environment.version", outcome="ok", targets={"environment_id": str(environment_id)},
                 versions={"policy": version, "digest": row["digest"]}, policy_decision={"reason": reason})
    return row


def set_grants_revoked(conn: psycopg.Connection, project_slug: str, environment_slug: str, kind: str,
                       revoked: bool, reason: str) -> list[str]:
    """Retire ou rétablit les octrois d'un type pour un environnement (exploitation, CLI de l'appliance)."""
    rows = conn.execute(
        """
        UPDATE resource_grants g SET revoked_at = CASE WHEN %(revoked)s THEN now() END,
               granted_at = CASE WHEN %(revoked)s THEN g.granted_at ELSE now() END
        FROM environments e JOIN projects p ON p.id = e.project_id
        WHERE g.environment_id = e.id AND p.slug = %(project)s AND e.slug = %(env)s AND g.kind = %(kind)s
          AND (g.revoked_at IS NULL) = %(revoked)s
        RETURNING g.stable_id, g.project_id, g.environment_id
        """,
        {"revoked": revoked, "project": project_slug, "env": environment_slug, "kind": kind},
    ).fetchall()
    if rows:
        audit.record(conn, actor_type="system", actor_id="cli", action="grant.revoke" if revoked else "grant.restore",
                     outcome="ok", project_id=rows[0]["project_id"],
                     targets={"environment_id": str(rows[0]["environment_id"]), "kind": kind,
                              "stable_ids": sorted(r["stable_id"] for r in rows)},
                     policy_decision={"reason": reason})
    return sorted(r["stable_id"] for r in rows)


def referent_name(conn: psycopg.Connection, project: dict[str, Any]) -> str:
    if project.get("referent_user_id") is None:
        return "désigné du projet"
    row = conn.execute("SELECT display_name FROM users WHERE id = %s", (project["referent_user_id"],)).fetchone()
    return row["display_name"].split(" (")[0] if row else "désigné du projet"
