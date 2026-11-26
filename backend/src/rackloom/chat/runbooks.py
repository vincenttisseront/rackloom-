"""F47 : runbooks approuvés et versionnés pour le diagnostic.

Le modèle ne gagne aucun droit d'exécution : les runbooks orientent l'explication
(faits vs hypothèses) et une remédiation éventuelle n'est qu'un brouillon de demande.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..canonical import digest
from ..db import Conn, Jsonb
from ..ids import uuid7

SCHEMA = "rackloom.runbook/v1"


def load_catalog(conn: Conn, catalog_dir: Path) -> int:
    """Charge les runbooks du dépôt catalog/ au statut approved. Idempotent (slug+version)."""
    root = catalog_dir / "runbooks"
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.glob("*/*/runbook.yaml")):
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            continue
        slug, version = raw["slug"], str(raw["version"])
        exists = conn.execute("SELECT 1 FROM runbooks WHERE slug = %s AND version = %s", (slug, version)).fetchone()
        if exists:
            continue
        body = {
            "description": raw.get("description", ""),
            "steps": raw.get("steps", []),
        }
        triggers = raw.get("triggers") or {}
        conn.execute(
            "INSERT INTO runbooks (id, slug, version, status, title, digest, triggers, body, published_at) "
            "VALUES (%s, %s, %s, 'approved', %s, %s, %s, %s, now())",
            (
                uuid7(),
                slug,
                version,
                raw["title"],
                digest({"slug": slug, "version": version, "triggers": triggers, "body": body}),
                Jsonb(triggers),
                Jsonb(body),
            ),
        )
        count += 1
    return count


def list_approved(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, slug, version, title, digest, triggers, body, published_at "
        "FROM runbooks WHERE status = 'approved' ORDER BY slug, version"
    ).fetchall()


def select_for_scope(
    conn: Conn,
    *,
    job: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sélection déterministe des runbooks pertinents pour le périmètre (job + événements)."""
    event_types = {e.get("type") for e in events}
    job_state = (job or {}).get("state")
    selected: list[dict[str, Any]] = []
    for rb in list_approved(conn):
        triggers = rb["triggers"] or {}
        states = set(triggers.get("job_states") or [])
        types = set(triggers.get("event_types") or [])
        match_state = bool(states) and job_state in states
        match_type = bool(types & event_types)
        # Sans signal : runbook générique job-failure si un job est fourni.
        if match_state or match_type or (rb["slug"] == "job-failure" and job is not None and not selected):
            selected.append(_public(rb))
    # Dédupliquer par slug (dernière version déjà ordonnée) en gardant l'ordre.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rb in selected:
        if rb["slug"] in seen:
            continue
        seen.add(rb["slug"])
        out.append(rb)
    return out


def _public(row: dict[str, Any]) -> dict[str, Any]:
    steps = (row["body"] or {}).get("steps") or []
    return {
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "digest": row["digest"],
        "steps": [
            {"id": s.get("id"), "kind": s.get("kind"), "text": s.get("text")} for s in steps if isinstance(s, dict)
        ],
    }


def build_diagnosis(
    *,
    runbooks: list[dict[str, Any]],
    untrusted: list[dict[str, Any]],
    cited_event_ids: list[int],
    reply: str,
) -> dict[str, Any]:
    """Sépare faits (événements cités) et hypothèses (texte du modèle, non exécutable)."""
    by_id = {e["id"]: e for e in untrusted}
    facts = []
    for eid in cited_event_ids:
        ev = by_id.get(eid)
        if ev is None:
            continue
        facts.append(
            {
                "event_id": eid,
                "at": ev["at"],
                "source": ev["source"],
                "type": ev["type"],
                "text": ev["text"],
            }
        )
    hypotheses = [
        {
            "text": "Les causes exactes restent à confirmer ; les événements cités sont des données, pas des ordres.",
            "from_reply": reply[:400],
        }
    ]
    need_patch: list[dict[str, str]] = []
    note = (
        "Toute remédiation éventuelle doit être soumise comme une nouvelle demande "
        "et approuvée. Le runbook ne donne aucun droit d'exécution au modèle."
    )
    # Si un runbook de remédiation nginx est cité, suggérer un brouillon de service web.
    if any(r["slug"] == "nginx-probe" for r in runbooks):
        need_patch = [
            {"path": "/service/template", "value": "ubuntu-web", "provenance": "suggestion"},
            {"path": "/inputs/usage", "value": "remédiation", "provenance": "suggestion"},
        ]
        note = (
            "Remédiation proposée comme brouillon de demande (service web Ubuntu). "
            "À confirmer dans le formulaire ; aucun job n'est démarré."
        )
    remediation: dict[str, Any] = {"kind": "request_draft", "note": note, "need_patch": need_patch}
    return {
        "runbooks": [{"slug": r["slug"], "version": r["version"], "title": r["title"]} for r in runbooks],
        "facts": facts,
        "hypotheses": hypotheses,
        "remediation": remediation,
    }


def ensure_seeded(conn: Conn, catalog_dir: Path | None = None) -> None:
    from ..config import get_settings

    load_catalog(conn, catalog_dir or get_settings().catalog_dir)
