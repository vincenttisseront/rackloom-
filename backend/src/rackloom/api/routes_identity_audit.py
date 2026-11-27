"""F41–F45 : SCIM, revue d'accès, rotation, copie d'audit, certificats TLS."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..audit import export as audit_export
from ..connectors import rotation, tls_registry
from ..errors import ApiError
from ..identity import access_review, scim
from ..identity.authz import require_admin
from .deps import bearer_token, csrf_guard, unit_of_work

router = APIRouter(tags=["identites-audit"], dependencies=[Depends(csrf_guard)])
scim_router = APIRouter(tags=["scim"])  # Bearer SCIM : pas de CSRF cookie


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


# --- F41 SCIM ---------------------------------------------------------------------------------


@router.post("/admin/scim/tokens")
def mint_scim_token(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"label"})
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return scim.mint_token(u.conn, u.p, label=str(body.get("label") or ""))


@router.post("/admin/scim/sync-fixture")
def sync_scim_fixture(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Synchronise depuis une liste fictive explicite (aucun IdP réel)."""
    _only(body, {"users"})
    users = body.get("users")
    if not isinstance(users, list):
        raise ApiError(422, "invalid_users", "liste users attendue", field="/users")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return scim.apply_fixture(u.conn, users)


def _scim_auth(request: Request, conn) -> None:
    token = bearer_token(request)
    if not token or scim.resolve_token(conn, token) is None:
        raise ApiError(401, "unauthenticated", "jeton SCIM absent ou révoqué")


@scim_router.get("/scim/v2/Users/{user_id}")
def scim_get_user(user_id: UUID, request: Request) -> JSONResponse:
    with unit_of_work(request, write=True, anonymous=True) as u:
        _scim_auth(request, u.conn)
        body = scim.get_user(u.conn, user_id)
        return JSONResponse(body, media_type=scim.SCIM_CONTENT)


@scim_router.post("/scim/v2/Users")
def scim_create_user(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    with unit_of_work(request, write=True, anonymous=True) as u:
        _scim_auth(request, u.conn)
        created = scim.create_user(u.conn, body)
        return JSONResponse(created, status_code=201, media_type=scim.SCIM_CONTENT)


@scim_router.patch("/scim/v2/Users/{user_id}")
def scim_patch_user(user_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    with unit_of_work(request, write=True, anonymous=True) as u:
        _scim_auth(request, u.conn)
        updated = scim.patch_user(u.conn, user_id, body)
        return JSONResponse(updated, media_type=scim.SCIM_CONTENT)


# --- F42 revue d'accès ------------------------------------------------------------------------


@router.get("/admin/access-reviews")
def list_access_reviews(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return access_review.list_campaigns(u.conn, u.p)


@router.post("/admin/access-reviews")
def prepare_access_review(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"title", "note"})
    with unit_of_work(request, write=True) as u:
        return access_review.prepare(u.conn, u.p, title=str(body.get("title") or ""), note=str(body.get("note") or ""))


@router.get("/admin/access-reviews/{campaign_id}")
def get_access_review(campaign_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return access_review.get_campaign(u.conn, u.p, campaign_id)


@router.get("/access-review-items/{item_id}")
def get_access_review_item(item_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return access_review.get_item(u.conn, u.p, item_id)


@router.post("/access-review-items/{item_id}/decide")
def decide_access_review_item(item_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"decision", "note"})
    with unit_of_work(request, write=True) as u:
        return access_review.decide(
            u.conn,
            u.p,
            item_id,
            decision=str(body.get("decision") or ""),
            note=str(body.get("note") or ""),
            request_id=u.request_id,
        )


# --- F43 rotation -----------------------------------------------------------------------------


@router.get("/connectors/{connector_id}/rotations")
def list_rotations(connector_id: UUID, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return rotation.list_for_connector(u.conn, u.p, connector_id)


@router.post("/connectors/{connector_id}/rotations")
def propose_rotation(connector_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"secret_name", "value"})
    with unit_of_work(request, write=True) as u:
        return rotation.propose(
            u.conn,
            u.p,
            connector_id,
            secret_name=str(body.get("secret_name") or ""),
            value=str(body.get("value") or ""),
            request_id=u.request_id,
        )


@router.post("/connector-rotations/{rotation_id}/validate")
def validate_rotation(rotation_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return rotation.validate(u.conn, u.p, rotation_id, request_id=u.request_id)


@router.post("/connector-rotations/{rotation_id}/cutover")
def cutover_rotation(rotation_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return rotation.cutover(u.conn, u.p, rotation_id, request_id=u.request_id)


@router.post("/connector-rotations/{rotation_id}/retire")
def retire_rotation(rotation_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return rotation.retire(u.conn, u.p, rotation_id, request_id=u.request_id)


# --- F44 copie d'audit ------------------------------------------------------------------------


@router.get("/admin/audit-exports")
def list_audit_exports(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return audit_export.list_destinations(u.conn, u.p)


@router.post("/admin/audit-exports")
def create_audit_export(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"slug", "relative_path", "object_lock"})
    with unit_of_work(request, write=True) as u:
        return audit_export.create_destination(
            u.conn,
            u.p,
            slug=str(body.get("slug") or ""),
            relative_path=str(body.get("relative_path") or body.get("slug") or ""),
            object_lock=bool(body.get("object_lock", True)),
        )


@router.post("/admin/audit-exports/{destination_id}/run")
def run_audit_export(destination_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return audit_export.run_export(u.conn, u.p, destination_id)


@router.post("/admin/audit-exports/{destination_id}/overwrite/{audit_id}")
def overwrite_audit_export(destination_id: UUID, audit_id: int, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return audit_export.try_overwrite(u.conn, u.p, destination_id, audit_id)


# --- F45 certificats TLS ----------------------------------------------------------------------


@router.get("/admin/tls-certificates")
def list_tls_certificates(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return tls_registry.list_records(u.conn, u.p)


@router.get("/admin/tls-certificates/alerts")
def list_tls_alerts(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return tls_registry.list_alerts(u.conn, u.p)


@router.post("/admin/approved-endpoints/{endpoint_id}/scan-tls")
def scan_tls_endpoint(endpoint_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _only(body or {}, {"owner_user_id", "alert_days"})
    with unit_of_work(request, write=True) as u:
        owner = None
        if body.get("owner_user_id"):
            try:
                owner = UUID(str(body["owner_user_id"]))
            except (TypeError, ValueError) as exc:
                raise ApiError(422, "invalid_identifier", "owner_user_id invalide") from exc
        return tls_registry.scan_endpoint(
            u.conn,
            u.p,
            endpoint_id,
            owner_user_id=owner,
            alert_days=int(body.get("alert_days") or 30),
            request_id=u.request_id,
        )
