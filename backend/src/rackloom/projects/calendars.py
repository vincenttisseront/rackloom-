"""Calendriers de changement (F27) : fenêtres de maintenance et gels planifiés.

Aucun job d'écriture n'est démarré hors fenêtre (quand des fenêtres sont définies) ni pendant
une période de gel du calendrier. Le gel d'urgence (F28) reste distinct. Seuls des plans
approuvés sont programmés (`run_after` = prochaine ouverture) ; l'admission R090 et R060
revérifient à l'ouverture. Fuseaux IANA ; DST Europe/Paris couverte via `zoneinfo`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import audit, clock, events
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact
from . import freezes

DEFAULT_TZ = "Europe/Paris"
WEEKDAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def can_manage(principal: Principal, env: dict[str, Any]) -> bool:
    return freezes.can_manage(principal, env)


def _authorize(principal: Principal, env: dict[str, Any]) -> None:
    if not can_manage(principal, env):
        raise forbidden("rôle approbateur de l'environnement ou administrateur de plateforme requis "
                        "pour le calendrier")


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ApiError(422, "invalid_timezone", f"fuseau « {name} » inconnu", field="/timezone") from exc


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def ensure_calendar(conn: Conn, env: dict[str, Any], *, timezone: str | None = None,
                    actor: Principal | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM environment_calendars WHERE environment_id = %s",
                       (env["id"],)).fetchone()
    if row is None:
        tz = timezone or DEFAULT_TZ
        _tz(tz)
        return conn.execute(
            "INSERT INTO environment_calendars (environment_id, project_id, timezone, updated_by) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (env["id"], env["project_id"], tz, actor.user_id if actor else None),
        ).fetchone()
    if timezone is not None and timezone != row["timezone"]:
        _tz(timezone)
        return conn.execute(
            "UPDATE environment_calendars SET timezone = %s, updated_at = now(), updated_by = %s "
            "WHERE environment_id = %s RETURNING *",
            (timezone, actor.user_id if actor else None, env["id"]),
        ).fetchone()
    return row


def timezone_of(conn: Conn, environment_id: UUID) -> str:
    row = conn.execute("SELECT timezone FROM environment_calendars WHERE environment_id = %s",
                       (environment_id,)).fetchone()
    return row["timezone"] if row else DEFAULT_TZ


def active_windows(conn: Conn, environment_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM change_windows WHERE environment_id = %s AND revoked_at IS NULL ORDER BY start_local, id",
        (environment_id,),
    ).fetchall()


def active_calendar_freezes(conn: Conn, environment_id: UUID, at: datetime | None = None) -> list[dict[str, Any]]:
    instant = at or clock.now()
    return conn.execute(
        "SELECT * FROM calendar_freezes WHERE environment_id = %s AND cancelled_at IS NULL "
        "AND starts_at <= %s AND ends_at > %s ORDER BY starts_at",
        (environment_id, instant, instant),
    ).fetchall()


def _local(at: datetime, tz: ZoneInfo) -> datetime:
    return at.astimezone(tz)


def _in_window(local: datetime, window: dict[str, Any]) -> bool:
    wd = local.weekday()  # Mon=0
    t = local.timetz().replace(tzinfo=None)
    start: time = window["start_local"]
    end: time = window["end_local"]
    days = set(window["weekdays"])
    if start < end:
        return wd in days and start <= t < end
    # Fenêtre à cheval sur minuit : [start, 24h) le jour J et [0, end) le lendemain.
    if wd in days and t >= start:
        return True
    prev = (wd - 1) % 7
    return prev in days and t < end


def _open_now(conn: Conn, environment_id: UUID, instant: datetime, tz_name: str,
              windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Verdict à `instant` sans calculer la prochaine ouverture (évite la récursion)."""
    freezes_now = active_calendar_freezes(conn, environment_id, instant)
    if freezes_now:
        f = freezes_now[0]
        return {"ok": False, "code": "calendar_frozen",
                "reason": f"période de gel du calendrier ({f['reason']})", "timezone": tz_name,
                "freeze_id": f["id"], "until": f["ends_at"]}
    if not windows:
        return {"ok": True, "code": "open", "reason": "aucune fenêtre : écritures toujours autorisées",
                "timezone": tz_name}
    local = _local(instant, _tz(tz_name))
    if any(_in_window(local, w) for w in windows):
        return {"ok": True, "code": "in_window", "reason": "dans une fenêtre de maintenance",
                "timezone": tz_name}
    return {"ok": False, "code": "outside_change_window", "reason": "hors fenêtre de maintenance",
            "timezone": tz_name}


def writable(conn: Conn, environment_id: UUID, at: datetime | None = None) -> dict[str, Any]:
    """État d'écriture calendaire à l'instant `at` (UTC)."""
    instant = at or clock.now()
    tz_name = timezone_of(conn, environment_id)
    windows = active_windows(conn, environment_id)
    state = _open_now(conn, environment_id, instant, tz_name, windows)
    if not state["ok"] and state["code"] == "outside_change_window":
        state["next_open"] = _next_window_start(conn, environment_id, instant, tz_name, windows)
    return state


def _combine_local(day: date, t: time, tz: ZoneInfo) -> datetime | None:
    """Construit un instant UTC pour (jour, heure locale). None si l'heure locale n'existe pas (DST)."""
    try:
        return datetime(day.year, day.month, day.day, t.hour, t.minute, t.second, tzinfo=tz).astimezone(UTC)
    except Exception:  # noqa: BLE001 - heure inexistante au passage d'heure
        return None


def _next_window_start(conn: Conn, environment_id: UUID, instant: datetime, tz_name: str,
                       windows: list[dict[str, Any]]) -> datetime | None:
    if not windows:
        return instant
    if _open_now(conn, environment_id, instant, tz_name, windows)["ok"]:
        return instant
    tz = _tz(tz_name)
    local = _local(instant, tz)
    candidates: list[datetime] = []
    for offset in range(0, 15):
        day = local.date() + timedelta(days=offset)
        for w in windows:
            for wd in set(w["weekdays"]):
                delta = (wd - day.weekday()) % 7
                target = day + timedelta(days=delta)
                start_utc = _combine_local(target, w["start_local"], tz)
                if start_utc is None or start_utc <= instant:
                    continue
                if not active_calendar_freezes(conn, environment_id, start_utc):
                    candidates.append(start_utc)
    return min(candidates) if candidates else None


def next_open(conn: Conn, environment_id: UUID, after: datetime | None = None) -> datetime | None:
    """Prochaine ouverture de fenêtre après `after` (UTC), ou None si aucune fenêtre."""
    instant = after or clock.now()
    tz_name = timezone_of(conn, environment_id)
    windows = active_windows(conn, environment_id)
    return _next_window_start(conn, environment_id, instant, tz_name, windows)


def schedule_run_after(conn: Conn, environment_id: UUID, at: datetime | None = None) -> datetime:
    """Instant auquel un job d'écriture peut être réclamé (maintenant ou prochaine fenêtre)."""
    instant = at or clock.now()
    state = writable(conn, environment_id, instant)
    if state["ok"]:
        return instant
    nxt = state.get("next_open") or next_open(conn, environment_id, instant)
    if nxt is None:
        # Gel calendaire sans fenêtre ultérieure connue : reporter à la fin du gel actif.
        freezes_now = active_calendar_freezes(conn, environment_id, instant)
        if freezes_now:
            return freezes_now[0]["ends_at"]
        return instant + timedelta(hours=1)
    return nxt


def set_timezone(conn: Conn, principal: Principal, environment_id: UUID, timezone: str,
                 request_id: str | None = None) -> dict[str, Any]:
    env = freezes.load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    cal = ensure_calendar(conn, env, timezone=timezone, actor=principal)
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="calendar.timezone",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"])},
                 policy_decision={"timezone": cal["timezone"]}, request_id=request_id)
    return cal


def add_window(conn: Conn, principal: Principal, environment_id: UUID, body: dict[str, Any],
               request_id: str | None = None) -> dict[str, Any]:
    env = freezes.load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    ensure_calendar(conn, env, actor=principal)
    weekdays = body.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays:
        raise ApiError(422, "weekdays_required", "au moins un jour de la semaine", field="/weekdays")
    try:
        days = sorted({int(d) for d in weekdays})
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid_weekdays", "jours 0 (lundi) à 6 (dimanche)", field="/weekdays") from exc
    if any(d < 0 or d > 6 for d in days):
        raise ApiError(422, "invalid_weekdays", "jours 0 (lundi) à 6 (dimanche)", field="/weekdays")
    start = _parse_time(body.get("start_local"), "start_local")
    end = _parse_time(body.get("end_local"), "end_local")
    if start == end:
        raise ApiError(422, "invalid_window", "début et fin identiques", field="/end_local")
    label = redact(str(body.get("label") or "").strip())[:120]
    row = conn.execute(
        "INSERT INTO change_windows (id, environment_id, project_id, weekdays, start_local, end_local, label, "
        "created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), env["id"], env["project_id"], days, start, end, label, principal.user_id),
    ).fetchone()
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="calendar.window.add",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "window_id": str(row["id"])},
                 policy_decision={"weekdays": days, "start": start.isoformat(), "end": end.isoformat()},
                 request_id=request_id)
    events.emit(conn, type="calendar.window_added", source="system", project_id=env["project_id"],
                environment_id=env["id"],
                data={"window_id": str(row["id"]), "weekdays": days, "by": principal.display_name},
                dedupe_key=f"calendar.window_added:{row['id']}")
    return row


def revoke_window(conn: Conn, principal: Principal, environment_id: UUID, window_id: UUID,
                  request_id: str | None = None) -> None:
    env = freezes.load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    row = conn.execute(
        "UPDATE change_windows SET revoked_at = now(), revoked_by = %s "
        "WHERE id = %s AND environment_id = %s AND revoked_at IS NULL RETURNING *",
        (principal.user_id, window_id, env["id"]),
    ).fetchone()
    if row is None:
        raise not_found("fenêtre")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="calendar.window.revoke",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "window_id": str(window_id)},
                 request_id=request_id)


def add_freeze(conn: Conn, principal: Principal, environment_id: UUID, body: dict[str, Any],
               request_id: str | None = None) -> dict[str, Any]:
    env = freezes.load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    cal = ensure_calendar(conn, env, actor=principal)
    tz_name = str(body.get("timezone") or cal["timezone"])
    tz = _tz(tz_name)
    starts = _parse_instant(body.get("starts_at"), "starts_at", tz)
    ends = _parse_instant(body.get("ends_at"), "ends_at", tz)
    if ends <= starts:
        raise ApiError(422, "invalid_period", "la fin doit être après le début", field="/ends_at")
    reason = _reason(body.get("reason"))
    row = conn.execute(
        "INSERT INTO calendar_freezes (id, environment_id, project_id, starts_at, ends_at, timezone, reason, "
        "created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), env["id"], env["project_id"], starts, ends, tz_name, reason, principal.user_id),
    ).fetchone()
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="calendar.freeze.add",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "calendar_freeze_id": str(row["id"])},
                 policy_decision={"reason": reason, "starts_at": starts.isoformat(), "ends_at": ends.isoformat()},
                 request_id=request_id)
    events.emit(conn, type="calendar.freeze_added", source="system", project_id=env["project_id"],
                environment_id=env["id"],
                data={"freeze_id": str(row["id"]), "reason": reason, "by": principal.display_name},
                severity="warning", dedupe_key=f"calendar.freeze_added:{row['id']}")
    return row


def cancel_freeze(conn: Conn, principal: Principal, environment_id: UUID, freeze_id: UUID, reason: Any,
                  request_id: str | None = None) -> None:
    env = freezes.load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    text = _reason(reason, "reason")
    row = conn.execute(
        "UPDATE calendar_freezes SET cancelled_at = now(), cancelled_by = %s, cancel_reason = %s "
        "WHERE id = %s AND environment_id = %s AND cancelled_at IS NULL RETURNING *",
        (principal.user_id, text, freeze_id, env["id"]),
    ).fetchone()
    if row is None:
        raise not_found("période de gel")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="calendar.freeze.cancel",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "calendar_freeze_id": str(freeze_id)},
                 policy_decision={"reason": text}, request_id=request_id)


def summary(conn: Conn, environment_id: UUID, at: datetime | None = None) -> dict[str, Any]:
    instant = at or clock.now()
    tz_name = timezone_of(conn, environment_id)
    state = writable(conn, environment_id, instant)
    windows = [{
        "id": str(w["id"]), "weekdays": list(w["weekdays"]),
        "weekdays_label": ", ".join(WEEKDAY_NAMES[d] for d in w["weekdays"]),
        "start_local": w["start_local"].isoformat(), "end_local": w["end_local"].isoformat(),
        "label": w["label"],
    } for w in active_windows(conn, environment_id)]
    upcoming = conn.execute(
        "SELECT * FROM calendar_freezes WHERE environment_id = %s AND cancelled_at IS NULL "
        "AND ends_at > %s ORDER BY starts_at LIMIT 20",
        (environment_id, instant),
    ).fetchall()
    freezes_view = [{
        "id": str(f["id"]), "starts_at": f["starts_at"].isoformat(), "ends_at": f["ends_at"].isoformat(),
        "timezone": f["timezone"], "reason": f["reason"],
        "active": f["starts_at"] <= instant < f["ends_at"],
    } for f in upcoming]
    return {
        "timezone": tz_name,
        "state": {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v)
                  for k, v in state.items()},
        "windows": windows,
        "calendar_freezes": freezes_view,
    }


def _parse_time(value: Any, field: str) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    try:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) == 3:
            return time(parts[0], parts[1], parts[2])
    except (TypeError, ValueError):
        pass
    raise ApiError(422, "invalid_time", "heure locale attendue (HH:MM)", field=f"/{field}")


def _parse_instant(value: Any, field: str, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=tz).astimezone(UTC)
    text = str(value or "").strip()
    if not text:
        raise ApiError(422, "invalid_datetime", "horodatage obligatoire", field=f"/{field}")
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(422, "invalid_datetime", "horodatage ISO 8601 attendu", field=f"/{field}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)
