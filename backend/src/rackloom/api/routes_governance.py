"""Lot C : politiques (F10), droits temporaires (F11), admin déléguée (F13), urgence (F14)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from ..errors import ApiError, forbidden, not_found
from ..identity import emergency, oidc, reauth, temporary
from ..identity.authz import require_admin
from ..policy import exceptions as policy_exc
from ..policy import simulate as policy_sim
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["gouvernance"], dependencies=[Depends(csrf_guard)])


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


def _parse_expiry(raw: Any) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid_expiry", "expires_at ISO-8601 attendu", field="/expires_at") from exc
    return value


# --- F10 : exceptions et simulation -----------------------------------------------------------


@router.get("/projects/{project_id}/policy-exceptions")
def list_policy_exceptions(project_id: UUID, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        if not (u.p.is_platform_admin or u.p.is_global_auditor or u.p.can_see_project(project_id)):
            raise not_found("projet")
        if not (
            u.p.is_platform_admin
            or u.p.has_role("project_admin", project_id)
            or u.p.has_role("auditor", project_id)
            or u.p.is_global_auditor
        ):
            raise forbidden("rôle project_admin ou auditeur requis")
        return policy_exc.list_for_project(u.conn, project_id, include_expired=True)


@router.post("/projects/{project_id}/policy-exceptions")
def create_policy_exception(project_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"rule_id", "environment_id", "owner_user_id", "reason", "expires_at"})
    with unit_of_work(request, write=True) as u:
        reauth.require_fresh(u.p, action="créer une exception de politique")
        try:
            owner = UUID(str(body.get("owner_user_id")))
            env_id = UUID(body["environment_id"]) if body.get("environment_id") else None
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_identifier", "identifiant invalide") from exc
        return policy_exc.create(
            u.conn,
            u.p,
            rule_id=str(body.get("rule_id") or ""),
            project_id=project_id,
            environment_id=env_id,
            owner_user_id=owner,
            reason=body.get("reason"),
            expires_at=_parse_expiry(body.get("expires_at")),
            request_id=u.request_id,
        )


@router.post("/policy-exceptions/{exception_id}/revoke")
def revoke_policy_exception(exception_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        reauth.require_fresh(u.p, action="révoquer une exception de politique")
        return policy_exc.revoke(u.conn, u.p, exception_id, body.get("reason"), request_id=u.request_id)


@router.post("/admin/policies/simulate")
def simulate_policy(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Simulation sur une demande historique : aucune écriture, plans inchangés (F10)."""
    _only(body, {"change_id", "candidate_organization", "job_kind"})
    with unit_of_work(request) as u:
        try:
            change_id = UUID(str(body.get("change_id")))
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_change", "change_id attendu", field="/change_id") from exc
        return policy_sim.simulate(
            u.conn,
            u.p,
            change_id,
            candidate_organization=body.get("candidate_organization"),
            job_kind=str(body.get("job_kind") or "tf_apply"),
        )


@router.get("/admin/policies/active")
def active_policy(request: Request) -> dict[str, Any]:
    from .. import policy

    with unit_of_work(request) as u:
        if not (
            u.p.is_platform_admin
            or u.p.is_global_auditor
            or any(b.role in ("project_admin", "auditor") for b in u.p.bindings)
        ):
            raise forbidden("rôle administrateur ou auditeur requis")
        active = policy.active()
        return {
            "version": active["version"],
            "digest": active["digest"],
            "rules": policy.BUILTIN_RULES["admission"],
            "organization": policy.BUILTIN_RULES["organization"],
            "platform": policy.BUILTIN_RULES["platform"],
        }


# --- F11 : droits temporaires -----------------------------------------------------------------


@router.get("/projects/{project_id}/temporary-grants")
def list_temporary_grants(project_id: UUID, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        if not (u.p.is_platform_admin or u.p.is_global_auditor or u.p.can_see_project(project_id)):
            raise not_found("projet")
        if not (
            u.p.is_platform_admin
            or u.p.has_role("project_admin", project_id)
            or u.p.has_role("auditor", project_id)
            or u.p.is_global_auditor
        ):
            raise forbidden("rôle project_admin ou auditeur requis")
        return temporary.list_for_project(u.conn, project_id, include_inactive=True)


@router.post("/projects/{project_id}/temporary-grants")
def create_temporary_grant(project_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"user_id", "role", "environment_id", "operation", "reason", "expires_at"})
    with unit_of_work(request, write=True) as u:
        reauth.require_fresh(u.p, action="accorder un droit temporaire")
        try:
            user_id = UUID(str(body.get("user_id")))
            env_id = UUID(body["environment_id"]) if body.get("environment_id") else None
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_identifier", "identifiant invalide") from exc
        return temporary.grant(
            u.conn,
            u.p,
            user_id=user_id,
            role=str(body.get("role") or ""),
            project_id=project_id,
            environment_id=env_id,
            operation=str(body.get("operation") or "any"),
            reason=body.get("reason"),
            expires_at=_parse_expiry(body.get("expires_at")),
            request_id=u.request_id,
        )


@router.post("/temporary-grants/{grant_id}/revoke")
def revoke_temporary_grant(grant_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        reauth.require_fresh(u.p, action="révoquer un droit temporaire")
        return temporary.revoke(u.conn, u.p, grant_id, body.get("reason"), request_id=u.request_id)


# --- F14 : urgence (lecture API ; activation console) -----------------------------------------


@router.get("/admin/emergency")
def emergency_status(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        require_admin(u.p)
        return emergency.status(u.conn)


@router.post("/admin/emergency/close")
def emergency_close_api(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Fermeture d'intervention depuis l'UI admin (activation reste console-only)."""
    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        reauth.require_fresh(u.p, action="fermer une intervention d'urgence")
        return emergency.close(
            u.conn,
            reason=body.get("reason"),
            actor=f"user:{u.p.email}",
            restore_sso=True,
        )


# --- Lot 4 : OIDC admin ---------------------------------------------------------------------


@router.get("/admin/oidc")
def oidc_admin_get(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        require_admin(u.p)
        view = oidc.admin_view(u.conn)
        view["idp_reachable"] = oidc.probe_idp(u.conn).get("reachable")
        return view


@router.put("/admin/oidc")
def oidc_admin_put(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(
        body,
        {
            "enabled",
            "issuer_url",
            "client_id",
            "client_secret",
            "clear_client_secret",
            "scopes",
            "email_claim",
            "groups_claim",
            "name_claim",
            "step_up_acr",
        },
    )
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        reauth.require_fresh(u.p, action="configurer le SSO OIDC")
        return oidc.configure(
            u.conn,
            u.p,
            enabled=body.get("enabled"),
            issuer_url=body.get("issuer_url"),
            client_id=body.get("client_id"),
            client_secret=body.get("client_secret"),
            clear_client_secret=bool(body.get("clear_client_secret")),
            scopes=body.get("scopes"),
            email_claim=body.get("email_claim"),
            groups_claim=body.get("groups_claim"),
            name_claim=body.get("name_claim"),
            step_up_acr=body.get("step_up_acr"),
        )


@router.post("/admin/oidc/probe")
def oidc_admin_probe(request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return oidc.probe_idp(u.conn)


@router.post("/admin/oidc/mappings")
def oidc_mapping_create(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"idp_group", "role", "project_id", "environment_id"})
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        reauth.require_fresh(u.p, action="créer un mapping de groupe OIDC")
        project_id = UUID(body["project_id"]) if body.get("project_id") else None
        environment_id = UUID(body["environment_id"]) if body.get("environment_id") else None
        return oidc.add_group_mapping(
            u.conn,
            u.p,
            idp_group=str(body.get("idp_group") or ""),
            role=str(body.get("role") or ""),
            project_id=project_id,
            environment_id=environment_id,
        )


@router.delete("/admin/oidc/mappings/{mapping_id}")
def oidc_mapping_delete(mapping_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        reauth.require_fresh(u.p, action="supprimer un mapping de groupe OIDC")
        return oidc.delete_group_mapping(u.conn, u.p, mapping_id)
