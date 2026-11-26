"""F54 : passer la main sans perdre le contexte.

Solliciter un collègue déjà habilité sur le projet. Aucun role_binding créé.
Un commentaire ne modifie pas le plan et ne vaut pas approbation.
Nouvelle version qui change la cible ou le contenu → écarts affichés ; cycle de validation inchangé.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, events, projects
from ..db import Conn
from ..errors import ApiError, not_found
from ..identity.authz import Principal
from ..ids import uuid7

MAX_QUESTION = 2000
MAX_REPLY = 4000


def _member_ids(conn: Conn, project_id: UUID) -> set[UUID]:
    return {m["id"] for m in projects.members(conn, project_id)}


def _latest_version(conn: Conn, change_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT version_no, need FROM request_versions WHERE change_id = %s ORDER BY version_no DESC LIMIT 1",
        (change_id,),
    ).fetchone()


def _latest_revision(conn: Conn, change_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT revision_no, version_no, outcome, resolution_digest FROM request_revisions "
        "WHERE change_id = %s ORDER BY revision_no DESC LIMIT 1",
        (change_id,),
    ).fetchone()


def _row_view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    creator = conn.execute("SELECT display_name, email FROM users WHERE id = %s", (row["created_by"],)).fetchone()
    solicited = conn.execute(
        "SELECT display_name, email FROM users WHERE id = %s", (row["solicited_user_id"],)
    ).fetchone()
    return {
        "id": str(row["id"]),
        "change_id": str(row["change_id"]),
        "version_no": row["version_no"],
        "revision_no": row["revision_no"],
        "plan_id": str(row["plan_id"]) if row.get("plan_id") else None,
        "focus": row.get("focus"),
        "question": row["question"],
        "status": row["status"],
        "created_by": {
            "id": str(row["created_by"]),
            "display_name": creator["display_name"] if creator else None,
            "email": creator["email"] if creator else None,
        },
        "solicited": {
            "id": str(row["solicited_user_id"]),
            "display_name": solicited["display_name"] if solicited else None,
            "email": solicited["email"] if solicited else None,
        },
        "reply_text": row.get("reply_text"),
        "replied_at": row["replied_at"].isoformat() if row.get("replied_at") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "notice": "Ce commentaire ne modifie pas le plan et ne vaut pas approbation.",
    }


def _diffs_since(conn: Conn, change: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    """Écarts depuis la dernière revue (ou depuis la sollicitation examinée)."""
    current_v = _latest_version(conn, change["id"])
    current_r = _latest_revision(conn, change["id"])
    if review is None:
        return {
            "has_newer_version": False,
            "version_no_then": None,
            "version_no_now": current_v["version_no"] if current_v else None,
            "revision_no_then": None,
            "revision_no_now": current_r["revision_no"] if current_r else None,
            "approval_revoked": False,
            "notes": [],
        }
    notes: list[str] = []
    newer = bool(current_v and current_v["version_no"] > review["version_no"])
    if newer:
        notes.append(
            f"Nouvelle version {current_v['version_no']} depuis la revue de la version "
            f"{review['version_no']} : un nouveau cycle de validation s'applique."
        )
    rev_newer = bool(
        current_r and review.get("revision_no") is not None and current_r["revision_no"] > review["revision_no"]
    )
    if rev_newer:
        notes.append(
            f"Nouvelle révision {current_r['revision_no']} depuis la revue (révision {review['revision_no']})."
        )
    superseded = conn.execute(
        "SELECT count(*) AS n FROM events "
        "WHERE correlation_id = %s AND type = 'change.superseded' AND occurred_at > %s",
        (change["id"], review["created_at"]),
    ).fetchone()
    approval_revoked = bool(superseded and superseded["n"])
    if approval_revoked:
        notes.append("Une version ultérieure a invalidé le plan ou l'approbation précédente.")

    return {
        "has_newer_version": newer or rev_newer,
        "version_no_then": review["version_no"],
        "version_no_now": current_v["version_no"] if current_v else None,
        "revision_no_then": review.get("revision_no"),
        "revision_no_now": current_r["revision_no"] if current_r else None,
        "approval_revoked": approval_revoked,
        "notes": notes,
    }


def list_reviews(conn: Conn, principal: Principal, change: dict[str, Any]) -> dict[str, Any]:
    """Liste les revues du changement (404 déjà appliqué via get_change)."""
    rows = conn.execute(
        "SELECT * FROM change_reviews WHERE change_id = %s ORDER BY created_at DESC",
        (change["id"],),
    ).fetchall()
    last = rows[0] if rows else None
    # Marquer les revues open dont la version a été dépassée.
    items = []
    for row in rows:
        view = _row_view(conn, row)
        if row["status"] == "open":
            current = _latest_version(conn, change["id"])
            if current and current["version_no"] > row["version_no"]:
                view["status_note"] = (
                    "Une version plus récente existe ; cette sollicitation porte sur une version antérieure."
                )
        items.append(view)
    return {
        "items": items,
        "diffs_since_last_review": _diffs_since(conn, change, last),
        "notice": "Partager ne crée aucun droit. Seuls les membres du projet peuvent être sollicités.",
    }


def solicit(
    conn: Conn,
    principal: Principal,
    change: dict[str, Any],
    body: dict[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    """Sollicite un collègue habilité. Aucun role_binding."""
    solicited_raw = body.get("solicited_user_id")
    question = body.get("question")
    focus = body.get("focus")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise ApiError(422, "invalid_question", "question requise (1 à 2000 caractères)", field="/question")
    try:
        solicited_id = UUID(str(solicited_raw))
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_user", "identifiant utilisateur invalide", field="/solicited_user_id") from None

    members = _member_ids(conn, change["project_id"])
    if solicited_id not in members:
        # Personne non habilitée : refus (pas de fuite sur l'existence hors projet via 404 user).
        raise ApiError(
            422,
            "not_eligible",
            "seul un collègue habilité sur le projet peut être sollicité",
            field="/solicited_user_id",
        )
    if solicited_id == principal.user_id:
        raise ApiError(422, "not_self", "vous ne pouvez pas vous solliciter vous-même", field="/solicited_user_id")

    version = _latest_version(conn, change["id"])
    if version is None:
        raise ApiError(409, "no_version", "aucune version à examiner")
    version_no = body.get("version_no", version["version_no"])
    if not isinstance(version_no, int) or version_no != version["version_no"]:
        # On ancre sur la version exacte courante ; un numéro fourni doit correspondre.
        if isinstance(version_no, int) and version_no != version["version_no"]:
            raise ApiError(
                409,
                "stale_version",
                f"la version courante est {version['version_no']}",
                field="/version_no",
            )
        version_no = version["version_no"]

    revision = _latest_revision(conn, change["id"])
    revision_no = body.get("revision_no")
    if revision_no is None and revision is not None:
        revision_no = revision["revision_no"]
    plan_id = body.get("plan_id")
    plan_uuid = UUID(str(plan_id)) if plan_id else None

    row = conn.execute(
        "INSERT INTO change_reviews "
        "(id, change_id, project_id, version_no, revision_no, plan_id, focus, question, "
        "created_by, solicited_user_id, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open') RETURNING *",
        (
            uuid7(),
            change["id"],
            change["project_id"],
            version_no,
            revision_no,
            plan_uuid,
            focus if isinstance(focus, str) else None,
            question.strip(),
            principal.user_id,
            solicited_id,
        ),
    ).fetchone()

    events.emit(
        conn,
        type="review.solicited",
        source="request",
        project_id=change["project_id"],
        correlation_id=change["id"],
        environment_id=change.get("environment_id"),
        data={
            "review_id": str(row["id"]),
            "version_no": version_no,
            "revision_no": revision_no,
            "solicited_user_id": str(solicited_id),
            "focus": focus,
        },
        dedupe_key=f"review.solicited:{row['id']}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="review.solicit",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change["id"],
        targets={"review_id": str(row["id"]), "solicited_user_id": str(solicited_id)},
        versions={"version_no": version_no, "revision_no": revision_no},
        request_id=request_id,
    )
    # Garantie produit : aucun INSERT role_bindings dans ce module.
    return _row_view(conn, row)


def reply(
    conn: Conn,
    principal: Principal,
    change: dict[str, Any],
    review_id: UUID,
    body: dict[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    """Répond sur la version soumise. Ne modifie pas le plan."""
    row = conn.execute(
        "SELECT * FROM change_reviews WHERE id = %s AND change_id = %s FOR UPDATE",
        (review_id, change["id"]),
    ).fetchone()
    if row is None:
        raise not_found("revue")
    if row["solicited_user_id"] != principal.user_id:
        raise ApiError(403, "forbidden", "seul le collègue sollicité peut répondre")
    if row["status"] != "open":
        raise ApiError(409, "already_replied", "cette sollicitation a déjà une réponse")

    text = body.get("body")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY:
        raise ApiError(422, "invalid_reply", "réponse requise (1 à 4000 caractères)", field="/body")

    updated = conn.execute(
        "UPDATE change_reviews SET status = 'replied', reply_text = %s, replied_at = now(), "
        "updated_at = now() WHERE id = %s RETURNING *",
        (text.strip(), review_id),
    ).fetchone()

    events.emit(
        conn,
        type="review.replied",
        source="request",
        project_id=change["project_id"],
        correlation_id=change["id"],
        environment_id=change.get("environment_id"),
        data={
            "review_id": str(review_id),
            "version_no": updated["version_no"],
            "revision_no": updated["revision_no"],
        },
        dedupe_key=f"review.replied:{review_id}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="review.reply",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change["id"],
        targets={"review_id": str(review_id)},
        request_id=request_id,
    )
    return _row_view(conn, updated)


def candidates(conn: Conn, principal: Principal, change: dict[str, Any]) -> list[dict[str, Any]]:
    """Collègues habilités sollicitables (membres du projet, hors soi)."""
    out = []
    for m in projects.members(conn, change["project_id"]):
        if m["id"] == principal.user_id:
            continue
        out.append({"id": str(m["id"]), "display_name": m["display_name"], "email": m["email"]})
    return out
