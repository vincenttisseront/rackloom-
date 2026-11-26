"""Administration : registre et publication, politiques, connecteurs, utilisateurs et rôles, audit, état.

Publier n'est ni déployer ni approuver : aucune de ces routes ne crée de job ni d'approbation.
"""


from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from pydantic import ValidationError

from .. import audit, inventory, projects, registry, secrets, studio
from ..config import get_settings
from ..connectors import MATRIX, circuit, vsphere
from ..db import Conn, Jsonb, get_pool
from ..errors import ApiError, forbidden, not_found
from ..events import emit
from ..identity import service as identity
from ..identity.authz import ROLES, Principal, require_admin, require_auditor
from ..ids import uuid7
from .deps import csrf_guard, unit_of_work
from .views import iso

router = APIRouter(tags=["administration"], dependencies=[Depends(csrf_guard)])


SECRET_HINTS = ("password", "secret", "token", "passphrase", "private_key", "api_key")


ASSIGNABLE_ROLES = tuple(r for r in ROLES)


def _registry_reader(p: Principal) -> None:
    if not (
        p.is_platform_admin or p.has_role("template_publisher") or p.has_role("template_author") or p.is_global_auditor
    ):
        raise forbidden("rôle administrateur, auteur ou éditeur de templates, ou auditeur requis")


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


@router.get("/admin/components")
def list_components(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        _registry_reader(u.p)
        rows = u.conn.execute("SELECT slug, version, status, digest, contract, verification_level "
                              "FROM component_versions ORDER BY slug, loaded_at").fetchall()
        return [{"slug": r["slug"], "version": r["version"], "status": r["status"], "digest": r["digest"],
                 "verification_level": r["verification_level"], "title": r["contract"]["title"],
                 "effects": r["contract"].get("effects"), "checks": r["contract"].get("checks"),
                 "parameters": r["contract"].get("parameters"),
                 "implementation": r["contract"].get("implementation")} for r in rows]


@router.get("/admin/templates")
def list_templates(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        _registry_reader(u.p)
        rows = u.conn.execute("SELECT slug, version, status, digest, contract, published_at FROM template_versions "
                              "ORDER BY slug, loaded_at").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = out.setdefault(r["slug"], {"slug": r["slug"], "title": r["contract"]["title"], "versions": []})
            entry["versions"].append({"version": r["version"], "status": r["status"], "digest": r["digest"],
                                      "published_at": iso(r["published_at"])})
        for slug, entry in out.items():
            latest = registry.latest_published(u.conn, slug)
            entry["latest_published"] = latest["version"] if latest else None
        return list(out.values())


@router.get("/admin/templates/{slug}/versions/{version}")
def get_template(slug: str, version: str, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        _registry_reader(u.p)
        row = registry.template(u.conn, slug, version)
        if row is None:
            raise not_found("version de template")
        comps = registry.template_components(u.conn, row["contract"])
        own = row["submitted_by"] is not None and row["submitted_by"] == u.p.user_id
        actions = [
            a
            for a, (states, _) in registry.TRANSITIONS.items()
            if row["status"] in states
            and u.p.has_role("template_publisher")
            and not (own and row["status"] == "in_review")
        ]
        if row["status"] != "in_review":
            actions = [a for a in actions if a != "return"]
        promotions = registry.list_promotions(u.conn, row["id"])
        promoted_kinds = {p["environment_kind"] for p in promotions}
        pin = None
        if row.get("source_pin_id"):
            pin_row = u.conn.execute(
                "SELECT * FROM catalog_source_pins WHERE id = %s", (row["source_pin_id"],)
            ).fetchone()
            if pin_row:
                pin = {
                    "id": str(pin_row["id"]),
                    "source_kind": pin_row["source_kind"],
                    "commit_sha": pin_row["commit_sha"],
                    "artifact_digest": pin_row["artifact_digest"],
                    "reviewed": pin_row["reviewed"],
                }
        if (
            row["status"] == "published"
            and u.p.has_role("template_publisher")
        ):
            for kind in registry.ENV_KINDS:
                if kind in row["contract"]["deployment_policy"].get("admissible_environment_kinds", []) \
                        and kind not in promoted_kinds:
                    actions.append(f"promote:{kind}")
        return {
            "slug": slug,
            "version": version,
            "status": row["status"],
            "digest": row["digest"],
            "contract": row["contract"],
            "published_at": iso(row["published_at"]),
            "source_pin": pin,
            "promotions": [
                {
                    "environment_kind": p["environment_kind"],
                    "promoted_at": iso(p["promoted_at"]),
                    "reason": p["reason"],
                }
                for p in promotions
            ],
            "components": [
                {
                    "slug": c["slug"],
                    "version": c["version"],
                    "status": c["status"],
                    "digest": c["digest"],
                    "title": c["title"],
                    "verification_level": c["verification_level"],
                    "evidence": registry.evidence_summary(
                        u.conn, registry.component(u.conn, c["slug"], c["version"])["id"]
                    ),
                }
                for c in comps
            ],
            "history": [
                {
                    "version": h["version"],
                    "action": h["action"],
                    "reason": h["reason"],
                    "actor": h["actor"],
                    "at": iso(h["at"]),
                }
                for h in registry.template_history(u.conn, slug)
            ],
            "review": studio.review_view(u.conn, row) if row["draft_id"] else None,
            "separation_notice": "Vous avez soumis cette version : une autre personne la publie."
            if own and row["status"] == "in_review"
            else None,
            "allowed_actions": actions,
        }


@router.get("/admin/templates/{slug}/versions/{version}/impact")
def template_impact(slug: str, version: str, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        if not u.p.has_role("template_publisher"):
            raise forbidden("rôle template_publisher requis")
        if registry.template(u.conn, slug, version) is None:
            raise not_found("version de template")
        return registry.impact(u.conn, slug, version)


def _publication(slug: str, version: str, action: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    _only(body, {"reason", "digest"})
    with unit_of_work(request, write=True) as u:
        return registry.publish_action(
            u.conn,
            u.p,
            slug,
            version,
            action,
            str(body.get("reason") or ""),
            str(body["digest"]) if body.get("digest") else None,
        )


@router.post("/admin/templates/{slug}/versions/{version}/publish")
def publish(slug: str, version: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _publication(slug, version, "publish", request, body)


@router.post("/admin/templates/{slug}/versions/{version}/return")
def return_to_author(slug: str, version: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _publication(slug, version, "return", request, body)


@router.post("/admin/templates/{slug}/versions/{version}/deprecate")
def deprecate(slug: str, version: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _publication(slug, version, "deprecate", request, body)


@router.post("/admin/templates/{slug}/versions/{version}/withdraw")
def withdraw(slug: str, version: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _publication(slug, version, "withdraw", request, body)


@router.post("/admin/templates/{slug}/versions/{version}/promote")
def promote(slug: str, version: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F01 : promotion vers un kind d'environnement — distincte de la publication, aucun job."""
    _only(body, {"reason", "environment_kind"})
    with unit_of_work(request, write=True) as u:
        return registry.promote(
            u.conn,
            u.p,
            slug,
            version,
            str(body.get("environment_kind") or ""),
            str(body.get("reason") or ""),
        )


@router.post("/admin/catalog/pins/{pin_id}/review")
def review_catalog_pin(pin_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F01 : marque une épingle Git/artefact comme revue (préalable à la publication)."""
    from ..registry import git_source

    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        if not u.p.has_role("template_publisher"):
            raise forbidden("rôle template_publisher requis")
        pin = git_source.review_pin(u.conn, pin_id, u.p.user_id, str(body.get("reason") or ""))
        return {
            "id": str(pin["id"]),
            "source_kind": pin["source_kind"],
            "commit_sha": pin["commit_sha"],
            "artifact_digest": pin["artifact_digest"],
            "reviewed": pin["reviewed"],
            "review_reason": pin["review_reason"],
            "reviewed_at": iso(pin["reviewed_at"]),
        }


@router.get("/admin/environments/{environment_id}/policies")
def environment_policies(environment_id: UUID, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        if not (u.p.is_platform_admin or u.p.is_global_auditor):
            raise forbidden("rôle administrateur ou auditeur requis")
        if projects.get_environment(u.conn, environment_id) is None:
            raise not_found("environnement")
        rows = u.conn.execute("SELECT version, digest, rules, reason, created_at FROM environment_policies "
                              "WHERE environment_id = %s ORDER BY version", (environment_id,)).fetchall()
        return [{**r, "created_at": iso(r["created_at"])} for r in rows]


def _connector(conn, connector_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM connectors WHERE id = %s", (connector_id,)).fetchone()
    if row is None:
        raise not_found("connecteur")
    return row


def _connector_view(row: dict[str, Any]) -> dict[str, Any]:
    circ = circuit.summary(row)
    return {"id": str(row["id"]), "slug": row["slug"], "kind": row["kind"], "mode": row["mode"],
            "simulated": row["mode"] == "simulated", "config": row["config"], "config_version": row["config_version"],
            "secrets": {name: "défini" for name in row["secret_refs"]}, "capabilities": row["capabilities"],
            "last_test_at": iso(row["last_test_at"]), "last_test_result": row["last_test_result"],
            "error_count": row["error_count"],
            "circuit": {**circ, "opened_at": iso(circ["opened_at"]) if circ["opened_at"] else None}}


def _vsphere_config(conn: Conn, row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Valide la configuration vSphere ; l'URL devient un endpoint approuvé portant la CA importée."""
    try:
        cfg = vsphere.VsphereConfig.model_validate(config)
    except ValidationError as exc:
        err = exc.errors()[0]
        field = "/config/" + "/".join(str(p) for p in err["loc"])
        if err["type"] == "extra_forbidden":
            raise ApiError(422, "invalid_config", "champ inconnu : la vérification TLS ne se désactive pas, "
                           "importez la CA du vCenter (ca_pem)", field=field) from None
        raise ApiError(422, "invalid_config", str(err["msg"]).removeprefix("Value error, "), field=field) from None
    if cfg.url is None:
        return {}
    host, port = vsphere.endpoint(cfg.url)
    conn.execute("INSERT INTO approved_endpoints (id, connector_id, scheme, host, port, ca_bundle_pem) "
                 "VALUES (%s, %s, 'https', %s, %s, %s) ON CONFLICT (connector_id, scheme, host, port) "
                 "DO UPDATE SET ca_bundle_pem = %s", (uuid7(), row["id"], host, port, cfg.ca_pem, cfg.ca_pem))
    return {"url": cfg.url, "ca_imported": cfg.ca_pem is not None}


def _vsphere_test(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    url = row["config"].get("url")
    if not url:
        return vsphere.failure("not_configured")
    host, port = vsphere.endpoint(url)
    ep = conn.execute("SELECT ca_bundle_pem FROM approved_endpoints WHERE connector_id = %s AND scheme = 'https' "
                      "AND host = %s AND port = %s", (row["id"], host, port)).fetchone()
    if ep is None:
        return vsphere.failure("endpoint_not_approved")
    return vsphere.test_connection(host, port, ep["ca_bundle_pem"])


@router.get("/connectors")
def list_connectors(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        if not (u.p.is_platform_admin or u.p.bindings):
            raise forbidden("rôle requis")
        return [_connector_view(r) for r in u.conn.execute("SELECT * FROM connectors ORDER BY slug").fetchall()]


@router.put("/connectors/{connector_id}")
def update_connector(connector_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"config"})
    config = body.get("config")
    if not isinstance(config, dict):
        raise ApiError(422, "invalid_config", "objet attendu", field="/config")
    for key in config:
        if any(h in key.lower() for h in SECRET_HINTS):
            raise ApiError(422, "secret_in_config", "un secret se saisit par la route des secrets, jamais dans la "
                           "configuration", field=f"/config/{key}")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = _connector(u.conn, connector_id)
        if row["kind"] == "vsphere":
            config = _vsphere_config(u.conn, row, config)
        row = u.conn.execute("UPDATE connectors SET config = %s, config_version = config_version + 1 WHERE id = %s "
                             "RETURNING *", (Jsonb(config), row["id"])).fetchone()
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="connector.update", outcome="ok",
                     targets={"connector": row["slug"]}, versions={"config_version": row["config_version"]},
                     request_id=u.request_id)
        return _connector_view(row)


@router.put("/connectors/{connector_id}/secrets/{name}")
def put_connector_secret(connector_id: UUID, name: str, request: Request,
                         body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"value"})
    value = body.get("value")
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ApiError(422, "invalid_secret", "valeur attendue", field="/value")
    if not name.replace("_", "").isalnum() or len(name) > 40:
        raise ApiError(422, "invalid_secret_name", "nom de secret invalide")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = _connector(u.conn, connector_id)
        ref = f"secret://connectors/{row['slug']}/{name}"
        secrets.put(u.conn, ref, value.encode(), u.p.user_id)
        refs = dict(row["secret_refs"], **{name: ref})
        u.conn.execute("UPDATE connectors SET secret_refs = %s, config_version = config_version + 1 WHERE id = %s",
                       (Jsonb(refs), row["id"]))
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="connector.secret.put",
                     outcome="ok", targets={"connector": row["slug"], "secret": name}, request_id=u.request_id)
        return {"connector": row["slug"], "secret": name, "status": "défini"}


@router.post("/connectors/{connector_id}/circuit/open")
def open_connector_circuit(connector_id: UUID, request: Request,
                           body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Ouvre manuellement le disjoncteur (F37) : suspend les nouvelles écritures vers ce connecteur."""
    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = _connector(u.conn, connector_id)
        trip = circuit.open_circuit(
            u.conn, row["id"], body.get("reason"),
            actor_type="user", actor_id=str(u.p.user_id), request_id=u.request_id,
        )
        view = _connector_view(trip["connector"])
        return {"connector": view, "opened": trip["opened"], "write_jobs": trip["write_jobs"]}


@router.post("/connectors/{connector_id}/test")
def test_connector(connector_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = _connector(u.conn, connector_id)
        cap = row["capabilities"].get("test_connection") or MATRIX[row["kind"]]["test_connection"]
        result: dict[str, Any]
        if not cap["available"]:
            result = {"status": "unavailable", "simulated": False, "message": "test non disponible pour ce connecteur"}
        elif cap["mode"] == "simulated":
            result = {"status": "ok", "simulated": True,
                      "message": "Test simulé : aucun appel externe (connecteur fictif)"}
        elif row["kind"] == "vsphere":
            result = _vsphere_test(u.conn, row)
        else:
            result = {"status": "not_verified", "simulated": False,
                      "message": "Vérifié à l'exécution par le runner ; voir `make doctor`"}
        result["at"] = datetime.now(UTC).isoformat()
        u.conn.execute("UPDATE connectors SET last_test_at = now(), last_test_result = %s WHERE id = %s",
                       (Jsonb(result), row["id"]))
        status = str(result["status"])
        if status == "failed":
            reason = str(result.get("message") or result.get("code") or "test de connexion échoué")
            trip = circuit.record_failure(
                u.conn, row["id"], reason, actor_type="user", actor_id=str(u.p.user_id),
            )
            circ = circuit.summary(trip["connector"])
            result["circuit"] = circ
            result["circuit_opened"] = trip["opened"]
        elif status == "ok":
            close = circuit.note_success(
                u.conn, row["id"], actor_type="user", actor_id=str(u.p.user_id), request_id=u.request_id,
            )
            circ = circuit.summary(close["connector"])
            result["circuit"] = circ
            result["circuit_closed"] = close["closed"]
            if close.get("blocked_by_uncertain"):
                result["circuit_close_blocked"] = close["blocked_by_uncertain"]
                result["message"] = close.get("message") or result.get("message")
        else:
            circ = circuit.summary(row)
            result["circuit"] = circ
        circ_view = result["circuit"]
        assert isinstance(circ_view, dict)
        circ_view["opened_at"] = iso(circ_view["opened_at"]) if circ_view.get("opened_at") else None
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="connector.test",
                     outcome=status, targets={"connector": row["slug"]}, request_id=u.request_id)
        return result


@router.post("/connectors/{connector_id}/inventory-refresh")
def refresh_inventory(connector_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = _connector(u.conn, connector_id)
        if row["kind"] != "vsphere" or row["mode"] != "simulated":
            raise ApiError(409, "inventory_unavailable", "inventaire disponible uniquement pour le connecteur "
                           "vSphere fictif au lot 1")
        fixture = inventory.load_fixture(get_settings().fixtures_dir / "inventory.yaml")
        snapshot = inventory.record_snapshot(u.conn, row["id"], fixture["items"])
        emit(u.conn, type="inventory.refreshed", source="vcenter",
             data={"snapshot_id": str(snapshot), "items": len(fixture["items"]), "simulated": True})
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="inventory.refresh", outcome="ok",
                     targets={"connector": row["slug"]}, versions={"snapshot": str(snapshot)},
                     request_id=u.request_id)
        return {"snapshot_id": str(snapshot), "items": len(fixture["items"]), "simulated": True}


@router.get("/users")
def list_users(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        require_admin(u.p)
        users = u.conn.execute("SELECT id, email, display_name, status, is_demo FROM users ORDER BY email").fetchall()
        bindings = u.conn.execute("SELECT b.id, b.user_id, b.role, p.slug AS project, b.environment_id "
                                  "FROM role_bindings b LEFT JOIN projects p ON p.id = b.project_id "
                                  "WHERE b.revoked_at IS NULL").fetchall()
        return [{**usr, "id": str(usr["id"]),
                 "roles": [{"id": str(b["id"]), "role": b["role"], "project": b["project"],
                            "environment_id": str(b["environment_id"]) if b["environment_id"] else None}
                           for b in bindings if b["user_id"] == usr["id"]]} for usr in users]


@router.post("/users")
def create_user(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"email", "display_name", "password"})
    if not all(isinstance(body.get(k), str) and body[k] for k in ("email", "display_name", "password")):
        raise ApiError(422, "invalid_user", "email, nom et mot de passe requis")
    if len(body["password"]) < 12:
        raise ApiError(422, "weak_password", "12 caractères au moins", field="/password")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        if u.conn.execute("SELECT 1 FROM users WHERE email = %s", (body["email"].lower(),)).fetchone():
            raise ApiError(409, "user_exists", "un utilisateur existe déjà avec cet e-mail")
        user_id = identity.create_user(u.conn, body["email"], body["display_name"][:120], body["password"])
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="user.create", outcome="ok",
                     targets={"user": str(user_id)}, request_id=u.request_id)
        return {"id": str(user_id)}


@router.post("/role-bindings")
def grant_role(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"user_id", "role", "project_id", "environment_id"})
    role = body.get("role")
    if role not in ASSIGNABLE_ROLES:
        raise ApiError(422, "invalid_role", f"rôle attendu : {', '.join(ASSIGNABLE_ROLES)}", field="/role")
    try:
        user_id = UUID(str(body.get("user_id")))
        project_id = UUID(body["project_id"]) if body.get("project_id") else None
        environment_id = UUID(body["environment_id"]) if body.get("environment_id") else None
    except ValueError as exc:
        raise ApiError(422, "invalid_identifier", "identifiant invalide") from exc
    if role in ("platform_admin", "template_publisher", "template_author") and project_id is not None:
        raise ApiError(422, "global_role", "ce rôle est global", field="/project_id")
    if role in ("operator", "approver") and project_id is None:
        raise ApiError(422, "project_required", "ce rôle exige un projet", field="/project_id")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        if u.conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            raise not_found("utilisateur")
        if environment_id is not None:
            env = projects.get_environment(u.conn, environment_id)
            if env is None or env["project_id"] != project_id:
                raise ApiError(422, "invalid_environment", "environnement hors du projet", field="/environment_id")
        if (
            role == "template_publisher"
            and u.conn.execute(
                "SELECT 1 FROM role_bindings WHERE role = 'template_publisher' AND revoked_at IS NULL"
            ).fetchone()
        ):
            raise ApiError(409, "single_publisher", "une seule personne publie au lot 1 (décision D16)")
        binding_id = uuid7()
        u.conn.execute(
            "INSERT INTO role_bindings (id, user_id, role, project_id, environment_id, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (binding_id, user_id, role, project_id, environment_id, u.p.user_id),
        )
        audit.record(
            u.conn,
            actor_type="user",
            actor_id=str(u.p.user_id),
            action="role.grant",
            outcome="ok",
            project_id=project_id,
            targets={"user": str(user_id), "role": role, "environment": str(environment_id or "")},
            request_id=u.request_id,
        )
        return {"id": str(binding_id)}


@router.post("/role-bindings/{binding_id}/revoke")
def revoke_role(binding_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        row = u.conn.execute("UPDATE role_bindings SET revoked_at = now(), revoked_by = %s "
                             "WHERE id = %s AND revoked_at IS NULL RETURNING *", (u.p.user_id, binding_id)).fetchone()
        if row is None:
            raise not_found("attribution de rôle")
        identity.bump_privilege_epoch(u.conn, row["user_id"], "role_revoked")
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="role.revoke", outcome="ok",
                     project_id=row["project_id"], targets={"user": str(row["user_id"]), "role": row["role"]},
                     request_id=u.request_id)
        return {"id": str(binding_id), "revoked": True, "sessions": "invalidées"}


@router.get("/audit")
def list_audit(request: Request, project_id: UUID | None = None, correlation_id: UUID | None = None,
               limit: int = 200) -> dict[str, Any]:
    with unit_of_work(request) as u:
        require_auditor(u.p, project_id)
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = %s")
            params.append(project_id)
        elif not u.p.is_global_auditor:
            clauses.append("project_id IS NULL")
        if correlation_id is not None:
            clauses.append("correlation_id = %s")
            params.append(correlation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = u.conn.execute(f"SELECT id, at, actor_type, actor_id, action, project_id, correlation_id, targets, "
                              f"versions, policy_decision, outcome, request_id, hash FROM audit_log {where} "
                              f"ORDER BY id DESC LIMIT %s", (*params, max(1, min(limit, 1000)))).fetchall()
        return {"entries": [{**r, "at": iso(r["at"])} for r in rows], "chain": audit.verify(u.conn)}


@router.get("/platform/status")
def platform_status(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        require_admin(u.p)
        settings = get_settings()
        workers = u.conn.execute("SELECT id, hostname, heartbeat_at, slots, busy FROM workers "
                                 "ORDER BY heartbeat_at DESC LIMIT 20").fetchall()
        alive = [w for w in workers if w["heartbeat_at"] > datetime.now(UTC) - timedelta(seconds=60)]
        queue = u.conn.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state").fetchall()
        assistant = u.conn.execute("SELECT value FROM platform_settings WHERE key = 'assistant'").fetchone()
        return {"profile": settings.profile, "version": os.environ.get("RACKLOOM_VERSION", "dev"),
                "workers": [{**w, "heartbeat_at": iso(w["heartbeat_at"])} for w in workers],
                "workers_alive": len(alive), "jobs": {r["state"]: r["n"] for r in queue},
                "inventory": inventory.freshness(u.conn, settings.inventory_max_age_hours),
                "assistant": {"adapter": settings.chat_adapter, **((assistant or {}).get("value") or {})},
                "audit": audit.verify(u.conn)}


health = APIRouter(tags=["santé"])


@health.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/health/ready")
def ready() -> dict[str, Any]:
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
            head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            conn.rollback()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "not_ready", "base de données indisponible") from exc
    return {"status": "ok", "migration": head["version_num"] if head else None}
