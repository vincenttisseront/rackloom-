"""F50 : préférences de projet approuvées et versionnées.

Publiées par le référent habilité. Elles simplifient la résolution suivante
(provenance `project_preference`) sans modifier un plan existant ni créer de droit.
Le chat peut suggérer une valeur mais ne mémorise jamais une préférence active.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, registry
from ..canonical import digest
from ..db import Conn, Jsonb
from ..errors import ApiError, forbidden
from ..identity.authz import Principal, require_project
from ..ids import uuid7

ALLOWED_KEYS = frozenset({"default_service_template", "default_size_tier", "naming"})
NAMING_TOKENS = frozenset({"project_code", "nn", "environment", "project"})


def current(conn: Conn, project_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM project_preference_versions WHERE project_id = %s ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def list_versions(conn: Conn, project_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM project_preference_versions WHERE project_id = %s ORDER BY version DESC",
        (project_id,),
    ).fetchall()


def _require_referent(conn: Conn, principal: Principal, project: dict[str, Any]) -> None:
    require_project(principal, project["id"])
    if principal.is_platform_admin:
        return
    if project.get("referent_user_id") is None or project["referent_user_id"] != principal.user_id:
        raise forbidden("seul le référent du projet (ou un administrateur) peut publier des préférences")


def validate_preferences(conn: Conn, project: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ApiError(422, "invalid_preferences", "objet de préférences attendu", field="/preferences")
    extra = set(raw) - ALLOWED_KEYS
    if extra:
        raise ApiError(
            422,
            "unknown_field",
            f"champ de préférence « {sorted(extra)[0]} » refusé",
            field=f"/preferences/{sorted(extra)[0]}",
        )
    out: dict[str, Any] = {}
    if "default_service_template" in raw:
        slug = raw["default_service_template"]
        if not isinstance(slug, str) or not slug:
            raise ApiError(
                422, "invalid_preferences", "slug de service attendu", field="/preferences/default_service_template"
            )
        granted = registry.granted_services(conn, project["id"])
        if slug not in granted:
            raise ApiError(
                422,
                "service_not_granted",
                "ce service n'est pas octroyé au projet ; la préférence est refusée",
                field="/preferences/default_service_template",
            )
        if registry.latest_published(conn, slug) is None:
            raise ApiError(
                422,
                "no_published_version",
                "aucune version publiée de ce service",
                field="/preferences/default_service_template",
            )
        out["default_service_template"] = slug
    if "default_size_tier" in raw:
        tier = raw["default_size_tier"]
        if not isinstance(tier, str) or not tier:
            raise ApiError(422, "invalid_preferences", "niveau attendu", field="/preferences/default_size_tier")
        # Vérifié à la résolution contre les tiers de l'environnement ; ici on refuse les clés techniques.
        if tier in ("vcpu", "memory", "disk", "network", "vlan"):
            raise ApiError(422, "locked_field", "valeur technique refusée", field="/preferences/default_size_tier")
        out["default_size_tier"] = tier
    if "naming" in raw:
        pattern = raw["naming"]
        if not isinstance(pattern, str) or "{nn}" not in pattern or "{project_code}" not in pattern:
            raise ApiError(
                422,
                "invalid_naming",
                "convention de nommage : doit contenir {project_code} et {nn}",
                field="/preferences/naming",
            )
        import re

        tokens = set(re.findall(r"\{([a-z_]+)\}", pattern))
        if not tokens <= NAMING_TOKENS:
            bad = sorted(tokens - NAMING_TOKENS)[0]
            raise ApiError(422, "invalid_naming", f"jeton de nommage non autorisé : {bad}", field="/preferences/naming")
        out["naming"] = pattern
    if not out:
        raise ApiError(422, "empty_preferences", "au moins une préférence est requise", field="/preferences")
    return out


def publish(
    conn: Conn,
    principal: Principal,
    project: dict[str, Any],
    *,
    preferences: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    _require_referent(conn, principal, project)
    reason = (reason or "").strip()
    if not reason or len(reason) > 500:
        raise ApiError(422, "invalid_reason", "motif de publication requis (500 caractères au plus)", field="/reason")
    prefs = validate_preferences(conn, project, preferences)
    current_row = current(conn, project["id"])
    version = (current_row["version"] + 1) if current_row else 1
    row = conn.execute(
        "INSERT INTO project_preference_versions "
        "(id, project_id, version, digest, preferences, reason, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            uuid7(),
            project["id"],
            version,
            digest(prefs),
            Jsonb(prefs),
            reason,
            principal.user_id,
        ),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="project.preference.publish",
        outcome="ok",
        project_id=project["id"],
        targets={"version": version, "digest": row["digest"]},
        policy_decision={"reason": reason, "preferences": prefs},
    )
    return row


def public_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "version": row["version"],
        "digest": row["digest"],
        "preferences": row["preferences"],
        "reason": row["reason"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
        "created_by": str(row["created_by"]) if row.get("created_by") else None,
    }


def refuse_chat_write() -> None:
    """Le chat ne peut pas mémoriser une préférence active."""
    raise ApiError(
        422,
        "preference_not_from_chat",
        "une préférence de projet ne peut pas être créée ou activée depuis une conversation",
        field="/preferences",
    )
