"""F49 : aperçu du contexte filtré envoyé au modèle.

Le filtrage (secrets, canari) est appliqué côté serveur avant l'aperçu.
L'opérateur peut retirer des extraits admissibles, jamais en ajouter hors périmètre.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..db import Conn
from ..errors import ApiError
from ..identity.authz import Principal
from ..redact import Redactor


def preview(
    conn: Conn,
    principal: Principal,
    *,
    project: dict[str, Any],
    need: dict[str, Any],
    mode: str,
    job_id: UUID | None,
    exclude_extract_ids: list[int] | None,
    include_extract_ids: list[int] | None,
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    """Construit l'aperçu admissible. Refuse tout ajout hors périmètre."""
    # Imports locaux : évite le cycle avec chat.__init__.
    from . import _untrusted_events, build_context

    if include_extract_ids:
        raise ApiError(
            422,
            "extract_out_of_scope",
            "ajout d'extrait hors périmètre refusé : seuls des retraits sont autorisés",
            field="/include_extract_ids",
        )
    redactor = redactor or Redactor()
    context = build_context(conn, principal, project, need)
    extracts: list[dict[str, Any]] = []
    if mode in ("explain", "diagnose"):
        if job_id is None:
            raise ApiError(422, "job_required", "job à expliquer requis", field="/job_id")
        extracts = _untrusted_events(conn, project["id"], job_id, redactor)
    admissible_ids = {e["id"] for e in extracts}
    excluded = list(exclude_extract_ids or [])
    for eid in excluded:
        if eid not in admissible_ids:
            raise ApiError(
                422,
                "extract_unknown",
                f"extrait « {eid} » absent du périmètre admissible",
                field="/exclude_extract_ids",
            )
    kept = [e for e in extracts if e["id"] not in set(excluded)]
    # Double filtrage : rien de sensible ne doit apparaître dans l'aperçu.
    safe_context = redactor.data(context)
    safe_extracts = redactor.data(kept)
    return {
        "mode": mode,
        "context": safe_context,
        "extracts": safe_extracts,
        "excluded_extract_ids": excluded,
        "admissible_extract_ids": sorted(admissible_ids),
        "note": (
            "Contexte déjà filtré côté serveur. Vous pouvez retirer des extraits ; "
            "vous ne pouvez pas en ajouter hors périmètre. Aucun secret n'est affiché."
        ),
    }


def apply_exclusions(
    extracts: list[dict[str, Any]],
    exclude_extract_ids: list[int] | None,
    *,
    allow_unknown: bool = False,
) -> list[dict[str, Any]]:
    if not exclude_extract_ids:
        return extracts
    admissible = {e["id"] for e in extracts}
    for eid in exclude_extract_ids:
        if eid not in admissible and not allow_unknown:
            raise ApiError(
                422,
                "extract_unknown",
                f"extrait « {eid} » absent du périmètre admissible",
                field="/exclude_extract_ids",
            )
    banned = set(exclude_extract_ids)
    return [e for e in extracts if e["id"] not in banned]
