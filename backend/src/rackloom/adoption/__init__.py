"""F08 : adoption de VM existantes.

Enrôlement et observation en lecture seule d'abord. La proposition d'import
Terraform est séparée de son exécution. Une gestion concurrente par un autre
state bloque l'import. L'aperçu ne modifie jamais la VM.

Sans laboratoire vSphere (D2) : observations et import sont fictifs / simulés
et portent le badge correspondant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import audit, events, inventory
from ..db import Conn, Jsonb
from ..errors import ApiError, conflict, not_found
from ..executions import queue
from ..identity.authz import Principal, require_role
from ..ids import uuid7
from ..projects import environments as list_environments
from ..projects import get_project

STATES = (
    "observing",
    "preview_ready",
    "import_proposed",
    "blocked_conflict",
    "imported",
    "cancelled",
)

STATE_LABELS = {
    "observing": "Observation (lecture seule)",
    "preview_ready": "Aperçu prêt",
    "import_proposed": "Import proposé",
    "blocked_conflict": "Bloqué — conflit de propriété",
    "imported": "Importé (simulé)",
    "cancelled": "Annulé",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_adoption(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM vm_adoptions WHERE id = %s", (adoption_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("adoption")
    return row


def _environment_by_slug(
    conn: Conn, principal: Principal, project_id: UUID, environment_slug: str
) -> dict[str, Any]:
    for env in list_environments(conn, project_id):
        if env["slug"] == environment_slug:
            if not principal.can_see_project(project_id):
                raise not_found("environnement")
            return env
    raise not_found("environnement")


def _inventory_vm(conn: Conn, project_id: UUID, environment_id: UUID, vm_stable_id: str) -> dict[str, Any]:
    items = inventory.granted_items(conn, project_id, environment_id, kind="vm")
    for item in items:
        if item["stable_id"] == vm_stable_id:
            return item
    raise ApiError(
        404,
        "vm_not_granted",
        "VM absente de l'inventaire octroyé à cet environnement",
        field="/vm_stable_id",
    )


def _host_claim(conn: Conn, vm_stable_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, project_id, name, deployment_id FROM hosts WHERE vm_stable_id = %s LIMIT 1",
        (vm_stable_id,),
    ).fetchone()


def _external_claim(conn: Conn, vm_stable_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM terraform_resource_claims WHERE vm_stable_id = %s",
        (vm_stable_id,),
    ).fetchone()


def detect_ownership(
    conn: Conn,
    *,
    vm_stable_id: str,
    inventory_item: dict[str, Any] | None = None,
    exclude_adoption_id: UUID | None = None,
) -> dict[str, Any]:
    """Détermine la propriété actuelle sans écrire."""
    host = _host_claim(conn, vm_stable_id)
    if host is not None:
        return {
            "ownership": "rackloom",
            "conflict": True,
            "reason": f"VM déjà gérée par Rackloom (hôte {host['name']})",
            "host_id": str(host["id"]),
            "external_state_id": None,
        }
    claim = _external_claim(conn, vm_stable_id)
    if claim is not None:
        if exclude_adoption_id and claim.get("adoption_id") == exclude_adoption_id:
            return {
                "ownership": "rackloom" if claim["claim_source"] == "rackloom" else "external_state",
                "conflict": False,
                "reason": None,
                "host_id": None,
                "external_state_id": claim["state_id"],
            }
        if claim["claim_source"] == "external":
            return {
                "ownership": "external_state",
                "conflict": True,
                "reason": f"VM revendiquée par un state externe ({claim['state_id']})",
                "host_id": None,
                "external_state_id": claim["state_id"],
            }
        return {
            "ownership": "rackloom",
            "conflict": True,
            "reason": f"VM déjà revendiquée par un state Rackloom ({claim['state_id']})",
            "host_id": None,
            "external_state_id": claim["state_id"],
        }
    attrs = (inventory_item or {}).get("attributes") or {}
    managed_by = attrs.get("managed_by")
    external_state = attrs.get("terraform_state_id") or attrs.get("external_state_id")
    if managed_by or external_state:
        state_id = str(external_state or managed_by)
        return {
            "ownership": "external_state",
            "conflict": True,
            "reason": f"Inventaire : gestion concurrente ({state_id})",
            "host_id": None,
            "external_state_id": state_id,
        }
    return {
        "ownership": "unmanaged",
        "conflict": False,
        "reason": None,
        "host_id": None,
        "external_state_id": None,
    }


def _fictional_observation(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    return {
        "fictional": True,
        "source": "fictional_inventory",
        "notice": "Observation fictive explicite (pas de collecteur vSphere réel — D2).",
        "vm_stable_id": item["stable_id"],
        "name": item["name"],
        "power": attrs.get("power", "poweredOn"),
        "guest_os": attrs.get("guest_os"),
        "vcpu": attrs.get("vcpu"),
        "memory_mb": attrs.get("memory_mb"),
        "disk_gb": attrs.get("disk_gb"),
        "cluster": attrs.get("cluster"),
        "observed_at": datetime.now(UTC).isoformat(),
        "mutation": False,
    }


def _build_import_proposal(adoption: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    resource_address = f'vsphere_virtual_machine.adopted["{adoption["vm_name"]}"]'
    proposal = {
        "schema": "rackloom.tf_import_proposal/v1",
        "simulated": True,
        "resource_address": resource_address,
        "import_id": adoption["vm_stable_id"],
        "module": {"slug": "lab-vm", "version": "1.0.0"},
        "observation_digest": {
            "vcpu": observation.get("vcpu"),
            "memory_mb": observation.get("memory_mb"),
            "disk_gb": observation.get("disk_gb"),
            "guest_os": observation.get("guest_os"),
        },
        "notice": "Proposition seule : aucune écriture sur la VM ni sur un state.",
    }
    return proposal


def view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    env = conn.execute(
        "SELECT slug, name, kind, is_production FROM environments WHERE id = %s",
        (row["environment_id"],),
    ).fetchone()
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "environment": {
            "id": str(row["environment_id"]),
            "slug": env["slug"],
            "name": env["name"],
            "kind": env["kind"],
            "is_production": env["is_production"],
        },
        "vm_stable_id": row["vm_stable_id"],
        "vm_name": row["vm_name"],
        "connector_slug": row["connector_slug"],
        "state": row["state"],
        "state_label": STATE_LABELS.get(row["state"], row["state"]),
        "ownership": row["ownership"],
        "conflict_reason": row["conflict_reason"],
        "external_state_id": row["external_state_id"],
        "observation": row["observation"],
        "observation_fictional": row["observation_fictional"],
        "preview": row["preview"],
        "previewed_at": _iso(row["previewed_at"]),
        "import_proposal": row["import_proposal"],
        "import_proposal_sha256": row["import_proposal_sha256"],
        "proposed_at": _iso(row["proposed_at"]),
        "approved_at": _iso(row["approved_at"]),
        "approval_proposal_sha256": row["approval_proposal_sha256"],
        "imported_at": _iso(row["imported_at"]),
        "import_job_id": str(row["import_job_id"]) if row["import_job_id"] else None,
        "enrolled_at": _iso(row["enrolled_at"]),
        "updated_at": _iso(row["updated_at"]),
        "simulated": True,
        "notice": "Adoption : lecture seule jusqu'à l'exécution d'un import approuvé (simulé).",
    }


def list_candidates(
    conn: Conn,
    principal: Principal,
    project_ref: str,
    *,
    environment_slug: str,
) -> dict[str, Any]:
    """VM octroyées non encore enrôlées comme hôte Rackloom ni adoption active."""
    project = get_project(conn, principal, project_ref)
    env = _environment_by_slug(conn, principal, project["id"], environment_slug)
    items = inventory.granted_items(conn, project["id"], env["id"], kind="vm")
    enrolled = {
        r["vm_stable_id"]
        for r in conn.execute(
            "SELECT vm_stable_id FROM vm_adoptions WHERE project_id = %s AND state <> 'cancelled'",
            (project["id"],),
        ).fetchall()
    }
    managed = {
        r["vm_stable_id"]
        for r in conn.execute(
            "SELECT vm_stable_id FROM hosts WHERE project_id = %s", (project["id"],)
        ).fetchall()
    }
    candidates = []
    for item in items:
        ownership = detect_ownership(conn, vm_stable_id=item["stable_id"], inventory_item=item)
        candidates.append(
            {
                "vm_stable_id": item["stable_id"],
                "name": item["name"],
                "attributes": {
                    k: item["attributes"].get(k)
                    for k in ("power", "guest_os", "vcpu", "memory_mb", "disk_gb", "managed_by")
                    if k in (item.get("attributes") or {})
                },
                "ownership": ownership["ownership"],
                "conflict": ownership["conflict"],
                "already_enrolled": item["stable_id"] in enrolled,
                "already_host": item["stable_id"] in managed,
                "fictional": True,
                "observed_at": item["observed_at"].isoformat() if item.get("observed_at") else None,
            }
        )
    return {
        "environment": {"id": str(env["id"]), "slug": env["slug"], "kind": env["kind"]},
        "candidates": candidates,
        "fictional": True,
        "notice": "Inventaire simulé : observations fictives explicites (D2).",
    }


def list_adoptions(conn: Conn, principal: Principal, project_ref: str) -> list[dict[str, Any]]:
    project = get_project(conn, principal, project_ref)
    rows = conn.execute(
        "SELECT * FROM vm_adoptions WHERE project_id = %s ORDER BY enrolled_at DESC",
        (project["id"],),
    ).fetchall()
    return [view(conn, r) for r in rows]


def get(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    return view(conn, _get_adoption(conn, principal, adoption_id))


def enroll(
    conn: Conn,
    principal: Principal,
    project_ref: str,
    *,
    environment_slug: str,
    vm_stable_id: str,
) -> dict[str, Any]:
    """Enrôle une VM pour observation en lecture seule (aucune mutation)."""
    project = get_project(conn, principal, project_ref)
    env = _environment_by_slug(conn, principal, project["id"], environment_slug)
    require_role(principal, "operator", project["id"], env["id"])
    if not vm_stable_id or not vm_stable_id.strip():
        raise ApiError(422, "missing_field", "vm_stable_id requis", field="/vm_stable_id")
    item = _inventory_vm(conn, project["id"], env["id"], vm_stable_id)
    ownership = detect_ownership(conn, vm_stable_id=vm_stable_id, inventory_item=item)
    existing = conn.execute(
        "SELECT id, state FROM vm_adoptions WHERE project_id = %s AND vm_stable_id = %s",
        (project["id"], vm_stable_id),
    ).fetchone()
    if existing and existing["state"] != "cancelled":
        raise conflict("adoption_exists", "une adoption est déjà en cours pour cette VM")

    observation = _fictional_observation(item)
    state = "blocked_conflict" if ownership["conflict"] else "observing"
    ownership_code = "conflict" if ownership["conflict"] else ownership["ownership"]
    adoption_id = existing["id"] if existing else uuid7()

    if existing:
        row = conn.execute(
            """
            UPDATE vm_adoptions SET
                environment_id = %s, vm_name = %s, state = %s, ownership = %s,
                conflict_reason = %s, external_state_id = %s, observation = %s,
                observation_fictional = true, preview = NULL, previewed_at = NULL,
                import_proposal = NULL, import_proposal_sha256 = NULL, proposed_at = NULL,
                proposed_by = NULL, approved_at = NULL, approved_by = NULL,
                approval_proposal_sha256 = NULL, imported_at = NULL, import_job_id = NULL,
                enrolled_by = %s, enrolled_at = now(), updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (
                env["id"],
                item["name"],
                state,
                ownership_code,
                ownership["reason"],
                ownership["external_state_id"],
                Jsonb(observation),
                principal.user_id,
                adoption_id,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            """
            INSERT INTO vm_adoptions (
                id, project_id, environment_id, vm_stable_id, vm_name, state, ownership,
                conflict_reason, external_state_id, observation, observation_fictional, enrolled_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s) RETURNING *
            """,
            (
                adoption_id,
                project["id"],
                env["id"],
                vm_stable_id,
                item["name"],
                state,
                ownership_code,
                ownership["reason"],
                ownership["external_state_id"],
                Jsonb(observation),
                principal.user_id,
            ),
        ).fetchone()

    conn.execute(
        "INSERT INTO adoption_observations (adoption_id, project_id, data, status, source, fictional) "
        "VALUES (%s, %s, %s, 'ok', 'fictional', true)",
        (row["id"], project["id"], Jsonb(observation)),
    )
    events.emit(
        conn,
        type="adoption.enrolled",
        source="system",
        project_id=project["id"],
        environment_id=env["id"],
        correlation_id=row["id"],
        data={
            "message": f"Adoption enrôlée : {item['name']}",
            "vm_stable_id": vm_stable_id,
            "state": state,
            "fictional": True,
        },
        dedupe_key=f"adoption.enrolled:{row['id']}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.enroll",
        outcome="ok",
        project_id=project["id"],
        targets={"adoption_id": str(row["id"]), "vm_stable_id": vm_stable_id, "state": state},
        versions={"fictional": "true"},
    )
    return view(conn, row)


def observe(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    """Rafraîchit l'observation fictive (lecture seule : aucune mutation inventaire/VM)."""
    row = _get_adoption(conn, principal, adoption_id)
    require_role(principal, "operator", row["project_id"], row["environment_id"])
    if row["state"] in ("imported", "cancelled"):
        raise conflict("adoption_closed", "adoption terminée ou annulée")
    item = _inventory_vm(conn, row["project_id"], row["environment_id"], row["vm_stable_id"])
    # Empreinte avant observation pour prouver l'absence de mutation.
    before = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = %s "
        "ORDER BY observed_at DESC LIMIT 1",
        (row["vm_stable_id"],),
    ).fetchone()
    observation = _fictional_observation(item)
    ownership = detect_ownership(
        conn,
        vm_stable_id=row["vm_stable_id"],
        inventory_item=item,
        exclude_adoption_id=row["id"],
    )
    state = "blocked_conflict" if ownership["conflict"] else (
        "observing" if row["state"] == "blocked_conflict" else row["state"]
    )
    ownership_code = "conflict" if ownership["conflict"] else ownership["ownership"]
    updated = conn.execute(
        """
        UPDATE vm_adoptions SET observation = %s, observation_fictional = true,
            ownership = %s, conflict_reason = %s, external_state_id = %s,
            state = %s, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (
            Jsonb(observation),
            ownership_code,
            ownership["reason"],
            ownership["external_state_id"],
            state,
            row["id"],
        ),
    ).fetchone()
    conn.execute(
        "INSERT INTO adoption_observations (adoption_id, project_id, data, status, source, fictional) "
        "VALUES (%s, %s, %s, 'ok', 'fictional', true)",
        (row["id"], row["project_id"], Jsonb(observation)),
    )
    after = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = %s "
        "ORDER BY observed_at DESC LIMIT 1",
        (row["vm_stable_id"],),
    ).fetchone()
    if before and after and before["attributes"] != after["attributes"]:
        raise ApiError(500, "observation_mutated", "l'observation a modifié l'inventaire")
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.observe",
        outcome="ok",
        project_id=row["project_id"],
        targets={"adoption_id": str(row["id"]), "mutation": "false"},
    )
    return view(conn, updated)


def preview_import(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    """Aperçu d'import Terraform : ne modifie ni la VM, ni le state, ni les revendications."""
    row = _get_adoption(conn, principal, adoption_id)
    require_role(principal, "operator", row["project_id"], row["environment_id"])
    if row["state"] in ("imported", "cancelled"):
        raise conflict("adoption_closed", "adoption terminée ou annulée")

    claims_before = conn.execute(
        "SELECT count(*) AS n FROM terraform_resource_claims WHERE vm_stable_id = %s",
        (row["vm_stable_id"],),
    ).fetchone()["n"]
    item_attrs_before = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = %s "
        "ORDER BY observed_at DESC LIMIT 1",
        (row["vm_stable_id"],),
    ).fetchone()

    item = _inventory_vm(conn, row["project_id"], row["environment_id"], row["vm_stable_id"])
    ownership = detect_ownership(
        conn,
        vm_stable_id=row["vm_stable_id"],
        inventory_item=item,
        exclude_adoption_id=row["id"],
    )
    proposal = _build_import_proposal(row, row["observation"] or _fictional_observation(item))
    preview = {
        "preview": True,
        "mutation": False,
        "import_executed": False,
        "ownership": ownership,
        "proposal": proposal,
        "proposal_sha256": _sha256(proposal),
        "notice": "Aperçu seul : la VM et le state restent inchangés.",
        "simulated": True,
        "fictional_observation": True,
    }

    new_state = "blocked_conflict" if ownership["conflict"] else "preview_ready"
    ownership_code = "conflict" if ownership["conflict"] else ownership["ownership"]
    updated = conn.execute(
        """
        UPDATE vm_adoptions SET preview = %s, previewed_at = now(),
            ownership = %s, conflict_reason = %s, external_state_id = %s,
            state = CASE
                WHEN state IN ('import_proposed', 'imported', 'cancelled') THEN state
                ELSE %s
            END,
            updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (
            Jsonb(preview),
            ownership_code,
            ownership["reason"],
            ownership["external_state_id"],
            new_state,
            row["id"],
        ),
    ).fetchone()

    claims_after = conn.execute(
        "SELECT count(*) AS n FROM terraform_resource_claims WHERE vm_stable_id = %s",
        (row["vm_stable_id"],),
    ).fetchone()["n"]
    item_attrs_after = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = %s "
        "ORDER BY observed_at DESC LIMIT 1",
        (row["vm_stable_id"],),
    ).fetchone()
    if claims_before != claims_after:
        raise ApiError(500, "preview_mutated_claims", "l'aperçu a créé une revendication")
    if item_attrs_before and item_attrs_after and item_attrs_before["attributes"] != item_attrs_after["attributes"]:
        raise ApiError(500, "preview_mutated_vm", "l'aperçu a modifié la VM")

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.preview",
        outcome="ok",
        project_id=row["project_id"],
        targets={"adoption_id": str(row["id"]), "mutation": "false", "conflict": str(ownership["conflict"])},
    )
    result = view(conn, updated)
    result["preview_result"] = preview
    return result


def propose_import(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    """Enregistre la proposition d'import (sans l'exécuter)."""
    row = _get_adoption(conn, principal, adoption_id)
    require_role(principal, "operator", row["project_id"], row["environment_id"])
    if row["state"] in ("imported", "cancelled"):
        raise conflict("adoption_closed", "adoption terminée ou annulée")
    item = _inventory_vm(conn, row["project_id"], row["environment_id"], row["vm_stable_id"])
    ownership = detect_ownership(
        conn,
        vm_stable_id=row["vm_stable_id"],
        inventory_item=item,
        exclude_adoption_id=row["id"],
    )
    if ownership["conflict"]:
        updated = conn.execute(
            """
            UPDATE vm_adoptions SET state = 'blocked_conflict', ownership = 'conflict',
                conflict_reason = %s, external_state_id = %s, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (ownership["reason"], ownership["external_state_id"], row["id"]),
        ).fetchone()
        audit.record(
            conn,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="adoption.propose",
            outcome="denied",
            project_id=row["project_id"],
            targets={"adoption_id": str(row["id"]), "code": "ownership_conflict"},
        )
        raise ApiError(
            409,
            "ownership_conflict",
            ownership["reason"] or "conflit de propriété",
            details={"ownership": ownership, "adoption": view(conn, updated)},
            commit=True,
        )
    proposal = _build_import_proposal(row, row["observation"] or _fictional_observation(item))
    digest = _sha256(proposal)
    updated = conn.execute(
        """
        UPDATE vm_adoptions SET
            import_proposal = %s, import_proposal_sha256 = %s, proposed_at = now(),
            proposed_by = %s, state = 'import_proposed', ownership = 'unmanaged',
            conflict_reason = NULL, approved_at = NULL, approved_by = NULL,
            approval_proposal_sha256 = NULL, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (Jsonb(proposal), digest, principal.user_id, row["id"]),
    ).fetchone()
    events.emit(
        conn,
        type="adoption.import_proposed",
        source="terraform",
        project_id=row["project_id"],
        environment_id=row["environment_id"],
        correlation_id=row["id"],
        data={"message": "Proposition d'import Terraform enregistrée", "sha256": digest},
        dedupe_key=f"adoption.propose:{row['id']}:{digest}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.propose",
        outcome="ok",
        project_id=row["project_id"],
        targets={"adoption_id": str(row["id"]), "sha256": digest},
    )
    return view(conn, updated)


def approve_import(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    """Approuve la proposition figée (empreinte) ; distincte de l'auteur en production."""
    row = _get_adoption(conn, principal, adoption_id)
    require_role(principal, "approver", row["project_id"], row["environment_id"])
    if row["state"] != "import_proposed" or not row["import_proposal_sha256"]:
        raise conflict("proposal_required", "aucune proposition d'import à approuver")
    env = conn.execute(
        "SELECT is_production FROM environments WHERE id = %s", (row["environment_id"],)
    ).fetchone()
    if env["is_production"] and row["proposed_by"] == principal.user_id:
        raise ApiError(
            403,
            "approver_must_differ",
            "en production, l'approbateur doit être distinct de l'auteur de la proposition",
        )
    if row["proposed_by"] == principal.user_id:
        # Hors production : autorisé mais journalisé ; politique d'environnement peut exiger distinct.
        pass
    updated = conn.execute(
        """
        UPDATE vm_adoptions SET approved_at = now(), approved_by = %s,
            approval_proposal_sha256 = import_proposal_sha256, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (principal.user_id, row["id"]),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.approve",
        outcome="ok",
        project_id=row["project_id"],
        targets={
            "adoption_id": str(row["id"]),
            "sha256": updated["approval_proposal_sha256"],
        },
    )
    return view(conn, updated)


def execute_import(conn: Conn, principal: Principal, adoption_id: UUID) -> dict[str, Any]:
    """Exécute l'import simulé. Bloqué en cas de conflit de propriété."""
    row = _get_adoption(conn, principal, adoption_id)
    require_role(principal, "operator", row["project_id"], row["environment_id"])
    if row["state"] == "imported":
        return view(conn, row)
    if row["state"] == "cancelled":
        raise conflict("adoption_closed", "adoption annulée")
    if row["state"] != "import_proposed" or not row["import_proposal"]:
        raise conflict("proposal_required", "proposer l'import avant de l'exécuter")
    if not row["approved_at"] or row["approval_proposal_sha256"] != row["import_proposal_sha256"]:
        raise ApiError(
            409,
            "approval_required",
            "l'import exige une approbation liée à l'empreinte de la proposition",
        )

    item = _inventory_vm(conn, row["project_id"], row["environment_id"], row["vm_stable_id"])
    ownership = detect_ownership(
        conn,
        vm_stable_id=row["vm_stable_id"],
        inventory_item=item,
        exclude_adoption_id=row["id"],
    )
    if ownership["conflict"]:
        updated = conn.execute(
            """
            UPDATE vm_adoptions SET state = 'blocked_conflict', ownership = 'conflict',
                conflict_reason = %s, external_state_id = %s, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (ownership["reason"], ownership["external_state_id"], row["id"]),
        ).fetchone()
        audit.record(
            conn,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="adoption.import",
            outcome="denied",
            project_id=row["project_id"],
            targets={"adoption_id": str(row["id"]), "code": "ownership_conflict"},
        )
        raise ApiError(
            409,
            "ownership_conflict",
            ownership["reason"] or "conflit de propriété : import bloqué",
            details={"ownership": ownership, "adoption": view(conn, updated)},
            commit=True,
        )

    # Revendication Rackloom simulée (pas d'appel Terraform réel).
    existing_claim = _external_claim(conn, row["vm_stable_id"])
    if existing_claim is not None and existing_claim.get("adoption_id") != row["id"]:
        updated = conn.execute(
            """
            UPDATE vm_adoptions SET state = 'blocked_conflict', ownership = 'conflict',
                conflict_reason = %s, external_state_id = %s, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (
                f"VM déjà revendiquée ({existing_claim['state_id']})",
                existing_claim["state_id"],
                row["id"],
            ),
        ).fetchone()
        raise ApiError(
            409,
            "ownership_conflict",
            "conflit de propriété : un autre state revendique déjà cette VM",
            details={"ownership": detect_ownership(conn, vm_stable_id=row["vm_stable_id"]),
                     "adoption": view(conn, updated)},
            commit=True,
        )
    claim_id = uuid7()
    state_id = f"rackloom-adopt:{row['id']}"
    if existing_claim is None:
        conn.execute(
            """
            INSERT INTO terraform_resource_claims (
                id, vm_stable_id, claim_source, state_id, project_id, adoption_id, note, simulated
            ) VALUES (%s, %s, 'rackloom', %s, %s, %s, %s, true)
            """,
            (
                claim_id,
                row["vm_stable_id"],
                state_id,
                row["project_id"],
                row["id"],
                "Import Terraform simulé (F08)",
            ),
        )

    job = queue.enqueue(
        conn,
        kind="tf_import",
        idempotency_key=f"tf_import:{row['id']}:{row['import_proposal_sha256']}",
        project_id=row["project_id"],
        payload={
            "adoption_id": str(row["id"]),
            "vm_stable_id": row["vm_stable_id"],
            "proposal_sha256": row["import_proposal_sha256"],
            "simulated": True,
        },
        priority=80,
    )
    updated = conn.execute(
        """
        UPDATE vm_adoptions SET state = 'imported', ownership = 'rackloom',
            conflict_reason = NULL, imported_at = now(), import_job_id = %s, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (job["id"], row["id"]),
    ).fetchone()
    events.emit(
        conn,
        type="adoption.imported",
        source="terraform",
        project_id=row["project_id"],
        environment_id=row["environment_id"],
        correlation_id=row["id"],
        job_id=job["id"],
        data={
            "message": "Import Terraform simulé",
            "vm_stable_id": row["vm_stable_id"],
            "simulated": True,
        },
        dedupe_key=f"adoption.imported:{row['id']}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adoption.import",
        outcome="ok",
        project_id=row["project_id"],
        targets={
            "adoption_id": str(row["id"]),
            "job_id": str(job["id"]),
            "simulated": "true",
        },
    )
    result = view(conn, updated)
    result["job_id"] = str(job["id"])
    return result


def seed_external_claim(
    conn: Conn,
    *,
    vm_stable_id: str,
    state_id: str,
    note: str = "State Terraform externe (fictif)",
) -> dict[str, Any]:
    """Helper de test / démo : pose une revendication externe sans toucher la VM."""
    row = conn.execute(
        """
        INSERT INTO terraform_resource_claims (
            id, vm_stable_id, claim_source, state_id, note, simulated
        ) VALUES (%s, %s, 'external', %s, %s, true)
        ON CONFLICT (vm_stable_id) DO UPDATE SET
            claim_source = 'external', state_id = EXCLUDED.state_id,
            note = EXCLUDED.note, claimed_at = now()
        RETURNING *
        """,
        (uuid7(), vm_stable_id, state_id, note),
    ).fetchone()
    return dict(row)
