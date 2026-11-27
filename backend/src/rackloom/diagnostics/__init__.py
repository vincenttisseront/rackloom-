"""F22 : dossier de diagnostic partageable.

Rapport filtré (période, symptômes, preuves, événements, données manquantes).
Stocké en JSONB : l'API ne peut pas écrire le volume artefacts (monté :ro ;
classe B = worker écrit / API lit). Les preuves listées restent des métadonnées
d'artefacts B. Droits vérifiés à la génération et au téléchargement.
Secrets et données hors périmètre exclus de l'export et du résumé.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import audit, events
from ..canonical import digest
from ..changes import service as changes_service
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..events import scrub_event_data
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import Redactor

SCHEMA = "rackloom.diagnostic_pack/v1"
MAX_EVENTS = 500
MAX_SYMPTOM_LEN = 500


def _parse_ts(value: Any, *, field: str) -> datetime:
    if value is None:
        raise ApiError(422, "missing_field", f"champ « {field} » requis", field=f"/{field}")
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value).strip()
        if not text:
            raise ApiError(422, "invalid_period", f"horodatage invalide pour « {field} »", field=f"/{field}")
        try:
            ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(422, "invalid_period", f"horodatage invalide pour « {field} »", field=f"/{field}") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _known_secrets(_conn: Conn, _project_id: UUID) -> list[str]:
    """Valeurs connues pour le redacteur.

    L'API ne déchiffre pas le coffre : le filtrage repose sur les motifs génériques
    (RLCANARY-…, password=, Bearer…) et sur le scrub des clés brutes d'événements.
    """
    return []


def _collect_events(
    conn: Conn, *, change_id: UUID, project_id: UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, type, source, severity, occurred_at, job_id, correlation_id, data
        FROM events
        WHERE correlation_id = %s AND project_id = %s
          AND occurred_at >= %s AND occurred_at <= %s
        ORDER BY id
        LIMIT %s
        """,
        (change_id, project_id, start, end, MAX_EVENTS),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = row["data"] if isinstance(row["data"], dict) else {}
        out.append(
            {
                "id": row["id"],
                "type": row["type"],
                "source": row["source"],
                "severity": row["severity"],
                "occurred_at": row["occurred_at"].isoformat(),
                "job_id": str(row["job_id"]) if row["job_id"] else None,
                "correlation_id": str(row["correlation_id"]) if row["correlation_id"] else None,
                "data": scrub_event_data(data),
            }
        )
    return out


def _symptoms_from_events(evts: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    symptoms: list[dict[str, Any]] = []
    for job in jobs:
        if job["state"] in ("failed", "blocked", "uncertain"):
            symptoms.append(
                {
                    "kind": "job",
                    "job_id": str(job["id"]),
                    "job_kind": job["kind"],
                    "state": job["state"],
                    "detail": (job.get("blocked_reason") or "")[:MAX_SYMPTOM_LEN] or None,
                }
            )
    for ev in evts:
        if ev["severity"] in ("error", "warning"):
            msg = ""
            data = ev.get("data") or {}
            for key in ("message", "reason", "code", "step", "state"):
                if key in data and data[key] is not None:
                    msg = f"{msg} {data[key]}".strip()
            symptoms.append(
                {
                    "kind": "event",
                    "event_id": ev["id"],
                    "type": ev["type"],
                    "source": ev["source"],
                    "severity": ev["severity"],
                    "detail": msg[:MAX_SYMPTOM_LEN] or None,
                }
            )
    return symptoms


def _evidence_class_b(conn: Conn, *, change_id: UUID, project_id: UUID) -> list[dict[str, Any]]:
    """Métadonnées des artefacts de classe B uniquement — jamais le contenu de classe A."""
    rows = conn.execute(
        """
        SELECT id, kind, sha256, size, created_at, key_class
        FROM artifacts
        WHERE change_id = %s AND project_id = %s AND key_class = 'B'
        ORDER BY created_at
        """,
        (change_id, project_id),
    ).fetchall()
    return [
        {
            "artifact_id": str(r["id"]),
            "kind": r["kind"],
            "sha256": r["sha256"],
            "size": r["size"],
            "created_at": r["created_at"].isoformat(),
            "key_class": "B",
        }
        for r in rows
    ]


def _missing_data(
    conn: Conn, *, change: dict[str, Any], evts: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    if not evts:
        missing.append({"code": "no_events", "message": "aucun événement dans la période demandée"})
    hosts = conn.execute(
        "SELECT count(*) AS n FROM hosts WHERE change_id = %s",
        (change["id"],),
    ).fetchone()
    if not hosts or hosts["n"] == 0:
        missing.append({"code": "no_hosts", "message": "aucune machine enrôlée liée à cette demande"})
    obs = conn.execute(
        """
        SELECT count(*) AS n FROM host_observations o
        JOIN hosts h ON h.id = o.host_id
        WHERE h.project_id = %s AND h.change_id = %s
        """,
        (change["project_id"], change["id"]),
    ).fetchone()
    if not obs or obs["n"] == 0:
        missing.append(
            {"code": "no_observations", "message": "aucune observation d'hôte disponible pour cette demande"}
        )
    if not evidence:
        missing.append(
            {
                "code": "no_filtered_reports",
                "message": "aucun rapport filtré (classe B) associé à cette demande",
            }
        )
    inv = conn.execute(
        "SELECT max(observed_at) AS last FROM inventory_snapshots WHERE status = 'ok'",
    ).fetchone()
    if inv is None or inv["last"] is None:
        missing.append({"code": "inventory_unavailable", "message": "inventaire indisponible ou jamais capturé"})
    return missing


def _build_summary(
    *,
    change: dict[str, Any],
    symptoms: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    event_count: int,
    redactor: Redactor,
) -> dict[str, Any]:
    """Résumé déterministe (sans appel modèle) à partir des seules données déjà filtrées."""
    parts: list[str] = [
        f"Demande {changes_service.change_ref(change)} ({change['state']}).",
        f"{event_count} événement(s) dans la période.",
    ]
    if symptoms:
        first = symptoms[0]
        detail = first.get("detail") or first.get("type") or first.get("state") or "signal"
        parts.append(f"Symptôme principal : {detail}.")
    else:
        parts.append("Aucun symptôme d'erreur ou d'avertissement dans la période.")
    if missing:
        parts.append("Données manquantes : " + ", ".join(m["code"] for m in missing) + ".")
    parts.append("Ce résumé ne contient aucun secret ni sortie brute d'outil.")
    text = redactor.text(" ".join(parts))
    return {
        "adapter": "deterministic",
        "model": "none",
        "text": text,
        "simulated": True,
        "note": "Résumé local filtré ; aucun appel Ollama sur des données non filtrées.",
    }


def _pack_view(row: dict[str, Any]) -> dict[str, Any]:
    art = row.get("artifact_id")
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "change_id": str(row["change_id"]),
        "artifact_id": str(art) if art else None,
        "created_by": str(row["created_by"]),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
        "period": {
            "start": row["period_start"].isoformat()
            if hasattr(row["period_start"], "isoformat")
            else row["period_start"],
            "end": row["period_end"].isoformat() if hasattr(row["period_end"], "isoformat") else row["period_end"],
        },
        "symptoms": row["symptoms"] or [],
        "evidence": row["evidence"] or [],
        "missing": row["missing"] or [],
        "summary": row["summary"],
        "event_count": row["event_count"],
        "evidence_count": row["evidence_count"],
        "digest": row["digest"],
        "schema": row["schema_version"],
        "download_path": f"/api/v1/diagnostic-packs/{row['id']}/download",
    }


def create_pack(
    conn: Conn,
    principal: Principal,
    change_id: UUID,
    *,
    period_start: Any = None,
    period_end: Any = None,
    symptoms: list[str] | None = None,
    include_summary: bool = True,
    request_id: str | None = None,
) -> dict[str, Any]:
    change = changes_service.get_change(conn, principal, change_id)

    now = datetime.now(UTC)
    start = _parse_ts(period_start, field="period_start") if period_start is not None else change["created_at"]
    if isinstance(start, datetime) and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = _parse_ts(period_end, field="period_end") if period_end is not None else now
    if end < start:
        raise ApiError(422, "invalid_period", "period_end doit être postérieur à period_start", field="/period_end")

    redactor = Redactor(_known_secrets(conn, change["project_id"]))
    evts = redactor.data(
        _collect_events(conn, change_id=change["id"], project_id=change["project_id"], start=start, end=end)
    )
    jobs = conn.execute(
        "SELECT id, kind, state, blocked_reason FROM jobs WHERE change_id = %s ORDER BY created_at",
        (change["id"],),
    ).fetchall()
    auto_symptoms = _symptoms_from_events(evts, jobs)
    user_symptoms: list[dict[str, Any]] = []
    for raw in symptoms or []:
        text = redactor.text(str(raw).strip())[:MAX_SYMPTOM_LEN]
        if text:
            user_symptoms.append({"kind": "operator", "detail": text})
    all_symptoms = redactor.data(user_symptoms + auto_symptoms)
    evidence = redactor.data(_evidence_class_b(conn, change_id=change["id"], project_id=change["project_id"]))
    missing = redactor.data(_missing_data(conn, change=change, evts=evts, evidence=evidence))
    summary = (
        _build_summary(
            change=change,
            symptoms=all_symptoms,
            missing=missing,
            event_count=len(evts),
            redactor=redactor,
        )
        if include_summary
        else None
    )

    report = redactor.data(
        {
            "schema": SCHEMA,
            "generated_at": now.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "change": {
                "id": str(change["id"]),
                "ref": changes_service.change_ref(change),
                "title": change["title"],
                "state": change["state"],
                "kind": change["kind"],
                "project_id": str(change["project_id"]),
            },
            "symptoms": all_symptoms,
            "evidence": evidence,
            "events": evts,
            "missing": missing,
            "summary": summary,
        }
    )
    # Re-parse après masquage binaire pour garantir un JSON sans canari.
    safe_text = redactor.text(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    report = json.loads(safe_text)
    pack_digest = digest(report)
    pack_id = uuid7()
    row = conn.execute(
        """
        INSERT INTO diagnostic_packs (
            id, project_id, change_id, artifact_id, created_by,
            period_start, period_end, symptoms, evidence, missing, summary, payload,
            event_count, evidence_count, digest, schema_version
        ) VALUES (
            %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) RETURNING *
        """,
        (
            pack_id,
            change["project_id"],
            change["id"],
            principal.user_id,
            start,
            end,
            Jsonb(all_symptoms),
            Jsonb(evidence),
            Jsonb(missing),
            Jsonb(summary) if summary is not None else None,
            Jsonb(report),
            len(evts),
            len(evidence),
            pack_digest,
            SCHEMA,
        ),
    ).fetchone()
    assert row is not None
    events.emit(
        conn,
        type="diagnostic.pack_created",
        source="system",
        project_id=change["project_id"],
        correlation_id=change["id"],
        data={
            "pack_id": str(pack_id),
            "event_count": len(evts),
            "evidence_count": len(evidence),
            "digest": pack_digest,
        },
        dedupe_key=f"diagnostic.pack:{pack_id}",
        redactor=redactor,
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="diagnostic.pack.create",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change["id"],
        targets={"pack_id": str(pack_id), "digest": pack_digest},
        request_id=request_id,
    )
    return _pack_view(row)


def list_packs(conn: Conn, principal: Principal, change_id: UUID) -> list[dict[str, Any]]:
    change = changes_service.get_change(conn, principal, change_id)
    rows = conn.execute(
        "SELECT * FROM diagnostic_packs WHERE change_id = %s AND project_id = %s ORDER BY created_at DESC",
        (change["id"], change["project_id"]),
    ).fetchall()
    return [_pack_view(r) for r in rows]


def get_pack(conn: Conn, principal: Principal, pack_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM diagnostic_packs WHERE id = %s", (pack_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("dossier de diagnostic")
    return _pack_view(row)


def download_pack(
    conn: Conn, principal: Principal, pack_id: UUID, *, request_id: str | None = None
) -> tuple[dict[str, Any], bytes]:
    """Téléchargement : droits revérifiés ; export filtré depuis le payload applicatif."""
    row = conn.execute("SELECT * FROM diagnostic_packs WHERE id = %s", (pack_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("dossier de diagnostic")
    payload = row["payload"]
    if not isinstance(payload, dict):
        raise not_found("dossier de diagnostic")
    # Re-filtre défensif au téléchargement.
    safe = Redactor().data(payload)
    content = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    content = Redactor().bytes(content)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="diagnostic.pack.download",
        outcome="ok",
        project_id=row["project_id"],
        correlation_id=row["change_id"],
        targets={"pack_id": str(pack_id), "digest": row["digest"]},
        request_id=request_id,
    )
    return _pack_view(row), content
