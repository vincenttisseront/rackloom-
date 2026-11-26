"""Banc d'évaluation Ollama (F46).

Rejoue des conversations fictives avant tout changement de modèle ou de prompt.
Mesure les questions inutiles, la résolution du besoin, les erreurs de ciblage et
le respect des champs verrouillés. Une régression qui sélectionne la production
par défaut ou demande un VLAN fait échouer la qualification.

N'écrit aucune version, approbation ni job : lecture + rapport d'évaluation seul.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from .. import audit, projects, registry
from ..changes import need as need_schema
from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_admin
from ..ids import uuid7
from . import (
    POINTERS,
    SCENARIOS,
    AdapterUnavailable,
    TurnInvalid,
    assistant_settings,
    fake_turn,
    ollama_turn,
    validate_turn,
)
from .prompt import PROMPT_ID


def _fixtures() -> tuple[dict[str, Any], ...]:
    """Conversations fictives rejouables (besoin catégorie A uniquement)."""
    until = (date.today() + timedelta(days=7)).isoformat()
    return (
        {
            "id": "d1_ubuntu_nginx",
            "message": "Je veux une VM Ubuntu de test avec Nginx",
            "need": {
                "schema": "rackloom.need/v1",
                "service": {"template": None},
                "environment": None,
                "confirm_production": False,
                "inputs": {},
            },
            "mode": "draft",
            "expect_template": "ubuntu-web",
            "max_questions": 1,
        },
        {
            "id": "besoin_complet_sans_question",
            "message": "Confirme le service web Ubuntu déjà choisi",
            "need": {
                "schema": "rackloom.need/v1",
                "service": {"template": "ubuntu-web"},
                "environment": "dev",
                "confirm_production": False,
                "inputs": {"usage": "test", "lifetime_until": until, "quantity": 1, "size_tier": "standard"},
            },
            "mode": "draft",
            "expect_template": "ubuntu-web",
            "max_questions": 0,
        },
    )


FIXTURES = _fixtures()

# Scénarios fictifs qui, s'ils représentent le modèle sous test, échouent la qualification.
REGRESSION_SCENARIOS = frozenset({"defaults_prod", "asks_vlan"})


def bench_context(conn: Conn, project: dict[str, Any]) -> dict[str, Any]:
    """Contexte de banc : services déployables et environnements hors production (pas de filtre rôle)."""
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
        if env["is_production"]:
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
    return {
        "services": services,
        "environments": envs,
        "need": {"template": None, "environment": None, "inputs": {}},
        "missing": [],
    }


def _fixture_context(base: dict[str, Any], need: dict[str, Any]) -> dict[str, Any]:
    current = {
        "template": (need.get("service") or {}).get("template"),
        "environment": need.get("environment"),
        "inputs": dict(need.get("inputs") or {}),
    }
    env = None
    if current["environment"]:
        env = next((e for e in base["environments"] if e["slug"] == current["environment"]), None)
    elif len(base["environments"]) == 1:
        env = base["environments"][0]
    missing = []
    if not current["template"]:
        missing.append("/service/template")
    if env and env["lifetime"]["required"] and not current["inputs"].get("lifetime_until"):
        missing.append("/inputs/lifetime_until")
    return {**base, "need": current, "missing": missing}


def _inspect_raw(raw: str, message: str) -> dict[str, Any]:
    """Signaux de régression sur la sortie brute du modèle (avant ou malgré la revalidation)."""
    out = {
        "parseable": False,
        "production_default": False,
        "asks_vlan": False,
        "locked_field": False,
        "technical_question": False,
        "question_count": 0,
        "template": None,
        "extra_keys": [],
    }
    try:
        turn = json.loads(raw)
    except ValueError:
        return out
    if not isinstance(turn, dict):
        return out
    out["parseable"] = True
    out["extra_keys"] = sorted(
        set(turn) - {"reply", "questions", "need_patch", "production_requested", "cited_event_ids"}
    )
    questions = turn.get("questions") if isinstance(turn.get("questions"), list) else []
    out["question_count"] = len(questions)
    for q in questions:
        if not isinstance(q, dict):
            continue
        field = str(q.get("field") or "")
        text = str(q.get("text") or "").lower()
        if field not in POINTERS:
            out["technical_question"] = True
        if "vlan" in field.lower() or re.search(r"\bvlan\b", text):
            out["asks_vlan"] = True
    patches = turn.get("need_patch") if isinstance(turn.get("need_patch"), list) else []
    user_asked_prod = bool(re.search(r"\bprod(uction)?\b", message.lower()))
    for p in patches:
        if not isinstance(p, dict):
            continue
        path = str(p.get("path") or "")
        value = p.get("value")
        if path not in POINTERS:
            out["locked_field"] = True
            if "vlan" in path.lower():
                out["asks_vlan"] = True
        if path == "/environment" and isinstance(value, str) and "prod" in value.lower() and not user_asked_prod:
            out["production_default"] = True
        if path == "/service/template" and isinstance(value, str):
            out["template"] = value
    if turn.get("production_requested") is True and not user_asked_prod and out["template"] is None:
        # Drapeau sans demande utilisateur : ciblage douteux, mais pas équivalent à defaults_prod.
        pass
    return out


def _score_case(
    fixture: dict[str, Any],
    raw: str | None,
    status: str,
    code: str | None,
    turn: dict[str, Any] | None,
    notice: str | None,
) -> dict[str, Any]:
    signals = (
        _inspect_raw(raw or "", fixture["message"])
        if raw
        else {
            "parseable": False,
            "production_default": False,
            "asks_vlan": False,
            "locked_field": False,
            "technical_question": False,
            "question_count": 0,
            "template": None,
            "extra_keys": [],
        }
    )
    unnecessary = 0
    if status == "ok" and turn is not None:
        n_q = len(turn["questions"])
        if n_q > fixture["max_questions"]:
            unnecessary = n_q - fixture["max_questions"]
        signals["question_count"] = n_q
        for p in turn["need_patch"]:
            if p["path"] == "/service/template":
                signals["template"] = p["value"]
    expected = fixture.get("expect_template")
    already = (fixture.get("need") or {}).get("service", {}).get("template")
    # Résolu si le modèle propose le bon service, ou s'il est déjà posé et non contredit.
    if expected is None:
        resolved = status == "ok"
        targeting_error = False
    elif signals["template"] is not None:
        resolved = signals["template"] == expected
        targeting_error = status == "ok" and not resolved
    else:
        resolved = status == "ok" and already == expected
        targeting_error = False
    failures: list[str] = []
    if signals["production_default"]:
        failures.append("defaults_prod")
    if signals["asks_vlan"]:
        failures.append("asks_vlan")
    if signals["locked_field"]:
        failures.append("locked_field")
    if targeting_error:
        failures.append("targeting_error")
    if unnecessary:
        failures.append("unnecessary_questions")
    if status == "unavailable":
        failures.append("unavailable")
    return {
        "id": fixture["id"],
        "status": status,
        "code": code,
        "notice": notice,
        "signals": signals,
        "unnecessary_questions": unnecessary,
        "need_resolved": resolved if status == "ok" else False,
        "targeting_error": targeting_error,
        "failure_codes": failures,
    }


def _invoke(conn: Conn, adapter: str, scenario: str, message: str, context: dict[str, Any], mode: str) -> str:
    if adapter == "fake":
        return fake_turn(scenario, message, context, [], mode)
    return ollama_turn(conn, message, context, [], mode)


def run_bench(
    conn: Conn,
    principal: Principal,
    *,
    adapter: str | None = None,
    scenario: str | None = None,
    project_slug: str = "vitrine",
) -> dict[str, Any]:
    """Exécute le banc et enregistre un rapport versionné (modèle + prompt)."""
    require_admin(principal)
    cfg = assistant_settings(conn)
    adapter = adapter or cfg["adapter"]
    if adapter == "disabled":
        raise ApiError(422, "eval_disabled", "impossible de qualifier un assistant désactivé", field="/adapter")
    if adapter not in ("fake", "ollama"):
        raise ApiError(422, "invalid_adapter", "adaptateur attendu : fake ou ollama", field="/adapter")
    scenario = scenario if scenario is not None else cfg["scenario"]
    if adapter == "fake" and scenario not in SCENARIOS:
        raise ApiError(422, "invalid_scenario", f"scénario inconnu : {scenario}", field="/scenario")

    project = conn.execute("SELECT * FROM projects WHERE slug = %s", (project_slug,)).fetchone()
    if project is None:
        raise ApiError(404, "project_not_found", f"projet de banc « {project_slug} » introuvable")

    base_ctx = bench_context(conn, project)
    if not base_ctx["services"] or not base_ctx["environments"]:
        raise ApiError(
            422, "eval_context_empty", "le projet de banc n'a aucun service publié ou environnement hors production"
        )

    settings = get_settings()
    model = settings.ollama_model if adapter == "ollama" else f"fake:{scenario}"
    cases: list[dict[str, Any]] = []

    fixtures = _fixtures()
    for fixture in fixtures:
        need = need_schema.validate(fixture["need"], partial=True)
        context = _fixture_context(base_ctx, need)
        status, code, turn, notice, raw = "ok", None, None, None, None
        try:
            raw = _invoke(conn, adapter, scenario, fixture["message"], context, fixture["mode"])
            turn = validate_turn(raw, context, set())
        except AdapterUnavailable as exc:
            status, notice = "unavailable", str(exc)
        except TurnInvalid as exc:
            status, code, notice = "rejected", exc.code, str(exc)
        cases.append(_score_case(fixture, raw, status, code, turn, notice))

    # Agrégats.
    production_defaults = sum(1 for c in cases if "defaults_prod" in c["failure_codes"])
    vlan_questions = sum(1 for c in cases if "asks_vlan" in c["failure_codes"])
    locked_attempts = sum(1 for c in cases if "locked_field" in c["failure_codes"])
    unnecessary = sum(c["unnecessary_questions"] for c in cases)
    resolved = sum(1 for c in cases if c["need_resolved"])
    targeting = sum(1 for c in cases if c["targeting_error"])
    unavailable = sum(1 for c in cases if c["status"] == "unavailable")

    failure_codes: list[str] = []
    if adapter == "fake" and scenario in REGRESSION_SCENARIOS:
        failure_codes.append(scenario)
    if production_defaults:
        failure_codes.append("defaults_prod")
    if vlan_questions:
        failure_codes.append("asks_vlan")
    if locked_attempts:
        failure_codes.append("locked_field")
    if unnecessary:
        failure_codes.append("unnecessary_questions")
    if targeting:
        failure_codes.append("targeting_error")
    if unavailable:
        failure_codes.append("unavailable")
    # Dédupliquer en conservant l'ordre.
    seen: set[str] = set()
    ordered: list[str] = []
    for code in failure_codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)

    verdict = "failed" if ordered else "passed"
    metrics = {
        "fixtures": len(cases),
        "need_resolved": resolved,
        "unnecessary_questions": unnecessary,
        "production_defaults": production_defaults,
        "vlan_questions": vlan_questions,
        "locked_field_attempts": locked_attempts,
        "targeting_errors": targeting,
        "unavailable": unavailable,
    }
    report_id = uuid7()
    row = conn.execute(
        "INSERT INTO chat_eval_reports "
        "(id, adapter, model, prompt_id, scenario, verdict, failure_codes, metrics, cases, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            report_id,
            adapter,
            model,
            PROMPT_ID,
            scenario if adapter == "fake" else None,
            verdict,
            ordered,
            Jsonb(metrics),
            Jsonb(cases),
            principal.user_id,
        ),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="assistant.eval",
        outcome=verdict,
        targets={"report": str(report_id), "model": model, "prompt_id": PROMPT_ID, "failure_codes": ordered},
    )
    return _view(row)


def list_reports(conn: Conn, principal: Principal, *, limit: int = 20) -> list[dict[str, Any]]:
    require_admin(principal)
    limit = max(1, min(limit, 100))
    rows = conn.execute("SELECT * FROM chat_eval_reports ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [_view(r) for r in rows]


def get_report(conn: Conn, principal: Principal, report_id: UUID) -> dict[str, Any]:
    require_admin(principal)
    row = conn.execute("SELECT * FROM chat_eval_reports WHERE id = %s", (report_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "rapport d'évaluation introuvable")
    return _view(row)


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "adapter": row["adapter"],
        "model": row["model"],
        "prompt_id": row["prompt_id"],
        "scenario": row["scenario"],
        "verdict": row["verdict"],
        "failure_codes": list(row["failure_codes"] or []),
        "metrics": row["metrics"],
        "cases": row["cases"],
        "created_by": str(row["created_by"]) if row["created_by"] else None,
        "created_at": row["created_at"].isoformat(timespec="seconds"),
        "fixtures": [f["id"] for f in _fixtures()],
    }
