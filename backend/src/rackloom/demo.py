"""Chargement du profil de démonstration (identités fictives, inventaire simulé, octrois).

Refusé en profil production. Les mots de passe sont générés à chaque chargement et écrits
dans `.lab/demo-credentials.txt` (ignoré par git, mode 0600), jamais dans le dépôt.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import psycopg
import yaml

from . import audit, catalog, inventory, policy
from .config import get_settings
from .connectors import capabilities_for
from .db import Jsonb
from .identity.service import create_user
from .ids import uuid7


class DemoRefused(Exception):
    pass


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def seed(conn: psycopg.Connection, root: Path, *, passwords: dict[str, str] | None = None,
         credentials_file: Path | None = None) -> dict[str, Any]:
    if get_settings().profile == "production":
        raise DemoRefused("profil de démonstration refusé en profil production")
    profile = _read(root / "lab" / "fixtures" / "demo-profile.yaml")
    fixture = _read(root / "lab" / "fixtures" / "inventory.yaml")
    policy.ensure_builtin(conn)
    catalog.load(conn, root / "catalog")

    connectors: dict[str, Any] = {}
    for c in profile["connectors"]:
        row = conn.execute("SELECT * FROM connectors WHERE slug = %s", (c["slug"],)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO connectors (id, slug, kind, mode, capabilities) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (uuid7(), c["slug"], c["kind"], c["mode"], Jsonb(capabilities_for(c["kind"], c["mode"]))),
            ).fetchone()
        connectors[c["slug"]] = row

    projects: dict[str, Any] = {}
    envs: dict[tuple[str, str], Any] = {}
    for p in profile["projects"]:
        row = conn.execute("SELECT * FROM projects WHERE slug = %s", (p["slug"],)).fetchone() or conn.execute(
            "INSERT INTO projects (id, slug, name) VALUES (%s, %s, %s) RETURNING *",
            (uuid7(), p["slug"], p["name"])).fetchone()
        projects[p["slug"]] = row
        for e in p["environments"]:
            env = conn.execute("SELECT * FROM environments WHERE project_id = %s AND slug = %s",
                               (row["id"], e["slug"])).fetchone() or conn.execute(
                "INSERT INTO environments (id, project_id, slug, name, kind, requires_distinct_approver, "
                "plan_ttl_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid7(), row["id"], e["slug"], e["name"], e["kind"], e["requires_distinct_approver"],
                 e["plan_ttl_seconds"])).fetchone()
            envs[(p["slug"], e["slug"])] = env
        for slug in p.get("templates", []):
            conn.execute("INSERT INTO project_templates (project_id, template_slug) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (row["id"], slug))

    generated: dict[str, str] = {}
    users: dict[str, Any] = {}
    for u in profile["users"]:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (u["email"],)).fetchone()
        if row is None:
            password = (passwords or {}).get(u["email"]) or secrets.token_urlsafe(12)
            user_id = create_user(conn, u["email"], u["name"], password, is_demo=True)
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            generated[u["email"]] = password
        users[u["email"]] = row
        for binding in u["roles"]:
            project_id = projects[binding["project"]]["id"] if binding.get("project") else None
            exists = conn.execute(
                "SELECT 1 FROM role_bindings WHERE user_id = %s AND role = %s AND project_id IS NOT DISTINCT FROM %s "
                "AND revoked_at IS NULL", (row["id"], binding["role"], project_id)).fetchone()
            if not exists:
                conn.execute("INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, %s, %s)",
                             (uuid7(), row["id"], binding["role"], project_id))

    fake = connectors["vsphere-fake"]
    for g in profile["grants"]:
        conn.execute(
            "INSERT INTO resource_grants (id, project_id, environment_id, connector_id, kind, stable_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (uuid7(), projects[g["project"]]["id"], envs[(g["project"], g["environment"])]["id"], fake["id"],
             g["kind"], g["stable_id"]))
    snapshot = inventory.record_snapshot(conn, fake["id"], fixture["items"])
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('lab_nodes', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_at = now()", (Jsonb(fixture.get("lab_nodes", [])),))
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('assistant', %s) ON CONFLICT (key) DO NOTHING",
        (Jsonb({"adapter": "fake", "scenario": "nominal"}),))
    audit.record(conn, actor_type="system", actor_id="demo-seed", action="demo.seed", outcome="ok",
                 targets={"projects": sorted(projects), "users": len(users)},
                 versions={"inventory_snapshot": str(snapshot)})

    if generated and credentials_file is not None:
        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# Identités de démonstration (fictives). Fichier local, ignoré par git.\n")
            for email, password in generated.items():
                fh.write(f"{email} {password}\n")
    return {"projects": {k: v["id"] for k, v in projects.items()},
            "environments": {f"{k[0]}/{k[1]}": v["id"] for k, v in envs.items()},
            "users": {k: v["id"] for k, v in users.items()}, "snapshot_id": snapshot,
            "generated_passwords": sorted(generated)}
