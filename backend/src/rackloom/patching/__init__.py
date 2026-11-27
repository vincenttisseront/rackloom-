"""F19 : patch par vagues.

Sélection d'hôtes enrôlés, groupe pilote, contrôles avant/après chaque vague,
respect des fenêtres (F27 / R090) et des règles de redémarrage. Une dégradation
sur le pilote bloque la vague suivante et ne déclenche aucun retour arrière
non approuvé.

Sans laboratoire réel (D2) : application et redémarrage sont simulés.
Les contrôles s'appuient sur les observations `probe` (F18 enrichira les sondes).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from .. import audit, events, registry
from .. import hosts as hosts_mod
from ..changes import service
from ..changes.transitions import EDITABLE
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..executions import queue
from ..identity.authz import Principal, require_role
from ..ids import uuid7
from ..projects import calendars, get_project
from ..redact import redact

REBOOT_POLICIES = ("never", "if_required_in_window")
CAMPAIGN_STATES = (
    "draft",
    "plan_ready",
    "approved",
    "running",
    "wave_blocked",
    "succeeded",
    "failed",
    "cancelled",
)
HOST_STATES = (
    "pending",
    "pre_ok",
    "pre_failed",
    "patched",
    "post_ok",
    "degraded",
    "wave_blocked",
    "skipped",
)

STATE_LABELS = {
    "draft": "Brouillon",
    "plan_ready": "Plan prêt",
    "approved": "Approuvée",
    "running": "En cours",
    "wave_blocked": "Vague bloquée",
    "succeeded": "Réussie",
    "failed": "Échouée",
    "cancelled": "Annulée",
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _title(value: Any) -> str:
    text = redact(str(value or "").strip())
    if not 1 <= len(text) <= 120:
        raise ApiError(422, "invalid_title", "titre obligatoire (1 à 120 caractères)", field="/title")
    return text


def enrolled_hosts(
    conn: Conn, principal: Principal, project_slug: str, *, environment_slug: str | None = None
) -> list[dict[str, Any]]:
    """Machines enrôlées du projet (périmètre), candidates à un patch."""
    project = get_project(conn, principal, project_slug)
    params: list[Any] = [project["id"]]
    sql = (
        "SELECT h.* FROM hosts h WHERE h.project_id = %s AND h.enrolled_at IS NOT NULL "
        "AND h.deployment_state NOT IN ('out_of_scope', 'created')"
    )
    if environment_slug:
        env = conn.execute(
            "SELECT id FROM environments WHERE project_id = %s AND slug = %s",
            (project["id"], environment_slug),
        ).fetchone()
        if env is None:
            raise not_found("environnement")
        sql += " AND h.environment_id = %s"
        params.append(env["id"])
    sql += " ORDER BY h.name"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": str(h["id"]),
            "name": h["name"],
            "environment_id": str(h["environment_id"]),
            "deployment_id": str(h["deployment_id"]),
            "deployment_state": h["deployment_state"],
            "enrolled_at": _iso(h["enrolled_at"]),
            "health": hosts_mod.health(conn, h),
        }
        for h in rows
    ]


def _get_campaign(conn: Conn, principal: Principal, campaign_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM patch_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("campagne")
    return row


def _host_rows(conn: Conn, campaign_id: UUID) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT * FROM patch_campaign_hosts WHERE campaign_id = %s ORDER BY wave_index, host_name",
            (campaign_id,),
        ).fetchall()
    )


def view(conn: Conn, campaign: dict[str, Any]) -> dict[str, Any]:
    hosts = _host_rows(conn, campaign["id"])
    return {
        "id": str(campaign["id"]),
        "project_id": str(campaign["project_id"]),
        "environment_id": str(campaign["environment_id"]),
        "deployment_id": str(campaign["deployment_id"]),
        "change_id": str(campaign["change_id"]) if campaign.get("change_id") else None,
        "title": campaign["title"],
        "state": campaign["state"],
        "state_label": STATE_LABELS.get(campaign["state"], campaign["state"]),
        "waves": campaign["waves"] or [],
        "current_wave": campaign["current_wave"],
        "pilot_size": campaign["pilot_size"],
        "wave_size": campaign["wave_size"],
        "stop_on_failure": campaign["stop_on_failure"],
        "reboot_policy": campaign["reboot_policy"],
        "package_selector": campaign["package_selector"] or {},
        "block_reason": campaign.get("block_reason"),
        "simulated": bool(campaign.get("simulated", True)),
        "created_at": _iso(campaign["created_at"]),
        "updated_at": _iso(campaign["updated_at"]),
        "hosts": [
            {
                "host_id": str(h["host_id"]),
                "host_name": h["host_name"],
                "wave_index": h["wave_index"],
                "state": h["state"],
                "pre_check": h.get("pre_check"),
                "post_check": h.get("post_check"),
                "reboot_required": h["reboot_required"],
                "reboot_done": h["reboot_done"],
                "write_attempted": h["write_attempted"],
            }
            for h in hosts
        ],
        "notice": (
            "Patch simulé (D2) : aucune écriture réelle sur le parc. "
            "Une dégradation du pilote bloque la suite sans retour arrière automatique."
        ),
    }


def list_campaigns(conn: Conn, principal: Principal, project_slug: str) -> list[dict[str, Any]]:
    project = get_project(conn, principal, project_slug)
    rows = conn.execute(
        "SELECT * FROM patch_campaigns WHERE project_id = %s ORDER BY created_at DESC",
        (project["id"],),
    ).fetchall()
    return [view(conn, r) for r in rows]


def create_campaign(
    conn: Conn,
    principal: Principal,
    project_slug: str,
    body: dict[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Crée une campagne + demande `kind=patch` (plan / approbation avant écriture)."""
    project = get_project(conn, principal, project_slug)
    host_ids_raw = body.get("host_ids")
    if not isinstance(host_ids_raw, list) or not host_ids_raw:
        raise ApiError(422, "hosts_required", "sélectionnez au moins une machine enrôlée", field="/host_ids")
    try:
        host_ids = [UUID(str(x)) for x in host_ids_raw]
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid_host_id", "identifiant d'hôte invalide", field="/host_ids") from exc
    if len(set(host_ids)) != len(host_ids):
        raise ApiError(422, "duplicate_hosts", "hôtes en double refusés", field="/host_ids")

    rows = list(
        conn.execute(
            "SELECT * FROM hosts WHERE id = ANY(%s) AND project_id = %s ORDER BY name",
            (host_ids, project["id"]),
        ).fetchall()
    )
    if len(rows) != len(host_ids):
        raise ApiError(404, "host_not_found", "une ou plusieurs machines sont hors périmètre", field="/host_ids")
    for h in rows:
        if h["enrolled_at"] is None:
            raise ApiError(
                422,
                "host_not_enrolled",
                f"{h['name']} n'est pas enrôlée",
                field="/host_ids",
            )
        if h["deployment_state"] in ("out_of_scope", "created"):
            raise ApiError(
                422,
                "host_not_ready",
                f"{h['name']} n'est pas prête pour un patch",
                field="/host_ids",
            )
    env_ids = {h["environment_id"] for h in rows}
    dep_ids = {h["deployment_id"] for h in rows}
    if len(env_ids) != 1 or len(dep_ids) != 1:
        raise ApiError(
            422,
            "mixed_scope",
            "toutes les machines doivent appartenir au même environnement et déploiement",
            field="/host_ids",
        )
    environment_id = rows[0]["environment_id"]
    deployment_id = rows[0]["deployment_id"]
    require_role(principal, "operator", project["id"], environment_id)

    pilot_size = body.get("pilot_size", 1)
    wave_size = body.get("wave_size", 1)
    if not isinstance(pilot_size, int) or isinstance(pilot_size, bool) or not 1 <= pilot_size <= 3:
        raise ApiError(422, "invalid_pilot_size", "pilot_size entre 1 et 3", field="/pilot_size")
    if not isinstance(wave_size, int) or isinstance(wave_size, bool) or not 1 <= wave_size <= 10:
        raise ApiError(422, "invalid_wave_size", "wave_size entre 1 et 10", field="/wave_size")
    reboot_policy = body.get("reboot_policy", "if_required_in_window")
    if reboot_policy not in REBOOT_POLICIES:
        raise ApiError(
            422,
            "invalid_reboot_policy",
            f"reboot_policy attendu : {', '.join(REBOOT_POLICIES)}",
            field="/reboot_policy",
        )

    names = [h["name"] for h in rows]
    waves_spec = {"mode": "pilot", "pilot_size": pilot_size, "wave_size": wave_size, "stop_on_failure": True}
    registry.check_waves(waves_spec)
    waves = registry.plan_waves(waves_spec, names)
    title = _title(body.get("title") or f"Patch {', '.join(names[:3])}" + ("…" if len(names) > 3 else ""))

    busy_states = (service.ACTIVE_STATES | EDITABLE) - {"rejected", "plan_failed"}
    busy = conn.execute(
        "SELECT id FROM changes WHERE deployment_id = %s AND state = ANY(%s)",
        (deployment_id, list(busy_states)),
    ).fetchone()
    if busy:
        raise ApiError(
            409,
            "change_in_progress",
            "un changement est déjà en cours sur ce déploiement",
            details={"change_id": str(busy["id"])},
        )

    dep = service.get_deployment(conn, principal, deployment_id, for_update=True)
    change_id = uuid7()
    campaign_id = uuid7()
    row = conn.execute(
        "INSERT INTO changes (id, kind, deployment_id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'patch', %s, %s, %s, %s, 'draft', %s) RETURNING *",
        (change_id, deployment_id, project["id"], environment_id, title, principal.user_id),
    ).fetchone()
    service._created(conn, principal, row, request_id)

    env = conn.execute("SELECT slug, is_production FROM environments WHERE id = %s", (environment_id,)).fetchone()
    current_tier = next(
        (i.get("size_tier") for i in (dep.get("instances") or []) if i.get("size_tier")),
        "test_leger",
    )
    # Le besoin ne sert qu'au circuit plan/approbation ; les cibles sont dans la campagne.
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env["slug"],
        "inputs": {
            "quantity": 1,
            "size_tier": current_tier,
            "usage": "Campagne de patch par vagues",
            "lifetime_until": (date.today() + timedelta(days=14)).isoformat(),
        },
        "confirm_production": bool(env["is_production"]),
    }
    service.add_version(
        conn,
        principal,
        change_id,
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )

    campaign = conn.execute(
        """
        INSERT INTO patch_campaigns (
            id, project_id, environment_id, deployment_id, change_id, title, state, waves,
            current_wave, pilot_size, wave_size, stop_on_failure, reboot_policy,
            package_selector, simulated, created_by
        ) VALUES (
            %s, %s, %s, %s, %s, %s, 'draft', %s, 0, %s, %s, true, %s, %s, true, %s
        ) RETURNING *
        """,
        (
            campaign_id,
            project["id"],
            environment_id,
            deployment_id,
            change_id,
            title,
            Jsonb(waves),
            pilot_size,
            wave_size,
            reboot_policy,
            Jsonb(body.get("package_selector") or {"security": True}),
            principal.user_id,
        ),
    ).fetchone()

    name_to_host = {h["name"]: h for h in rows}
    for wi, wave in enumerate(waves):
        for name in wave:
            h = name_to_host[name]
            conn.execute(
                """
                INSERT INTO patch_campaign_hosts (
                    campaign_id, host_id, host_name, wave_index, state
                ) VALUES (%s, %s, %s, %s, 'pending')
                """,
                (campaign_id, h["id"], h["name"], wi),
            )

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="patch.campaign_created",
        outcome="ok",
        project_id=project["id"],
        correlation_id=change_id,
        targets={"campaign": str(campaign_id), "change": str(change_id), "hosts": names},
        request_id=request_id,
    )
    events.emit(
        conn,
        type="patch.campaign_created",
        source="system",
        project_id=project["id"],
        environment_id=environment_id,
        correlation_id=change_id,
        data={
            "campaign_id": str(campaign_id),
            "waves": waves,
            "message": f"Campagne de patch : pilote puis {len(waves)} vague(s)",
        },
        dedupe_key=f"patch.campaign_created:{campaign_id}",
    )
    return view(conn, campaign)


def get_campaign(conn: Conn, principal: Principal, campaign_id: UUID) -> dict[str, Any]:
    return view(conn, _get_campaign(conn, principal, campaign_id))


def campaign_for_change(conn: Conn, change_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM patch_campaigns WHERE change_id = %s", (change_id,)).fetchone()


def mark_plan_ready(conn: Conn, change_id: UUID) -> None:
    conn.execute(
        "UPDATE patch_campaigns SET state = 'plan_ready', updated_at = now() WHERE change_id = %s",
        (change_id,),
    )


def enqueue_patch(conn: Conn, change: dict[str, Any], *, approval_id: UUID | None = None) -> dict[str, Any]:
    """Après approbation : démarre la vague 0 (contrôles + patch simulé)."""
    campaign = campaign_for_change(conn, change["id"])
    if campaign is None:
        raise ApiError(409, "campaign_missing", "aucune campagne de patch liée à ce changement")
    conn.execute(
        "UPDATE patch_campaigns SET state = 'approved', updated_at = now() WHERE id = %s",
        (campaign["id"],),
    )
    run_after = calendars.schedule_run_after(conn, change["environment_id"])
    return queue.enqueue(
        conn,
        kind="host_patch",
        idempotency_key=f"host_patch:{change['id']}:w0",
        change=change,
        payload={
            "campaign_id": str(campaign["id"]),
            "wave": 0,
            "waves": campaign["waves"],
            "approval_id": str(approval_id) if approval_id else None,
            "simulated": True,
        },
        run_after=run_after,
        priority=80,
    )


def probe_status(conn: Conn, host_id: UUID) -> dict[str, Any]:
    """Contrôle avant/après : observation `probe` (F18) ou santé hôte."""
    obs = hosts_mod.latest_observations(conn, host_id)
    probe = obs.get("probe")
    if probe is None:
        return {
            "ok": True,
            "status": "missing",
            "evidence": "aucune sonde : contrôle neutre (pas de dégradation constatée)",
            "degraded": False,
        }
    ok = probe["status"] == "ok"
    data = probe.get("data") or {}
    return {
        "ok": ok,
        "status": probe["status"],
        "evidence": data.get("evidence") or data.get("message") or probe["status"],
        "degraded": not ok,
        "collected_at": _iso(probe.get("collected_at")),
    }


def apply_reboot_rule(
    conn: Conn,
    *,
    environment_id: UUID,
    reboot_policy: str,
    reboot_required: bool,
) -> dict[str, Any]:
    """Respecte la politique de redémarrage et la fenêtre de changement."""
    if not reboot_required:
        return {"reboot_done": False, "reason": "redémarrage non requis"}
    if reboot_policy == "never":
        return {
            "reboot_done": False,
            "reason": "politique reboot=never : redémarrage non effectué",
            "pending": True,
        }
    state = calendars.writable(conn, environment_id)
    if not state.get("ok"):
        return {
            "reboot_done": False,
            "reason": state.get("reason") or "hors fenêtre de changement",
            "pending": True,
            "code": state.get("code"),
        }
    return {"reboot_done": True, "reason": "redémarrage simulé dans la fenêtre", "simulated": True}


def block_remaining_waves(
    conn: Conn,
    campaign: dict[str, Any],
    *,
    from_wave: int,
    reason: str,
) -> None:
    """Bloque les hôtes des vagues suivantes ; aucune écriture supplémentaire."""
    conn.execute(
        """
        UPDATE patch_campaign_hosts SET state = 'wave_blocked'
        WHERE campaign_id = %s AND wave_index > %s AND state = 'pending'
        """,
        (campaign["id"], from_wave),
    )
    conn.execute(
        """
        UPDATE patch_campaigns
        SET state = 'wave_blocked', block_reason = %s, updated_at = now()
        WHERE id = %s
        """,
        (reason[:500], campaign["id"]),
    )


def count_writes(conn: Conn, campaign_id: UUID) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM patch_campaign_hosts WHERE campaign_id = %s AND write_attempted",
        (campaign_id,),
    ).fetchone()
    return int(row["n"])
