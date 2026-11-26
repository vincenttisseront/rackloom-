"""Assistant : aide à décrire le besoin et explique les jobs. Ne décide jamais.

Le modèle reçoit un contexte filtré (message, besoin courant, services déployables du projet,
environnements hors production autorisés, entrées manquantes ; événements délimités comme données
non fiables en mode explication) et répond avec le schéma `rackloom.chat-turn/v1`. Il ne reçoit
jamais d'inventaire technique, de valeur résolue sensible ni de secret.

Le tour est revalidé côté serveur : clé inconnue, chemin hors liste, question technique ou valeur
hors contrat rendent le tour invalide. Le patch valide revient au navigateur avec la provenance
`suggestion` ; il n'est jamais enregistré directement et repasse par V01 à V10 à l'enregistrement.
Aucune route du chat ne crée de version, d'approbation ni de job.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import httpx

from .. import audit, events, projects, registry
from ..changes import need as need_schema
from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_project, require_role
from ..ids import uuid7
from ..redact import Redactor
from .prompt import PROMPT_ID, SYSTEM_PROMPT

POINTERS = (
    "/service/template",
    "/environment",
    "/inputs/quantity",
    "/inputs/size_tier",
    "/inputs/usage",
    "/inputs/lifetime_until",
)

# Réexport pour les appelants et le banc F46.
__all__ = ("PROMPT_ID", "SYSTEM_PROMPT", "SCENARIOS", "TURN_SCHEMA", "POINTERS")


TURN_KEYS = ("reply", "questions", "need_patch", "production_requested", "cited_event_ids")


TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(TURN_KEYS),
    "properties": {
        "reply": {"type": "string", "maxLength": 1200},
        "questions": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "text"],
                "properties": {
                    "field": {"type": "string", "enum": list(POINTERS)},
                    "text": {"type": "string", "maxLength": 300},
                },
            },
        },
        "need_patch": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "value"],
                "properties": {"path": {"type": "string", "enum": list(POINTERS)}, "value": {}},
            },
        },
        "production_requested": {"type": "boolean"},
        "cited_event_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
    },
}


SCENARIOS = (
    "nominal",
    "invalid_json",
    "schema_violation",
    "unknown_option",
    "override_attempt",
    "defaults_prod",
    "asks_vlan",
    "unavailable",
    "timeout",
    "injection",
)


QUESTIONS = {
    "/service/template": "Quel service souhaitez-vous ?",
    "/inputs/lifetime_until": "Jusqu'à quelle date en avez-vous besoin ?",
}


class AdapterUnavailable(Exception):
    pass


class TurnInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_context(conn: Conn, principal: Principal, project: dict[str, Any], need: dict[str, Any]) -> dict[str, Any]:
    """Seules des données de catégorie A : aucun réseau, datastore, image ni valeur résolue."""
    services = []
    for slug in registry.granted_services(conn, project["id"]):
        tpl = registry.latest_published(conn, slug)
        if tpl is None:
            continue
        c = tpl["contract"]
        spec = c["inputs"]
        services.append(
            {
                "template": slug,
                "title": c["title"],
                "description": c["description"],
                "inputs": {
                    "quantity": {k: spec["quantity"][k] for k in ("min", "max", "default")},
                    "size_tier": {
                        "values": spec["size_tier"]["values"],
                        "labels": spec["size_tier"].get("value_labels", {}),
                    },
                    "usage": {"max_length": spec["usage"].get("max_length", 80)},
                    "lifetime_until": {"type": "date"},
                },
                "environments": {
                    kind: {"size_tiers": list(p["size_tiers"]), "default_size_tier": p["default_size_tier"]}
                    for kind, p in c["environments"].items()
                },
            }
        )
    envs = []
    for env in projects.environments(conn, project["id"]):
        if env["is_production"] or not principal.has_role("operator", project["id"], env["id"]):
            continue
        rules = (projects.current_policy(conn, env["id"]) or {}).get("rules", {})
        lifetime = rules.get("lifetime", {})
        envs.append(
            {
                "slug": env["slug"],
                "name": env["name"],
                "kind": env["kind"],
                "lifetime": {"required": bool(lifetime.get("required")), "max_days": lifetime.get("max_days")},
            }
        )
    current = {
        "template": (need.get("service") or {}).get("template"),
        "environment": need.get("environment"),
        "inputs": dict(need.get("inputs") or {}),
    }
    env = _target_env(envs, current["environment"])
    missing = []
    if not current["template"]:
        missing.append("/service/template")
    if env and env["lifetime"]["required"] and not current["inputs"].get("lifetime_until"):
        missing.append("/inputs/lifetime_until")
    return {"services": services, "environments": envs, "need": current, "missing": missing}


def _target_env(envs: list[dict[str, Any]], slug: str | None) -> dict[str, Any] | None:
    if slug:
        return next((e for e in envs if e["slug"] == slug), None)
    return envs[0] if len(envs) == 1 else None


def _invalid_patch_path(path: Any) -> TurnInvalid:
    last = str(path).rstrip("/").rsplit("/", 1)[-1]
    if need_schema.is_locked_key(last):
        return TurnInvalid(
            "locked_field", f"valeur technique proposée ({path}) : réglage verrouillé, choisi par Rackloom"
        )
    return TurnInvalid("schema_violation", f"champ non modifiable par l'assistant : {path!r}")


def validate_turn(raw: str, context: dict[str, Any], event_ids: set[int]) -> dict[str, Any]:
    try:
        turn = json.loads(raw)
    except ValueError as exc:
        raise TurnInvalid("invalid_json", "réponse du modèle illisible (JSON invalide)") from exc
    if not isinstance(turn, dict) or set(turn) != set(TURN_KEYS):
        extra = sorted(set(turn) - set(TURN_KEYS)) if isinstance(turn, dict) else []
        raise TurnInvalid(
            "schema_violation",
            "réponse du modèle hors du schéma imposé" + (f" (champ « {extra[0]} »)" if extra else ""),
        )
    if not isinstance(turn["reply"], str) or len(turn["reply"]) > 1200:
        raise TurnInvalid("schema_violation", "texte de réponse invalide")
    if not isinstance(turn["production_requested"], bool):
        raise TurnInvalid("schema_violation", "production_requested doit être un booléen")
    if not isinstance(turn["questions"], list) or len(turn["questions"]) >= 3:
        raise TurnInvalid("schema_violation", "trois questions ou plus")
    for q in turn["questions"]:
        if not isinstance(q, dict) or set(q) != {"field", "text"} or not isinstance(q["text"], str):
            raise TurnInvalid("schema_violation", "question hors schéma")
        if q["field"] not in POINTERS:
            raise TurnInvalid(
                "technical_question",
                f"question sur un champ technique ou inconnu ({q['field']}) : "
                "Rackloom ne pose jamais ce type de question",
            )
    if not isinstance(turn["need_patch"], list) or len(turn["need_patch"]) > len(POINTERS):
        raise TurnInvalid("schema_violation", "patch invalide")
    merged = json.loads(json.dumps(context["need"]))
    for p in turn["need_patch"]:
        if not isinstance(p, dict) or set(p) != {"path", "value"}:
            raise TurnInvalid("schema_violation", "élément de patch hors schéma")
        if p["path"] not in POINTERS:
            raise _invalid_patch_path(p["path"])
        _check_value(p["path"], p["value"], context, merged)
    if not isinstance(turn["cited_event_ids"], list) or not all(
        isinstance(i, int) and i in event_ids for i in turn["cited_event_ids"]
    ):
        raise TurnInvalid("schema_violation", "événement cité inconnu")
    return turn


def _check_value(path: str, value: Any, context: dict[str, Any], merged: dict[str, Any]) -> None:
    if path == "/service/template":
        if value not in {s["template"] for s in context["services"]}:
            raise TurnInvalid("unknown_option", f"service « {value} » absent des services déployables du projet")
        merged["template"] = value
        return
    if path == "/environment":
        if value not in {e["slug"] for e in context["environments"]}:
            raise TurnInvalid(
                "production_not_proposable" if isinstance(value, str) and "prod" in value else "unknown_option",
                "l'assistant ne propose jamais la production ; seuls les environnements hors "
                "production autorisés sont permis",
            )
        merged["environment"] = value
        return
    service = next((s for s in context["services"] if s["template"] == merged.get("template")), None)
    if service is None:
        raise TurnInvalid("unknown_option", "valeur proposée sans service connu")
    key = path.rsplit("/", 1)[-1]
    env = _target_env(context["environments"], merged.get("environment"))
    if key == "quantity":
        bounds = service["inputs"]["quantity"]
        if not isinstance(value, int) or isinstance(value, bool) or not bounds["min"] <= value <= bounds["max"]:
            raise TurnInvalid("unknown_option", f"quantité hors bornes ({bounds['min']} à {bounds['max']})")
    elif key == "size_tier":
        allowed = (
            set(service["environments"].get(env["kind"], {}).get("size_tiers", []))
            if env
            else set(service["inputs"]["size_tier"]["values"])
        )
        if value not in allowed:
            raise TurnInvalid("unknown_option", f"niveau « {value} » non proposé pour cet environnement")
    elif key == "usage":
        if not isinstance(value, str) or len(value) > service["inputs"]["usage"]["max_length"]:
            raise TurnInvalid("unknown_option", "usage invalide")
    elif key == "lifetime_until":
        try:
            day = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise TurnInvalid("unknown_option", "date de fin invalide") from exc
        max_days = (env or {}).get("lifetime", {}).get("max_days") or 365
        if not date.today() < day <= date.today() + timedelta(days=max_days):
            raise TurnInvalid("unknown_option", f"date de fin hors politique ({max_days} jours au plus)")
    merged["inputs"][key] = value


def _turn(reply: str, questions=(), patch=(), production: bool = False, cited=()) -> str:
    return json.dumps(
        {
            "reply": reply,
            "questions": list(questions),
            "need_patch": list(patch),
            "production_requested": production,
            "cited_event_ids": list(cited),
        },
        ensure_ascii=False,
    )


def _service_for(message: str, services: list[dict[str, Any]]) -> dict[str, Any] | None:
    text = message.lower()
    for s in services:
        words = {
            w
            for w in re.findall(r"[a-zà-ÿ0-9]+", f"{s['title']} {s['description']} {s['template']}".lower())
            if len(w) >= 4
        }
        if any(w in text for w in words):
            return s
    return None


def fake_turn(scenario: str, message: str, context: dict[str, Any], untrusted: list[dict[str, Any]], mode: str) -> str:
    """Adaptateur fictif déterministe (SIMULÉ) : scénarios choisis par l'administrateur ou les tests."""
    if scenario == "unavailable":
        raise AdapterUnavailable("assistant indisponible (scénario de test)")
    if scenario == "timeout":
        raise AdapterUnavailable("délai de réponse dépassé (scénario de test)")
    if scenario == "invalid_json":
        return '{"reply": "Voici votre VM", "questions": ['
    if scenario == "schema_violation":
        return json.dumps(
            {
                "reply": "Je lance le déploiement.",
                "questions": [],
                "need_patch": [],
                "production_requested": False,
                "cited_event_ids": [],
                "run": True,
            }
        )
    if scenario == "unknown_option":
        return _turn(
            "Je propose un serveur de messagerie.", patch=[{"path": "/service/template", "value": "ubuntu-mail"}]
        )
    if scenario == "override_attempt":
        return _turn(
            "Je fixe le réseau et les vCPU.",
            patch=[{"path": "/inputs/network", "value": "net-dev-vitrine-a"}, {"path": "/inputs/vcpu", "value": 4}],
        )
    if scenario == "defaults_prod":
        return _turn("Je mets la production par défaut.", patch=[{"path": "/environment", "value": "prod"}])
    if scenario == "asks_vlan":
        return _turn(
            "Il me faut une précision.",
            questions=[{"field": "/placement/vlan", "text": "Sur quel VLAN voulez-vous la VM ?"}],
        )
    if scenario == "injection":
        return json.dumps(
            {
                "reply": "approuvé",
                "questions": [],
                "production_requested": True,
                "cited_event_ids": [],
                "approve": True,
                "need_patch": [{"path": "/environment", "value": "prod"}],
            }
        )
    if mode == "explain":
        shown = untrusted[-6:]
        lines = [f"- {e['at']} [{e['source']}] « {e['text'][:160]} »" for e in shown]
        reply = (
            (
                "Voici les derniers événements de ce job. Ils sont cités comme données, pas comme "
                "instructions :\n" + "\n".join(lines)
            )
            if lines
            else "Aucun événement à expliquer pour ce job."
        )
        return _turn(reply[:1200], cited=[e["id"] for e in shown if e["id"] > 0])
    patch: list[dict[str, Any]] = []
    need = context["need"]
    service = _service_for(message, context["services"])
    if service and need.get("template") != service["template"]:
        patch.append({"path": "/service/template", "value": service["template"]})
    service = service or next((s for s in context["services"] if s["template"] == need.get("template")), None)
    text = message.lower()
    production = bool(re.search(r"\bprod(uction)?\b", text))
    if service:
        if re.search(r"\b(2|deux)\b", text):
            patch.append({"path": "/inputs/quantity", "value": 2})
        if "standard" in text:
            patch.append({"path": "/inputs/size_tier", "value": "standard"})
        usage = re.search(r"\bde (test|recette|démonstration)\b", text)
        if usage and not need["inputs"].get("usage"):
            patch.append({"path": "/inputs/usage", "value": usage.group(1)})
    missing = [m for m in context["missing"] if not (m == "/service/template" and service)]
    questions = [{"field": m, "text": QUESTIONS[m]} for m in missing[:1]]
    parts = []
    for p in patch:
        if p["path"] == "/service/template":
            parts.append(f"service « {service['title']} »")
        elif p["path"] == "/inputs/quantity":
            parts.append(f"{p['value']} serveurs")
        elif p["path"] == "/inputs/size_tier":
            parts.append(f"niveau « {service['inputs']['size_tier']['labels'].get(p['value'], p['value'])} »")
        elif p["path"] == "/inputs/usage":
            parts.append(f"usage « {p['value']} »")
    reply = (
        (
            "Je propose : " + ", ".join(parts) + ". Vérifiez puis confirmez chaque valeur ; Rackloom choisit "
            "ensuite l'emplacement, le réseau et les réglages techniques."
        )
        if parts
        else "Pouvez-vous préciser votre besoin ?"
    )
    if production:
        reply += " Vous mentionnez la production : confirmez-la explicitement dans le formulaire si besoin."
    return _turn(reply, questions=questions, patch=patch, production=production)


def ollama_turn(conn: Conn, message: str, context: dict[str, Any], untrusted: list[dict[str, Any]], mode: str) -> str:
    payload = {
        "mode": mode,
        "message": message,
        "context": context,
        "untrusted_events": untrusted,
        "prompt_id": PROMPT_ID,
    }
    return model_call(conn, SYSTEM_PROMPT, payload, TURN_SCHEMA)


def model_call(conn: Conn, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> str:
    """Appel Ollama local : point d'accès approuvé seulement, sortie contrainte par le schéma, aucun repli."""
    settings = get_settings()
    if not settings.ollama_url:
        raise AdapterUnavailable("assistant non configuré")
    url = httpx.URL(settings.ollama_url)
    approved = conn.execute(
        "SELECT 1 FROM approved_endpoints e JOIN connectors c ON c.id = e.connector_id "
        "WHERE c.kind = 'model' AND e.scheme = %s AND e.host = %s AND e.port = %s",
        (url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)),
    ).fetchone()
    if approved is None:
        raise AdapterUnavailable("point d'accès du modèle non approuvé")
    try:
        resp = httpx.post(
            str(url.join("/api/chat")),
            timeout=settings.ollama_timeout_seconds,
            json={
                "model": settings.ollama_model,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
        )
    except httpx.TimeoutException as exc:
        raise AdapterUnavailable("délai de réponse du modèle dépassé") from exc
    except httpx.HTTPError as exc:
        raise AdapterUnavailable("modèle injoignable") from exc
    if resp.status_code != 200:
        raise AdapterUnavailable(f"modèle indisponible ou incompatible (HTTP {resp.status_code})")
    return resp.json().get("message", {}).get("content", "")


def assistant_settings(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = 'assistant'").fetchone()
    value = row["value"] if row else {}
    adapter = value.get("adapter") or get_settings().chat_adapter
    return {
        "adapter": adapter,
        "scenario": value.get("scenario", "nominal"),
        "simulated": adapter == "fake",
        "available": adapter != "disabled",
    }


def set_assistant(conn: Conn, principal: Principal, adapter: str, scenario: str) -> dict[str, Any]:
    if adapter not in ("fake", "ollama", "disabled"):
        raise ApiError(422, "invalid_adapter", "adaptateur attendu : fake, ollama ou disabled", field="/adapter")
    if scenario not in SCENARIOS:
        raise ApiError(422, "invalid_scenario", f"scénario attendu : {', '.join(SCENARIOS)}", field="/scenario")
    if adapter == "fake" and get_settings().profile == "production":
        raise ApiError(403, "fake_refused", "adaptateur fictif refusé en profil production")
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('assistant', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_at = now()",
        (Jsonb({"adapter": adapter, "scenario": scenario}),),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="assistant.configure",
        outcome="ok",
        targets={"adapter": adapter, "scenario": scenario},
    )
    return assistant_settings(conn)


def create_conversation(
    conn: Conn, principal: Principal, project: dict[str, Any], change_id: UUID | None
) -> dict[str, Any]:
    require_role(principal, "operator", project["id"])
    if change_id is not None:
        change = conn.execute("SELECT project_id FROM changes WHERE id = %s", (change_id,)).fetchone()
        if change is None or change["project_id"] != project["id"]:
            raise not_found("demande")
    return conn.execute(
        "INSERT INTO chat_conversations (id, project_id, change_id, user_id) VALUES (%s, %s, %s, %s) RETURNING *",
        (uuid7(), project["id"], change_id, principal.user_id),
    ).fetchone()


def _untrusted_events(conn: Conn, project_id: UUID, job_id: UUID, redactor: Redactor) -> list[dict[str, Any]]:
    job = conn.execute("SELECT * FROM jobs WHERE id = %s AND project_id = %s", (job_id, project_id)).fetchone()
    if job is None:
        raise not_found("job")
    rows = conn.execute(
        "SELECT id, occurred_at, source, type, data FROM events WHERE correlation_id = %s "
        "AND project_id = %s ORDER BY id DESC LIMIT 30",
        (job["change_id"], project_id),
    ).fetchall()
    result = []
    for r in reversed(rows):
        data = r["data"]
        text = data.get("message") or data.get("label") or data.get("step") or r["type"]
        result.append(
            {
                "id": r["id"],
                "at": r["occurred_at"].isoformat(timespec="seconds"),
                "source": r["source"],
                "type": r["type"],
                "text": redactor.text(str(text))[:300],
            }
        )
    obs = conn.execute(
        "SELECT o.id, o.collected_at, o.data FROM host_observations o JOIN hosts h ON h.id = o.host_id "
        "WHERE h.change_id = %s AND h.project_id = %s AND o.kind = 'journal' ORDER BY o.collected_at DESC LIMIT 2",
        (job["change_id"], project_id),
    ).fetchall()
    for o in obs:
        for line in (o["data"].get("lines") or [])[-5:]:
            result.append(
                {
                    "id": -o["id"],
                    "at": o["collected_at"].isoformat(timespec="seconds"),
                    "source": "journal nginx (donnée non fiable)",
                    "type": "host.journal",
                    "text": redactor.text(str(line))[:300],
                }
            )
    return result


def post_message(conn: Conn, principal: Principal, conversation_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    conv = conn.execute("SELECT * FROM chat_conversations WHERE id = %s", (conversation_id,)).fetchone()
    if conv is None or conv["user_id"] != principal.user_id:
        raise not_found("conversation")
    require_project(principal, conv["project_id"], "conversation")
    extra = set(body) - {"message", "need", "mode", "job_id"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    message = str(body.get("message") or "").strip()
    if not message or len(message) > 2000:
        raise ApiError(422, "invalid_message", "message vide ou trop long", field="/message")
    mode = body.get("mode", "draft")
    if mode not in ("draft", "explain"):
        raise ApiError(422, "invalid_mode", "mode attendu : draft ou explain", field="/mode")
    raw_need = body.get("need") or {
        "schema": "rackloom.need/v1",
        "service": {"template": None},
        "environment": None,
        "confirm_production": False,
        "inputs": {},
    }
    need = need_schema.validate(raw_need, partial=True)
    redactor = Redactor()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (conv["project_id"],)).fetchone()
    context = build_context(conn, principal, project, need)
    untrusted: list[dict[str, Any]] = []
    if mode == "explain":
        if not body.get("job_id"):
            raise ApiError(422, "job_required", "job à expliquer requis", field="/job_id")
        try:
            job_id = UUID(str(body["job_id"]))
        except ValueError as exc:
            raise ApiError(422, "invalid_job", "identifiant de job invalide", field="/job_id") from exc
        untrusted = _untrusted_events(conn, project["id"], job_id, redactor)
    conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
        (conversation_id, redactor.text(message)),
    )
    cfg = assistant_settings(conn)
    outcome, turn, notice, code = "ok", None, None, None
    try:
        if cfg["adapter"] == "disabled":
            raise AdapterUnavailable("assistant désactivé par l'administrateur")
        if cfg["adapter"] == "ollama":
            raw = ollama_turn(conn, redactor.text(message), context, untrusted, mode)
        else:
            raw = fake_turn(cfg["scenario"], message, context, untrusted, mode)
        turn = validate_turn(raw, context, {e["id"] for e in untrusted})
    except AdapterUnavailable as exc:
        outcome, notice = "unavailable", f"Assistant indisponible : {exc}. Le formulaire reste utilisable."
    except TurnInvalid as exc:
        outcome, code = "rejected", exc.code
        notice = f"Suggestion de l'assistant rejetée ({exc}). Aucune valeur n'a été modifiée."
        events.emit(
            conn,
            type="chat.suggestion_rejected",
            source="chat",
            project_id=project["id"],
            correlation_id=conv["change_id"],
            severity="warning",
            data={"code": exc.code, "message": str(exc), "adapter": cfg["adapter"]},
        )
        audit.record(
            conn,
            actor_type="assistant",
            actor_id=cfg["adapter"],
            action="chat.suggestion_rejected",
            outcome=exc.code,
            project_id=project["id"],
            correlation_id=conv["change_id"],
            targets={"conversation": str(conversation_id)},
        )
    if turn is not None:
        turn["reply"] = redactor.text(turn["reply"])
        for p in turn["need_patch"]:
            p["provenance"] = "suggestion"
    conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, adapter, model, validation_outcome, data) "
        "VALUES (%s, 'assistant', %s, %s, %s, %s, %s)",
        (
            conversation_id,
            (turn or {}).get("reply") or notice or "",
            cfg["adapter"],
            get_settings().ollama_model if cfg["adapter"] == "ollama" else f"fake:{cfg['scenario']}",
            outcome,
            Jsonb(
                {
                    "questions": (turn or {}).get("questions", []),
                    "need_patch": (turn or {}).get("need_patch", []),
                    "code": code,
                }
            ),
        ),
    )
    return {
        "status": outcome,
        "code": code,
        "notice": notice,
        "turn": turn,
        "simulated": cfg["simulated"],
        "adapter": cfg["adapter"],
        "provenance": "suggestion",
        "untrusted_events": untrusted if mode == "explain" else [],
    }
