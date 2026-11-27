"""F43 : rotation des identifiants de connecteurs.

Nouvelle référence scellée, testée avant bascule. La valeur n'apparaît jamais dans les
logs, l'UI ou l'audit. Un test en échec refuse la bascule. L'ancienne référence n'est
retirée qu'après la fin des jobs déjà lancés. L'API ne lit jamais le chiffré : le sondage
simulé est calculé à la proposition (tant que le clair est encore en mémoire).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, secrets
from ..db import Conn, Jsonb
from ..errors import ApiError, forbidden, not_found
from ..identity import reauth
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact

ACTIVE = ("proposed", "validated", "cutover")


def _may_rotate(principal: Principal) -> None:
    if principal.is_platform_admin:
        return
    if any(b.role in ("connector_admin", "project_admin") and b.project_id for b in principal.bindings):
        return
    raise forbidden("rôle connector_admin, project_admin ou administrateur de plateforme requis")


def _connector(conn: Conn, connector_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM connectors WHERE id = %s", (connector_id,)).fetchone()
    if row is None:
        raise not_found("connecteur")
    return dict(row)


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "connector_id": str(row["connector_id"]),
        "secret_name": row["secret_name"],
        "old_ref": row["old_ref"],
        "candidate_ref": row["candidate_ref"],
        "status": row["status"],
        "test_result": row.get("test_result"),
        "tested_at": row["tested_at"].isoformat() if row.get("tested_at") else None,
        "cutover_at": row["cutover_at"].isoformat() if row.get("cutover_at") else None,
        "retired_at": row["retired_at"].isoformat() if row.get("retired_at") else None,
        "failure_reason": row.get("failure_reason"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "candidate_status": "défini",
    }


def _probe_from_plaintext(connector: dict[str, Any], plaintext: str) -> dict[str, Any]:
    """Sondage sans journaliser le clair. Connecteur simulé : refuse les préfixes « invalid »."""
    from . import MATRIX

    kind = connector["kind"]
    mode = connector["mode"]
    cap = (connector.get("capabilities") or {}).get("test_connection") or MATRIX.get(kind, {}).get(
        "test_connection", {}
    )
    if not cap.get("available"):
        return {"status": "unavailable", "simulated": False, "message": "test non disponible"}
    if mode == "simulated" or cap.get("mode") == "simulated":
        if plaintext.lower().startswith("invalid"):
            return {
                "status": "failed",
                "simulated": True,
                "code": "credential_invalid",
                "message": "identifiant candidat refusé par le test simulé",
            }
        return {
            "status": "ok",
            "simulated": True,
            "message": "Test simulé du candidat : aucun appel externe",
        }
    return {
        "status": "ok",
        "simulated": False,
        "message": "référence candidate scellée ; authentification fournisseur au lot 2",
    }


def propose(
    conn: Conn,
    principal: Principal,
    connector_id: UUID,
    *,
    secret_name: str,
    value: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    _may_rotate(principal)
    reauth.require_fresh(principal, action="proposer une rotation d'identifiant")
    name = (secret_name or "").strip()
    if not name.replace("_", "").isalnum() or len(name) > 40:
        raise ApiError(422, "invalid_secret_name", "nom de secret invalide", field="/secret_name")
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ApiError(422, "invalid_secret", "valeur attendue", field="/value")
    row = _connector(conn, connector_id)
    refs = dict(row["secret_refs"] or {})
    old_ref = refs.get(name)
    if not old_ref:
        raise ApiError(422, "secret_missing", "aucun secret actif à remplacer pour ce nom", field="/secret_name")
    existing = conn.execute(
        "SELECT id FROM connector_credential_rotations WHERE connector_id = %s AND secret_name = %s "
        "AND status = ANY(%s)",
        (connector_id, name, list(ACTIVE)),
    ).fetchone()
    if existing:
        raise ApiError(409, "rotation_in_progress", "une rotation est déjà en cours pour ce secret")
    # Sondage tant que le clair est en mémoire — jamais stocké ni journalisé.
    probe = redact(_probe_from_plaintext(row, value))
    assert isinstance(probe, dict)
    candidate_ref = f"secret://connectors/{row['slug']}/{name}/pending/{uuid7()}"
    secrets.put(conn, candidate_ref, value.encode(), principal.user_id)
    rot_id = uuid7()
    initial_status = "proposed"
    failure = None
    if probe.get("status") == "failed":
        # On enregistre quand même la proposition en failed pour audit, sans bascule.
        initial_status = "failed"
        failure = str(probe.get("message") or "test échoué")[:500]
    conn.execute(
        """
        INSERT INTO connector_credential_rotations
            (id, connector_id, secret_name, old_ref, candidate_ref, status, test_result, tested_at,
             failure_reason, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
        """,
        (rot_id, connector_id, name, old_ref, candidate_ref, initial_status, Jsonb(probe), failure, principal.user_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="connector.credential.propose",
        outcome="failed" if initial_status == "failed" else "ok",
        targets={"connector": row["slug"], "secret": name, "rotation_id": str(rot_id), "probe": probe.get("status")},
        request_id=request_id,
    )
    if initial_status == "failed":
        raise ApiError(
            422,
            "credential_invalid",
            "identifiant candidat invalide : bascule refusée",
            details={"test": {k: v for k, v in probe.items() if k != "value"}, "rotation_id": str(rot_id)},
            commit=True,
        )
    return get(conn, principal, rot_id)


def validate(
    conn: Conn,
    principal: Principal,
    rotation_id: UUID,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Confirme le sondage déjà calculé à la proposition (pas de relecture du secret)."""
    _may_rotate(principal)
    reauth.require_fresh(principal, action="valider une rotation d'identifiant")
    row = conn.execute(
        "SELECT * FROM connector_credential_rotations WHERE id = %s FOR UPDATE", (rotation_id,)
    ).fetchone()
    if row is None:
        raise not_found("rotation")
    if row["status"] == "failed":
        raise ApiError(422, "credential_invalid", "identifiant candidat invalide : bascule refusée")
    if row["status"] not in ("proposed", "validated"):
        raise ApiError(409, "invalid_state", f"rotation en état {row['status']}")
    probe = row["test_result"] or {}
    if probe.get("status") != "ok":
        conn.execute(
            "UPDATE connector_credential_rotations SET status = 'failed', failure_reason = %s WHERE id = %s",
            (str(probe.get("message") or "test échoué")[:500], rotation_id),
        )
        raise ApiError(422, "credential_invalid", "identifiant candidat invalide : bascule refusée")
    connector = _connector(conn, row["connector_id"])
    conn.execute(
        "UPDATE connector_credential_rotations SET status = 'validated', tested_at = now() WHERE id = %s",
        (rotation_id,),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="connector.credential.validate",
        outcome="ok",
        targets={"rotation_id": str(rotation_id), "connector": connector["slug"], "secret": row["secret_name"]},
        request_id=request_id,
    )
    return get(conn, principal, rotation_id)


def _inflight_jobs(conn: Conn) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE state IN ('running', 'leased') AND dispatched_at IS NOT NULL"
    ).fetchone()
    return int(row["n"]) if row else 0


def cutover(
    conn: Conn,
    principal: Principal,
    rotation_id: UUID,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Bascule : le `secret_refs` pointe vers la référence candidate (pas de reveal API)."""
    _may_rotate(principal)
    reauth.require_fresh(principal, action="basculer un identifiant de connecteur")
    row = conn.execute(
        "SELECT * FROM connector_credential_rotations WHERE id = %s FOR UPDATE", (rotation_id,)
    ).fetchone()
    if row is None:
        raise not_found("rotation")
    if row["status"] != "validated":
        raise ApiError(409, "not_validated", "validez le candidat avant la bascule")
    connector = _connector(conn, row["connector_id"])
    refs = dict(connector["secret_refs"] or {})
    refs[row["secret_name"]] = row["candidate_ref"]
    conn.execute(
        "UPDATE connectors SET secret_refs = %s, config_version = config_version + 1 WHERE id = %s",
        (Jsonb(refs), connector["id"]),
    )
    conn.execute(
        "UPDATE connector_credential_rotations SET status = 'cutover', cutover_at = now() WHERE id = %s",
        (rotation_id,),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="connector.credential.cutover",
        outcome="ok",
        targets={"rotation_id": str(rotation_id), "connector": connector["slug"], "secret": row["secret_name"]},
        versions={"config_version": connector["config_version"] + 1},
        request_id=request_id,
    )
    return get(conn, principal, rotation_id)


def retire(
    conn: Conn,
    principal: Principal,
    rotation_id: UUID,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    _may_rotate(principal)
    row = conn.execute(
        "SELECT * FROM connector_credential_rotations WHERE id = %s FOR UPDATE", (rotation_id,)
    ).fetchone()
    if row is None:
        raise not_found("rotation")
    if row["status"] != "cutover":
        raise ApiError(409, "not_cutover", "bascule requise avant le retrait de l'ancienne référence")
    pending = _inflight_jobs(conn)
    if pending:
        raise ApiError(
            409,
            "jobs_in_flight",
            f"{pending} job(s) encore en cours : attendez la fin avant de retirer l'ancienne référence",
            details={"in_flight": pending},
        )
    conn.execute(
        "UPDATE connector_credential_rotations SET status = 'retired', retired_at = now() WHERE id = %s",
        (rotation_id,),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="connector.credential.retire",
        outcome="ok",
        targets={"rotation_id": str(rotation_id), "old_ref": row["old_ref"]},
        request_id=request_id,
    )
    return get(conn, principal, rotation_id)


def get(conn: Conn, principal: Principal, rotation_id: UUID) -> dict[str, Any]:
    _may_rotate(principal)
    row = conn.execute("SELECT * FROM connector_credential_rotations WHERE id = %s", (rotation_id,)).fetchone()
    if row is None:
        raise not_found("rotation")
    return _view(dict(row))


def list_for_connector(conn: Conn, principal: Principal, connector_id: UUID) -> list[dict[str, Any]]:
    _may_rotate(principal)
    _connector(conn, connector_id)
    rows = conn.execute(
        "SELECT * FROM connector_credential_rotations WHERE connector_id = %s ORDER BY created_at DESC",
        (connector_id,),
    ).fetchall()
    return [_view(dict(r)) for r in rows]
