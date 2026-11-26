"""Assistant : conversations et tours validés. Aucune route du chat n'écrit de besoin, d'approbation ni de job."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import audit, chat, projects
from ..changes import need as need_schema
from ..chat import context as chat_context
from ..chat import eval as chat_eval
from ..chat import routing as model_routing
from ..errors import ApiError
from ..identity.authz import require_admin
from ..projects import preferences as project_preferences
from ..redact import Redactor
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["assistant"], dependencies=[Depends(csrf_guard)])


@router.get("/assistant/status")
def assistant_status(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        cfg = chat.assistant_settings(u.conn)
        return {
            "available": cfg["available"],
            "simulated": cfg["simulated"],
            "adapter": cfg["adapter"],
            "routing": cfg.get("routing"),
        }


@router.get("/admin/assistant")
def assistant_config(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        require_admin(u.p)
        cfg = chat.assistant_settings(u.conn)
        routing = model_routing.routing_config(u.conn)
        return {
            "adapter": cfg["adapter"],
            "scenario": cfg["scenario"],
            "simulated": cfg["simulated"],
            "adapters": ["fake", "ollama", "disabled"],
            "scenarios": list(chat.SCENARIOS),
            "prompt_id": chat.PROMPT_ID,
            "routing": routing,
        }


@router.put("/admin/assistant")
def configure_assistant(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    extra = set(body) - {"adapter", "scenario", "routing"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        result = chat.set_assistant(u.conn, u.p, str(body.get("adapter")), str(body.get("scenario", "nominal")))
        if "routing" in body and isinstance(body["routing"], dict):
            rt = body["routing"]
            model_routing.set_routing(
                u.conn,
                models=rt.get("models"),
                disabled_roles=rt.get("disabled_roles"),
            )
            audit.record(
                u.conn,
                actor_type="user",
                actor_id=str(u.p.user_id),
                action="assistant.routing.configure",
                outcome="ok",
                targets={"disabled_roles": rt.get("disabled_roles") or []},
            )
            result = chat.assistant_settings(u.conn)
        return result


@router.post("/admin/assistant/eval")
def run_assistant_eval(request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """F46 : rejoue les conversations fictives ; n'écrit ni besoin, ni approbation, ni job."""
    extra = set(body) - {"adapter", "scenario", "project"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    with unit_of_work(request, write=True) as u:
        return chat_eval.run_bench(
            u.conn,
            u.p,
            adapter=str(body["adapter"]) if body.get("adapter") is not None else None,
            scenario=str(body["scenario"]) if body.get("scenario") is not None else None,
            project_slug=str(body.get("project") or "vitrine"),
        )


@router.get("/admin/assistant/eval")
def list_assistant_evals(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return {"items": chat_eval.list_reports(u.conn, u.p), "prompt_id": chat.PROMPT_ID}


@router.get("/admin/assistant/eval/{report_id}")
def get_assistant_eval(report_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return chat_eval.get_report(u.conn, u.p, report_id)


@router.post("/projects/{project}/conversations")
def create_conversation(project: str, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    extra = set(body) - {"change_id"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    try:
        change_id = UUID(str(body["change_id"])) if body.get("change_id") else None
    except ValueError as exc:
        raise ApiError(422, "invalid_change", "identifiant de demande invalide", field="/change_id") from exc
    with unit_of_work(request, write=True) as u:
        p = projects.get_project(u.conn, u.p, project)
        conv = chat.create_conversation(u.conn, u.p, p, change_id)
        cfg = chat.assistant_settings(u.conn)
        return {
            "id": str(conv["id"]),
            "assistant": {
                "available": cfg["available"],
                "simulated": cfg["simulated"],
                "routing": cfg.get("routing"),
            },
        }


@router.post("/conversations/{conversation_id}/context-preview")
def preview_context(conversation_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """F49 : aperçu du contexte filtré. Retrait d'extraits seulement ; aucun ajout hors périmètre."""
    extra = set(body) - {"need", "mode", "job_id", "exclude_extract_ids", "include_extract_ids"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    mode = body.get("mode", "draft")
    if mode not in ("draft", "explain", "diagnose"):
        raise ApiError(422, "invalid_mode", "mode attendu : draft, explain ou diagnose", field="/mode")
    job_id = None
    if body.get("job_id"):
        try:
            job_id = UUID(str(body["job_id"]))
        except ValueError as exc:
            raise ApiError(422, "invalid_job", "identifiant de job invalide", field="/job_id") from exc
    raw_need = body.get("need") or {
        "schema": "rackloom.need/v1",
        "service": {"template": None},
        "environment": None,
        "confirm_production": False,
        "inputs": {},
    }
    with unit_of_work(request) as u:
        conv = u.conn.execute("SELECT * FROM chat_conversations WHERE id = %s", (conversation_id,)).fetchone()
        if conv is None or conv["user_id"] != u.p.user_id:
            from ..errors import not_found

            raise not_found("conversation")
        project = u.conn.execute("SELECT * FROM projects WHERE id = %s", (conv["project_id"],)).fetchone()
        if project is None:
            from ..errors import not_found

            raise not_found("projet")
        need = need_schema.validate(raw_need, partial=True)
        return chat_context.preview(
            u.conn,
            u.p,
            project=project,
            need=need,
            mode=mode,
            job_id=job_id,
            exclude_extract_ids=body.get("exclude_extract_ids"),
            include_extract_ids=body.get("include_extract_ids"),
            redactor=Redactor(),
        )


@router.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return chat.post_message(u.conn, u.p, conversation_id, body)


@router.get("/projects/{project}/preferences")
def get_preferences(project: str, request: Request) -> dict[str, Any]:
    """F50 : préférences de projet approuvées (lecture)."""
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        current = project_preferences.current(u.conn, p["id"])
        return {
            "current": project_preferences.public_view(current),
            "versions": [
                project_preferences.public_view(v) for v in project_preferences.list_versions(u.conn, p["id"])
            ],
        }


@router.post("/projects/{project}/preferences")
def publish_preferences(project: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F50 : publie une nouvelle version de préférences (référent uniquement)."""
    extra = set(body) - {"preferences", "reason"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    with unit_of_work(request, write=True) as u:
        p = projects.get_project(u.conn, u.p, project)
        row = project_preferences.publish(
            u.conn,
            u.p,
            p,
            preferences=body.get("preferences") or {},
            reason=str(body.get("reason") or ""),
        )
        return project_preferences.public_view(row) or {}
