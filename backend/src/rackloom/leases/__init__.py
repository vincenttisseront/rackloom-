"""F06 : baux pour environnements temporaires.

Durée de vie capturée (`lifetime_until`) → bail avec propriétaire, rappels et
renouvellement. À l'expiration : signalement + proposition d'un plan de retrait.
Rien n'est supprimé sans workflow et approbation ; l'exécution locale est simulée
(pas de destroy Terraform — labo L2 pour un retrait réel).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from .. import audit, clock, events
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal, load_bindings, require_project, require_role
from ..ids import uuid7
from ..redact import redact

REMINDER_DAYS = 3
MAX_TTL_DAYS = 30  # D14 : plafond de politique (dev) et de renouvellement
STATE_LABELS = {
    "active": "Actif",
    "reminded": "Rappel envoyé",
    "expired": "Expiré",
    "retirement_proposed": "Retrait proposé",
    "closed": "Clos",
}
OPEN_STATES = ("active", "reminded", "expired", "retirement_proposed")


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def _today() -> date:
    return clock.now().date()


def _parse_day(raw: Any, field: str) -> date:
    if not isinstance(raw, str) or not raw:
        raise ApiError(422, "invalid_date", "date AAAA-MM-JJ attendue", field=f"/{field}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(422, "invalid_date", "date AAAA-MM-JJ attendue", field=f"/{field}") from exc


def _max_days_for_env(conn: Conn, environment_id: UUID) -> int:
    row = conn.execute(
        """
        SELECT rules FROM environment_policies
        WHERE environment_id = %s ORDER BY version DESC LIMIT 1
        """,
        (environment_id,),
    ).fetchone()
    if row is None:
        return MAX_TTL_DAYS
    lifetime = (row["rules"] or {}).get("lifetime") or {}
    raw = lifetime.get("max_days")
    if isinstance(raw, int) and raw > 0:
        return min(raw, MAX_TTL_DAYS)
    return MAX_TTL_DAYS


def _owner_principal(conn: Conn, user_id: UUID) -> Principal:
    user = conn.execute(
        "SELECT id, email, display_name FROM users WHERE id = %s AND status = 'active'",
        (user_id,),
    ).fetchone()
    if user is None:
        raise not_found("propriétaire")
    return Principal(
        user_id=user["id"],
        email=user["email"],
        display_name=user["display_name"],
        session_id=uuid4(),
        auth_method="system",
        auth_time=clock.now(),
        bindings=load_bindings(conn, user["id"]),
    )


def view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    today = _today()
    expires = row["expires_on"]
    return {
        "id": str(row["id"]),
        "deployment_id": str(row["deployment_id"]),
        "project_id": str(row["project_id"]),
        "environment_id": str(row["environment_id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "expires_on": expires.isoformat(),
        "reminder_on": row["reminder_on"].isoformat(),
        "state": row["state"],
        "state_label": STATE_LABELS.get(row["state"], row["state"]),
        "expired": row["state"] in ("expired", "retirement_proposed") or expires < today,
        "days_remaining": (expires - today).days,
        "reminded_at": row["reminded_at"].isoformat() if row.get("reminded_at") else None,
        "expired_at": row["expired_at"].isoformat() if row.get("expired_at") else None,
        "retirement_change_id": str(row["retirement_change_id"]) if row.get("retirement_change_id") else None,
        "closed_at": row["closed_at"].isoformat() if row.get("closed_at") else None,
        "close_reason": row.get("close_reason"),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "notice": (
            "Un labo expiré est signalé mais reste intact tant que le retrait n'a pas été "
            "approuvé et exécuté. Aucune suppression automatique."
        ),
    }


def get_by_deployment(conn: Conn, deployment_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM deployment_leases WHERE deployment_id = %s",
        (deployment_id,),
    ).fetchone()


def ensure_for_existing(conn: Conn, dep: dict[str, Any]) -> dict[str, Any] | None:
    """Ouvre un bail à partir de la révision créatrice si la date de fin y était capturée."""
    existing = get_by_deployment(conn, dep["id"])
    if existing is not None:
        return existing
    change_id = dep.get("created_by_change_id")
    if not change_id:
        return None
    rev = conn.execute(
        """
        SELECT resolved FROM request_revisions
        WHERE change_id = %s AND outcome = 'resolved'
        ORDER BY revision_no DESC LIMIT 1
        """,
        (change_id,),
    ).fetchone()
    if rev is None:
        return None
    return open_from_resolution(conn, deployment=dep, resolution=rev["resolved"] or {})


def for_deployment(conn: Conn, principal: Principal, deployment_id: UUID) -> dict[str, Any] | None:
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found("déploiement")
    require_project(principal, dep["project_id"], "déploiement")
    # Backfill lecture seule : n'ouvre un bail que si déjà présent (écriture = renew / submit).
    return view(get_by_deployment(conn, deployment_id))


def open_from_resolution(
    conn: Conn,
    *,
    deployment: dict[str, Any],
    resolution: dict[str, Any],
    actor_type: str = "system",
    actor_id: str | None = None,
) -> dict[str, Any] | None:
    """Ouvre un bail si `lifetime_until` est présent. Idempotent."""
    existing = get_by_deployment(conn, deployment["id"])
    if existing is not None:
        return existing
    raw = (resolution.get("inputs") or {}).get("lifetime_until")
    if not raw:
        return None
    expires_on = date.fromisoformat(str(raw))
    today = _today()
    if expires_on <= today:
        # Déjà capturée mais dans le passé : bail expiré immédiatement, sweep proposera le retrait.
        expires_on = today
    reminder_on = max(today, expires_on - timedelta(days=REMINDER_DAYS))
    row = conn.execute(
        """
        INSERT INTO deployment_leases
            (id, deployment_id, project_id, environment_id, owner_user_id,
             expires_on, reminder_on, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
        RETURNING *
        """,
        (
            uuid7(),
            deployment["id"],
            deployment["project_id"],
            deployment["environment_id"],
            deployment["owner_id"],
            expires_on,
            reminder_on,
        ),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="lease.open",
        outcome="ok",
        project_id=deployment["project_id"],
        targets={"lease": str(row["id"]), "deployment": str(deployment["id"])},
        policy_decision={"expires_on": expires_on.isoformat(), "reminder_on": reminder_on.isoformat()},
    )
    events.emit(
        conn,
        type="lease.opened",
        source="system",
        project_id=deployment["project_id"],
        correlation_id=deployment.get("created_by_change_id"),
        data={
            "lease_id": str(row["id"]),
            "deployment_id": str(deployment["id"]),
            "expires_on": expires_on.isoformat(),
            "message": f"Bail ouvert jusqu'au {expires_on.isoformat()}",
        },
        dedupe_key=f"lease.opened:{row['id']}",
    )
    return row


def renew(
    conn: Conn,
    principal: Principal,
    deployment_id: UUID,
    body: dict[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Prolonge un bail (actif, rappelé ou expiré non clos). Annule un plan de retrait non exécuté."""
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s FOR UPDATE", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found("déploiement")
    require_project(principal, dep["project_id"], "déploiement")
    require_role(principal, "operator", dep["project_id"], dep["environment_id"])
    lease = conn.execute(
        "SELECT * FROM deployment_leases WHERE deployment_id = %s FOR UPDATE",
        (deployment_id,),
    ).fetchone()
    if lease is None:
        raise not_found("bail")
    if lease["state"] == "closed":
        raise ApiError(409, "lease_closed", "bail déjà clos")
    reason = _reason(body.get("reason"))
    today = _today()
    max_days = _max_days_for_env(conn, dep["environment_id"])
    if "expires_on" in body and body.get("expires_on") is not None:
        new_expires = _parse_day(body.get("expires_on"), "expires_on")
    elif "days" in body and body.get("days") is not None:
        try:
            days = int(body["days"])
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_days", "nombre de jours entier attendu", field="/days") from exc
        if days < 1:
            raise ApiError(422, "invalid_days", "au moins un jour", field="/days")
        new_expires = today + timedelta(days=days)
    else:
        raise ApiError(422, "expires_required", "expires_on ou days requis", field="/expires_on")
    if new_expires <= today:
        raise ApiError(422, "invalid_expiry", "la nouvelle date de fin doit être dans le futur", field="/expires_on")
    if new_expires > today + timedelta(days=max_days):
        raise ApiError(
            422,
            "ttl_exceeded",
            f"durée maximale {max_days} jours (D14)",
            field="/expires_on",
        )
    if new_expires <= lease["expires_on"] and lease["state"] in ("active", "reminded"):
        raise ApiError(422, "not_extended", "le renouvellement doit prolonger la date de fin", field="/expires_on")

    previous = lease["expires_on"]
    reminder_on = max(today, new_expires - timedelta(days=REMINDER_DAYS))
    # Annuler un plan de retrait encore non exécuté.
    if lease.get("retirement_change_id"):
        ch = conn.execute(
            "SELECT * FROM changes WHERE id = %s FOR UPDATE",
            (lease["retirement_change_id"],),
        ).fetchone()
        if ch is not None and ch["state"] in ("draft", "needs_info", "plan_ready", "planning", "validated"):
            from ..changes.transitions import transition

            transition(
                conn,
                ch,
                "cancelled",
                reason_code="lease_renewed",
                actor_type="user",
                actor_id=str(principal.user_id),
                detail={"message": "Retrait annulé : bail renouvelé"},
                request_id=request_id,
            )

    row = conn.execute(
        """
        UPDATE deployment_leases SET
            expires_on = %s,
            reminder_on = %s,
            state = 'active',
            reminded_at = NULL,
            expired_at = NULL,
            retirement_change_id = NULL,
            updated_at = now()
        WHERE id = %s
        RETURNING *
        """,
        (new_expires, reminder_on, lease["id"]),
    ).fetchone()
    assert row is not None
    conn.execute(
        """
        INSERT INTO deployment_lease_renewals
            (id, lease_id, previous_expires_on, new_expires_on, renewed_by, reason)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (uuid7(), lease["id"], previous, new_expires, principal.user_id, reason),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="lease.renew",
        outcome="ok",
        project_id=dep["project_id"],
        targets={"lease": str(lease["id"]), "deployment": str(deployment_id)},
        policy_decision={
            "previous_expires_on": previous.isoformat(),
            "new_expires_on": new_expires.isoformat(),
            "reason": reason,
        },
        request_id=request_id,
    )
    events.emit(
        conn,
        type="lease.renewed",
        source="system",
        project_id=dep["project_id"],
        correlation_id=dep.get("created_by_change_id"),
        data={
            "lease_id": str(lease["id"]),
            "deployment_id": str(deployment_id),
            "expires_on": new_expires.isoformat(),
            "by": principal.display_name,
            "message": f"Bail renouvelé jusqu'au {new_expires.isoformat()}",
        },
        dedupe_key=f"lease.renewed:{lease['id']}:{new_expires.isoformat()}",
    )
    return view(row)  # type: ignore[return-value]


def propose_retirement(
    conn: Conn,
    principal: Principal | None,
    deployment_id: UUID,
    *,
    reason: str = "Bail expiré : plan de retrait proposé",
    request_id: str | None = None,
    auto: bool = False,
) -> dict[str, Any]:
    """Crée une demande `retire` avec plan simulé prêt à approuver. Aucune écriture fournisseur."""
    from .. import artifacts
    from ..canonical import digest as canon_digest
    from ..changes import service
    from ..changes.transitions import EDITABLE, transition
    from ..executions import bundle as bundles

    dep = conn.execute("SELECT * FROM deployments WHERE id = %s FOR UPDATE", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found("déploiement")
    lease = conn.execute(
        "SELECT * FROM deployment_leases WHERE deployment_id = %s FOR UPDATE",
        (deployment_id,),
    ).fetchone()
    if lease is None:
        raise not_found("bail")
    if lease["state"] == "closed":
        raise ApiError(409, "lease_closed", "bail déjà clos")
    if lease["state"] == "retirement_proposed" and lease.get("retirement_change_id"):
        existing = conn.execute("SELECT * FROM changes WHERE id = %s", (lease["retirement_change_id"],)).fetchone()
        if existing is not None and existing["state"] not in ("cancelled", "rejected", "failed"):
            return {
                "lease": view(lease),
                "change_id": str(existing["id"]),
                "already_proposed": True,
                "notice": "Un plan de retrait est déjà proposé ; les ressources restent intactes.",
            }

    actor = principal
    if actor is None:
        actor = _owner_principal(conn, lease["owner_user_id"])
    else:
        require_project(actor, dep["project_id"], "déploiement")
        require_role(actor, "operator", dep["project_id"], dep["environment_id"])

    busy_states = (service.ACTIVE_STATES | EDITABLE) - {"rejected", "plan_failed", "cancelled"}
    busy = conn.execute(
        "SELECT id FROM changes WHERE deployment_id = %s AND state = ANY(%s) AND kind <> 'retire'",
        (deployment_id, list(busy_states)),
    ).fetchone()
    if busy and not auto:
        raise ApiError(
            409,
            "change_in_progress",
            "un changement est déjà en cours sur ce déploiement",
            details={"change_id": str(busy["id"])},
        )

    env = conn.execute("SELECT * FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    assert env is not None
    title = f"Retrait du labo {dep['template_slug']} (bail expiré)"[:120]
    row = conn.execute(
        "INSERT INTO changes (id, kind, deployment_id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'retire', %s, %s, %s, %s, 'draft', %s) RETURNING *",
        (uuid7(), deployment_id, dep["project_id"], dep["environment_id"], title, actor.user_id),
    ).fetchone()
    assert row is not None
    service._created(conn, actor, row, request_id)
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env["slug"],
        "inputs": {
            "quantity": max(1, len(dep.get("instances") or []) or 1),
            "size_tier": next(
                (i.get("size_tier") for i in (dep.get("instances") or []) if i.get("size_tier")),
                "test_leger",
            ),
            "usage": reason[:200],
            "lifetime_until": (_today() + timedelta(days=1)).isoformat(),
        },
        "confirm_production": bool(env["is_production"]),
    }
    service.add_version(
        conn,
        actor,
        row["id"],
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )
    change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (row["id"],)).fetchone()
    assert change is not None
    revision = service.current_revision(conn, change)
    assert revision is not None and revision["outcome"] == "resolved"

    # Plan simulé prêt à approuver (pas de Terraform). Les VM restent intactes jusqu'à exécution.
    names = [i["name"] for i in (dep.get("instances") or []) if i.get("name")]
    plan_bytes = b'{"rackloom":"retire-plan","simulated":true,"destructive_intent":true}'
    summary = {
        "simulated": True,
        "retirement": True,
        "destructive": False,
        "no_changes": False,
        "terraform_version": None,
        "vm_counts": {"create": 0, "update": 0, "delete": len(names), "replace": 0},
        "vms": [{"name": n, "action": "retire", "attributes": {}} for n in names],
        "message": (
            "Plan de retrait proposé — aucune suppression réelle tant que non approuvé et exécuté. "
            "Exécution locale simulée (pas de destroy Terraform)."
        ),
        "interruption": True,
        "hosts": names,
    }
    actor_type = "system" if auto else "user"
    actor_id = None if auto else str(actor.user_id)
    if change["state"] == "draft":
        change = transition(
            conn,
            change,
            "validated",
            reason_code="retire_submitted",
            actor_type=actor_type,
            actor_id=actor_id,
            detail={"revision_no": revision["revision_no"], "lease_id": str(lease["id"])},
            request_id=request_id,
        )
    change = transition(
        conn,
        change,
        "planning",
        reason_code="retire_plan",
        actor_type=actor_type,
        actor_id=actor_id,
        detail={"revision_no": revision["revision_no"], "lease_id": str(lease["id"])},
        request_id=request_id,
    )
    art = artifacts.store(
        conn,
        project_id=change["project_id"],
        change_id=change["id"],
        kind="tf_plan_bin",
        key_class="A",
        plaintext=plan_bytes,
    )
    valid_until = datetime.now(UTC) + timedelta(seconds=env["plan_ttl_seconds"])
    version = service.current_version(conn, change)
    assert version is not None
    stack = conn.execute("SELECT * FROM stacks WHERE id = %s", (dep["stack_id"],)).fetchone()
    assert stack is not None
    bundle = bundles.build(
        conn,
        change=change,
        version=version,
        revision=revision,
        stack=stack,
        plan_sha256=art["sha256"],
        state_lineage=None,
        state_serial=None,
        valid_until=valid_until.isoformat(),
        terraform_version=None,
    )
    plan_id = uuid7()
    conn.execute(
        """
        INSERT INTO plans (id, change_id, version_no, revision_no, stack_id, job_id, status, plan_sha256,
                           artifact_id, summary, counts, valid_until, bundle, bundle_digest, state_lineage,
                           state_serial)
        VALUES (%s, %s, %s, %s, %s, NULL, 'ready', %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
        """,
        (
            plan_id,
            change["id"],
            revision["version_no"],
            revision["revision_no"],
            dep["stack_id"],
            art["sha256"],
            art["id"],
            Jsonb(summary),
            Jsonb(summary["vm_counts"]),
            valid_until,
            Jsonb(bundle),
            canon_digest(bundle),
        ),
    )
    change = transition(
        conn,
        change,
        "plan_ready",
        reason_code="retire_plan_ready",
        actor_type=actor_type,
        actor_id=actor_id,
        detail={"plan_id": str(plan_id), "simulated": True, "lease_id": str(lease["id"])},
        request_id=request_id,
    )
    updated = conn.execute(
        """
        UPDATE deployment_leases SET
            state = 'retirement_proposed',
            retirement_change_id = %s,
            expired_at = coalesce(expired_at, now()),
            updated_at = now()
        WHERE id = %s
        RETURNING *
        """,
        (change["id"], lease["id"]),
    ).fetchone()
    assert updated is not None
    audit.record(
        conn,
        actor_type="system" if auto else "user",
        actor_id=None if auto else str(actor.user_id),
        action="lease.retirement_proposed",
        outcome="ok",
        project_id=dep["project_id"],
        correlation_id=change["id"],
        targets={"lease": str(lease["id"]), "deployment": str(deployment_id), "change": str(change["id"])},
        policy_decision={"reason": reason, "auto": auto},
        request_id=request_id,
    )
    events.emit(
        conn,
        type="lease.retirement_proposed",
        source="system",
        project_id=dep["project_id"],
        correlation_id=change["id"],
        data={
            "lease_id": str(lease["id"]),
            "deployment_id": str(deployment_id),
            "change_id": str(change["id"]),
            "auto": auto,
            "message": "Plan de retrait proposé — ressources intactes jusqu'à approbation et exécution",
        },
        severity="warning",
        dedupe_key=f"lease.retirement_proposed:{lease['id']}:{change['id']}",
    )
    # Vérifier que les instances existent encore (recette : labo intact).
    still = conn.execute(
        "SELECT count(*) AS n FROM deployments WHERE id = %s AND status <> 'abandoned'",
        (deployment_id,),
    ).fetchone()
    assert still is not None and still["n"] == 1
    return {
        "lease": view(updated),
        "change_id": str(change["id"]),
        "plan_id": str(plan_id),
        "already_proposed": False,
        "notice": "Plan de retrait proposé. Le labo reste intact tant que le retrait n'est pas approuvé et exécuté.",
    }


def mark_reminded(conn: Conn, lease: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE deployment_leases SET state = 'reminded', reminded_at = now(), updated_at = now()
        WHERE id = %s AND state = 'active'
        RETURNING *
        """,
        (lease["id"],),
    ).fetchone()
    if row is None:
        return lease
    audit.record(
        conn,
        actor_type="system",
        actor_id=None,
        action="lease.reminded",
        outcome="ok",
        project_id=lease["project_id"],
        targets={"lease": str(lease["id"]), "deployment": str(lease["deployment_id"])},
        policy_decision={"expires_on": lease["expires_on"].isoformat()},
    )
    events.emit(
        conn,
        type="lease.reminded",
        source="system",
        project_id=lease["project_id"],
        data={
            "lease_id": str(lease["id"]),
            "deployment_id": str(lease["deployment_id"]),
            "expires_on": lease["expires_on"].isoformat(),
            "message": f"Rappel : le bail expire le {lease['expires_on'].isoformat()}",
        },
        severity="warning",
        dedupe_key=f"lease.reminded:{lease['id']}:{lease['expires_on'].isoformat()}",
    )
    return row


def mark_expired(conn: Conn, lease: dict[str, Any]) -> dict[str, Any]:
    """Signale l'expiration sans aucune écriture fournisseur."""
    row = conn.execute(
        """
        UPDATE deployment_leases SET state = 'expired', expired_at = now(), updated_at = now()
        WHERE id = %s AND state IN ('active', 'reminded')
        RETURNING *
        """,
        (lease["id"],),
    ).fetchone()
    if row is None:
        return lease
    # Preuve : le déploiement et les hôtes restent intacts.
    dep = conn.execute(
        "SELECT status, instances FROM deployments WHERE id = %s",
        (lease["deployment_id"],),
    ).fetchone()
    host_row = conn.execute(
        "SELECT count(*) AS n FROM hosts WHERE deployment_id = %s",
        (lease["deployment_id"],),
    ).fetchone()
    host_count = int(host_row["n"]) if host_row else 0
    audit.record(
        conn,
        actor_type="system",
        actor_id=None,
        action="lease.expired",
        outcome="ok",
        project_id=lease["project_id"],
        targets={"lease": str(lease["id"]), "deployment": str(lease["deployment_id"])},
        policy_decision={
            "expires_on": lease["expires_on"].isoformat(),
            "deployment_status": dep["status"] if dep else None,
            "host_count": host_count,
            "instances_intact": True,
        },
    )
    events.emit(
        conn,
        type="lease.expired",
        source="system",
        project_id=lease["project_id"],
        data={
            "lease_id": str(lease["id"]),
            "deployment_id": str(lease["deployment_id"]),
            "message": "Bail expiré — labo signalé, ressources intactes ; plan de retrait à proposer",
            "instances_intact": True,
        },
        severity="warning",
        dedupe_key=f"lease.expired:{lease['id']}",
    )
    return row


def close_after_retirement(
    conn: Conn,
    *,
    lease_id: UUID,
    change_id: UUID,
    reason: str = "Retrait exécuté (simulé) — aucune suppression réelle de VM",
) -> dict[str, Any]:
    """Clôture le bail après exécution approuvée. Ne détruit aucune ressource fournisseur."""
    text = _reason(reason, "close_reason")
    lease = conn.execute(
        "SELECT * FROM deployment_leases WHERE id = %s FOR UPDATE",
        (lease_id,),
    ).fetchone()
    if lease is None:
        raise not_found("bail")
    row = conn.execute(
        """
        UPDATE deployment_leases SET
            state = 'closed',
            closed_at = now(),
            close_reason = %s,
            retirement_change_id = coalesce(retirement_change_id, %s),
            updated_at = now()
        WHERE id = %s
        RETURNING *
        """,
        (text, change_id, lease_id),
    ).fetchone()
    assert row is not None
    # Marquer le déploiement abandonné sans toucher au state Terraform ni aux fichiers labo.
    conn.execute(
        "UPDATE deployments SET status = 'abandoned' WHERE id = %s AND status <> 'abandoned'",
        (lease["deployment_id"],),
    )
    audit.record(
        conn,
        actor_type="system",
        actor_id=None,
        action="lease.closed",
        outcome="ok",
        project_id=lease["project_id"],
        correlation_id=change_id,
        targets={"lease": str(lease_id), "deployment": str(lease["deployment_id"])},
        policy_decision={"reason": text, "simulated": True, "no_provider_destroy": True},
    )
    events.emit(
        conn,
        type="lease.closed",
        source="system",
        project_id=lease["project_id"],
        correlation_id=change_id,
        data={
            "lease_id": str(lease_id),
            "deployment_id": str(lease["deployment_id"]),
            "message": text,
            "simulated": True,
        },
        dedupe_key=f"lease.closed:{lease_id}",
    )
    return row


def enqueue_retire(conn: Conn, change: dict[str, Any], *, approval_id: UUID | None = None) -> dict[str, Any]:
    from ..executions import queue

    return queue.enqueue(
        conn,
        kind="lease_retire",
        idempotency_key=f"lease_retire:{change['id']}:{change['current_revision_no']}",
        change=change,
        payload={"approval_id": str(approval_id) if approval_id else None, "simulated": True},
        priority=70,
    )


def sweep(conn: Conn, *, at: date | None = None) -> dict[str, int]:
    """Faucheuse : rappels, expiration (signalement), proposition de plan de retrait.

    Aucune écriture vers un fournisseur. Horloge via `clock` / `at`.
    """
    today = at or _today()
    reminded = expired = proposed = 0

    due_reminders = conn.execute(
        """
        SELECT * FROM deployment_leases
        WHERE state = 'active' AND reminder_on <= %s AND expires_on > %s
        ORDER BY reminder_on
        FOR UPDATE SKIP LOCKED
        """,
        (today, today),
    ).fetchall()
    for lease in due_reminders:
        mark_reminded(conn, lease)
        reminded += 1

    due_expire = conn.execute(
        """
        SELECT * FROM deployment_leases
        WHERE state IN ('active', 'reminded') AND expires_on <= %s
        ORDER BY expires_on
        FOR UPDATE SKIP LOCKED
        """,
        (today,),
    ).fetchall()
    for lease in due_expire:
        marked = mark_expired(conn, lease)
        expired += 1
        # Proposition automatique du plan de retrait (ressources intactes).
        try:
            propose_retirement(
                conn,
                None,
                marked["deployment_id"],
                reason="Bail expiré : plan de retrait proposé automatiquement",
                auto=True,
            )
            proposed += 1
        except ApiError as exc:
            # Un autre changement bloque : le labo reste signalé expiré, intact.
            if exc.code != "change_in_progress":
                raise

    return {"reminded": reminded, "expired": expired, "retirement_proposed": proposed}


def card_section(conn: Conn, principal: Principal, dep: dict[str, Any]) -> dict[str, Any] | None:
    """Bloc fiche service : état du bail, actions renouveler / proposer le retrait."""
    lease = get_by_deployment(conn, dep["id"])
    if lease is None:
        return None
    require_project(principal, dep["project_id"], "déploiement")
    can_operate = (
        principal.has_role("operator", dep["project_id"], dep["environment_id"]) or principal.is_platform_admin
    )
    can_renew = can_operate and lease["state"] in OPEN_STATES
    can_propose = can_operate and lease["state"] in ("active", "reminded", "expired")
    actions: list[dict[str, str]] = []
    if can_renew:
        actions.append({"id": "renew", "label": "Renouveler le bail"})
    if can_propose:
        actions.append({"id": "propose_retirement", "label": "Proposer le retrait"})
    owner_row = conn.execute("SELECT display_name FROM users WHERE id = %s", (lease["owner_user_id"],)).fetchone()
    base = view(lease)
    assert base is not None
    return {
        **base,
        "owner": owner_row["display_name"] if owner_row else None,
        "actions": actions,
        "can_renew": can_renew,
        "can_propose_retirement": can_propose,
    }
