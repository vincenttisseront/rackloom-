"""Chargement du profil de démonstration (identités fictives, inventaire simulé, octrois).

Refusé en profil production. Les mots de passe sont générés à chaque chargement et écrits
dans `.lab/demo-credentials.txt` (ignoré par git, mode 0600), jamais dans le dépôt.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from . import audit, inventory, policy, projects, registry
from .config import get_settings
from .connectors import capabilities_for
from .db import Conn, Jsonb
from .identity.service import create_user
from .ids import uuid7


class DemoRefused(Exception):
    pass


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def seed(conn: Conn, root: Path, *, passwords: dict[str, str] | None = None,
         credentials_file: Path | None = None) -> dict[str, Any]:
    if get_settings().profile == "production":
        raise DemoRefused("profil de démonstration refusé en profil production")
    profile = _read(root / "lab" / "fixtures" / "demo-profile.yaml")
    fixture = _read(root / "lab" / "fixtures" / "inventory.yaml")
    policy.ensure_builtin(conn)
    registry.load(conn, root / "catalog")
    from .chat import runbooks as runbook_mod

    runbook_mod.load_catalog(conn, root / "catalog")

    connectors: dict[str, Any] = {}
    for c in profile["connectors"]:
        row = conn.execute("SELECT * FROM connectors WHERE slug = %s", (c["slug"],)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO connectors (id, slug, kind, mode, capabilities) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (uuid7(), c["slug"], c["kind"], c["mode"], Jsonb(capabilities_for(c["kind"], c["mode"]))),
            ).fetchone()
        connectors[c["slug"]] = row

    generated: dict[str, str] = {}
    users: dict[str, Any] = {}
    for u in profile["users"]:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (u["email"],)).fetchone()
        if row is None:
            password = (passwords or {}).get(u["email"]) or secrets.token_urlsafe(12)
            user_id = create_user(conn, u["email"], u["name"], password, is_demo=True)
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            generated[u["email"]] = password
        users[u["key"]] = row

    projs: dict[str, Any] = {}
    envs: dict[str, Any] = {}
    for p in profile["projects"]:
        row = conn.execute("SELECT * FROM projects WHERE slug = %s", (p["slug"],)).fetchone() or conn.execute(
            "INSERT INTO projects (id, slug, code, name, referent_user_id) VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (uuid7(), p["slug"], p["code"], p["name"], users[p["referent"]]["id"])).fetchone()
        projs[p["slug"]] = row
        for e in p["environments"]:
            env = conn.execute("SELECT * FROM environments WHERE project_id = %s AND slug = %s",
                               (row["id"], e["slug"])).fetchone()
            if env is None:
                env = conn.execute(
                    "INSERT INTO environments (id, project_id, slug, name, kind, is_production, "
                    "requires_distinct_approver, plan_ttl_seconds) VALUES (%s, %s, %s, %s, %s, %s, true, %s) "
                    "RETURNING *",
                    (uuid7(), row["id"], e["slug"], e["name"], e["kind"], e["kind"] == "prod",
                     e["plan_ttl_seconds"])).fetchone()
                projects.add_policy_version(conn, env["id"], profile["policies"][e["policy"]], None,
                                            "politique initiale du profil de démonstration")
            envs[f"{p['slug']}/{e['slug']}"] = env

    for u in profile["users"]:
        row = users[u["key"]]
        for binding in u["roles"]:
            project_id = projs[binding["project"]]["id"] if binding.get("project") else None
            exists = conn.execute(
                "SELECT 1 FROM role_bindings WHERE user_id = %s AND role = %s AND project_id IS NOT DISTINCT FROM %s "
                "AND revoked_at IS NULL", (row["id"], binding["role"], project_id)).fetchone()
            if not exists:
                conn.execute("INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, %s, %s)",
                             (uuid7(), row["id"], binding["role"], project_id))

    for target, kinds in profile["grants"].items():
        env = envs[target]
        for kind, ids in kinds.items():
            for stable_id in ids:
                conn.execute(
                    "INSERT INTO resource_grants (id, project_id, environment_id, kind, stable_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (uuid7(), env["project_id"], env["id"], kind, stable_id))

    for pub in profile.get("publish", []):
        row = registry.template(conn, pub["template"], pub["version"])
        if row and row["status"] == "validated":
            registry.change_status(conn, None, pub["template"], pub["version"], "publish", pub["reason"])

    snapshot = inventory.record_snapshot(conn, connectors["vsphere-fake"]["id"], fixture["items"])
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('lab_nodes', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_at = now()", (Jsonb(fixture.get("lab_nodes", [])),))
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('assistant', %s) ON CONFLICT (key) DO NOTHING",
        (Jsonb({"adapter": "fake", "scenario": "nominal"}),))
    # F35 : identité de service limitée au projet vitrine (jeton Bearer, jamais de mot de passe).
    from .identity import service_accounts

    service_tokens: dict[str, str] = {}
    if "vitrine" in projs:
        existing_sa = conn.execute(
            "SELECT id FROM users WHERE email = %s", ("ci-vitrine@demo.invalid",)
        ).fetchone()
        if existing_sa is None:
            minted = service_accounts.mint(
                conn,
                email="ci-vitrine@demo.invalid",
                display_name="CI Vitrine (démo)",
                project_id=projs["vitrine"]["id"],
                label="pipeline-demo",
                is_demo=True,
                actor_id=users["admin"]["id"],
            )
            service_tokens["ci-vitrine@demo.invalid"] = minted["token"]
            users["ci_vitrine"] = conn.execute(
                "SELECT * FROM users WHERE id = %s", (minted["user_id"],)
            ).fetchone()

    from . import access, compatibility

    compatibility.ensure_seeded(conn)
    # F59 : correspondances niveau d'accès → groupe IdP (faux IdP local).
    access.ensure_demo_mappings(conn, published_by=users["admin"]["id"])

    audit.record(conn, actor_type="system", actor_id="demo-seed", action="demo.seed", outcome="ok",
                 targets={"projects": sorted(projs), "users": len(users)},
                 versions={"inventory_snapshot": str(snapshot)})

    if (generated or service_tokens) and credentials_file is not None:
        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# Identités de démonstration (fictives). Fichier local, ignoré par git.\n")
            for email, password in generated.items():
                fh.write(f"{email} {password}\n")
            for email, token in service_tokens.items():
                fh.write(f"# service token (F35)\n{email} bearer:{token}\n")
    return {"projects": {k: v["id"] for k, v in projs.items()},
            "environments": {k: v["id"] for k, v in envs.items()},
            "users": {k: v["id"] for k, v in users.items()}, "snapshot_id": snapshot,
            "generated_passwords": sorted(generated),
            "service_tokens": service_tokens}
