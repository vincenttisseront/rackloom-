"""Atelier de templates (AT01) : brouillon d'auteur assisté, puis soumission à la revue.

Un brouillon dérive d'une version publiée (la « base ») : module Terraform, profil système,
environnements, placement, nommage et entrées en sont hérités tels quels. L'auteur ne règle que
la conception : titre, description, composants publiés et leurs paramètres dans les bornes
déclarées, politique de vagues. Les contrôles de santé se calculent depuis les contrats des
composants.

L'assistant propose des suggestions ; une suggestion n'est ni un réglage, ni une approbation, ni
un ordre. Seule une décision de l'auteur modifie le brouillon, par le même contrôle qu'une saisie
manuelle. Ce qui dépasse le catalogue devient une proposition d'extension à revoir, jamais
appliquée. Soumettre crée une version immuable `in_review` du registre ; publier reste un acte
distinct, fait par une autre personne (registry.change_status).
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import audit, chat, registry
from ..canonical import digest
from ..db import Conn, Jsonb
from ..errors import ApiError, conflict, forbidden, not_found
from ..identity.authz import Principal, require_author
from ..ids import uuid7
from ..redact import Redactor
from . import reference as refmod

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
TEXT_RE = re.compile(r"[\x00-\x1f\x7f<>]")
COMPONENT_PATH = re.compile(r"^/components/([a-z][a-z0-9-]{1,40})$")
PARAM_PATH = re.compile(r"^/components/([a-z][a-z0-9-]{1,40})/config/([a-z][a-z0-9_]{0,40})$")
DESIGN_KEYS = ("reply", "questions", "suggestions", "extensions")
DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(DESIGN_KEYS),
    "properties": {
        "reply": {"type": "string", "maxLength": 1200},
        "questions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "maxLength": 300}},
            },
        },
        "suggestions": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "value", "why"],
                "properties": {
                    "path": {"type": "string", "maxLength": 120},
                    "value": {},
                    "why": {"type": "string", "maxLength": 300},
                },
            },
        },
        "extensions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "subject", "summary"],
                "properties": {
                    "kind": {"type": "string", "enum": ["component_parameter", "new_component"]},
                    "subject": {"type": "string", "maxLength": 80},
                    "summary": {"type": "string", "maxLength": 600},
                },
            },
        },
    },
}
STATUS_LABELS = {"editing": "En conception", "in_review": "En revue", "published": "Publié", "abandoned": "Abandonné"}
SOURCE_LABELS = {
    "base": "hérité de la base",
    "author": "saisi par l'auteur",
    "assistant": "suggestion de l'assistant acceptée par l'auteur",
    "reference_host": "observé sur le serveur de référence, confirmé par l'auteur",
}
ASSISTANT_SYSTEM = (
    "Tu aides un auteur de templates Rackloom à concevoir un brouillon. Réponds uniquement avec le schéma JSON "
    "imposé. Tu proposes des suggestions que l'auteur acceptera ou refusera une par une ; tu ne publies, "
    "n'approuves et ne déploies rien. Chemins autorisés : /title, /description, /waves, /components/<slug> "
    '(valeur {"version": ...} pour ajouter, null pour retirer) et /components/<slug>/config/<paramètre>. '
    "Utilise seulement les composants, versions et paramètres du catalogue fourni, dans leurs bornes. Ne propose "
    "jamais de réseau, d'adresse, de placement, de taille, d'image, de module, de commande ni de secret. Si le "
    "besoin dépasse le catalogue, décris-le dans « extensions » (texte à revoir, jamais appliqué)."
)


class TurnInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- Lecture ---------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def catalog(conn: Conn) -> list[dict[str, Any]]:
    """Composants utilisables par l'atelier : validés ou publiés, avec leurs paramètres déclarés."""
    rows = conn.execute(
        "SELECT * FROM component_versions WHERE status IN ('validated', 'published') ORDER BY slug, version"
    ).fetchall()
    out = []
    for r in sorted(rows, key=lambda r: (r["slug"], registry.semver_key(r["version"]))):
        c = r["contract"]
        out.append(
            {
                "slug": r["slug"],
                "version": r["version"],
                "title": c["title"],
                "description": c.get("description", ""),
                "supports": c["supports"],
                "parameters": {
                    k: {
                        key: v[key]
                        for key in ("type", "default", "min", "max", "values", "label", "requires_component")
                        if key in v
                    }
                    for k, v in c["parameters"].items()
                },
                "not_covered": c.get("not_covered", []),
                "verification_level": r["verification_level"],
                "evidence": registry.evidence_summary(conn, r["id"]),
            }
        )
    return out


def _component_row(conn: Conn, slug: str, version: str) -> dict[str, Any] | None:
    row = registry.component(conn, slug, version)
    return row if row is not None and row["status"] in ("validated", "published") else None


def bases(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT DISTINCT slug FROM template_versions WHERE status = 'published' ORDER BY slug")
    out = []
    for r in rows.fetchall():
        tpl = registry.latest_published(conn, r["slug"])
        if tpl is not None:
            out.append({"slug": tpl["slug"], "version": tpl["version"], "title": tpl["contract"]["title"]})
    return out


def _draft(conn: Conn, draft_id: UUID, for_update: bool = False) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM template_drafts WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (draft_id,)
    ).fetchone()
    if row is None:
        raise not_found("brouillon")
    return row


def _visible(principal: Principal, draft: dict[str, Any]) -> bool:
    if draft["created_by"] == principal.user_id:
        return True
    if principal.has_role("template_publisher") and draft["status"] in ("in_review", "published"):
        return True
    return principal.is_platform_admin


def get_visible(conn: Conn, principal: Principal, draft_id: UUID, for_update: bool = False) -> dict[str, Any]:
    draft = _draft(conn, draft_id, for_update)
    if not _visible(principal, draft):
        raise not_found("brouillon")
    return draft


def _own(conn: Conn, principal: Principal, draft_id: UUID, row_version: Any) -> dict[str, Any]:
    """Brouillon modifiable : son auteur, rôle de conception, statut « en conception », version vue."""
    draft = get_visible(conn, principal, draft_id, for_update=True)
    require_author(principal)
    if draft["created_by"] != principal.user_id:
        raise forbidden("seul l'auteur modifie son brouillon")
    if draft["status"] != "editing":
        raise conflict("invalid_status", f"brouillon « {STATUS_LABELS[draft['status']]} » : non modifiable")
    if row_version != draft["row_version"]:
        raise conflict("stale_version", "le brouillon a changé : recharger")
    return draft


def list_drafts(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.*, u.display_name AS author FROM template_drafts d "
        "JOIN users u ON u.id = d.created_by ORDER BY d.updated_at DESC"
    ).fetchall()
    return [
        {
            "id": str(d["id"]),
            "slug": d["slug"],
            "version": d["version"],
            "status": d["status"],
            "status_label": STATUS_LABELS[d["status"]],
            "title": d["content"]["title"],
            "author": d["author"],
            "mine": d["created_by"] == principal.user_id,
            "updated_at": d["updated_at"].isoformat(),
        }
        for d in rows
        if _visible(principal, d)
    ]


# --- Contrat candidat ------------------------------------------------------------------------


def _base(conn: Conn, draft: dict[str, Any]) -> dict[str, Any]:
    row = registry.template_by_id(conn, draft["base_template_version_id"])
    if row is None:
        raise not_found("template de base")
    return row


def _config(comp: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    full = {k: v["default"] for k, v in comp["parameters"].items() if "default" in v}
    full.update(config)
    return full


def build_contract(conn: Conn, draft: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Contrat rackloom.template/v1 candidat : la base, plus la conception de l'auteur."""
    base = base or _base(conn, draft)
    content = draft["content"]
    contract = copy.deepcopy(base["contract"])
    contract.update(
        slug=draft["slug"], version=draft["version"], title=content["title"], description=content["description"]
    )
    contract["components"] = [
        {"component": c["component"], "version": c["version"], "config": dict(c["config"])}
        for c in content["components"]
    ]
    checks: list[dict[str, Any]] = []
    for c in content["components"]:
        row = _component_row(conn, c["component"], c["version"])
        if row is None:
            continue
        for check in registry.component_checks(row["contract"], _config(row["contract"], c["config"])):
            if check not in checks:
                checks.append(check)
    contract["health_checks"] = checks
    contract["deployment_policy"]["waves"] = dict(content["waves"])
    return contract


def problems(conn: Conn, draft: dict[str, Any]) -> list[dict[str, str]]:
    """Ce qui empêche la soumission, en clair. Vide : le contrat passe les contrôles du registre."""
    out: list[dict[str, str]] = []
    if not draft["content"]["components"]:
        out.append({"path": "/components", "message": "au moins un composant est requis"})
    for c in draft["content"]["components"]:
        if _component_row(conn, c["component"], c["version"]) is None:
            out.append(
                {
                    "path": f"/components/{c['component']}",
                    "message": f"composant {c['component']}@{c['version']} absent ou retiré du catalogue",
                }
            )
    pending = [s for s in draft["proposals"] if s["status"] == "pending"]
    if pending:
        out.append({"path": "/suggestions", "message": f"{len(pending)} suggestion(s) de l'assistant sans décision"})
    pending_ref = sum(
        1
        for analysis in draft.get("sources") or []
        if analysis.get("kind") == "reference_analysis"
        for item in analysis.get("items") or []
        if item.get("decidable") and item.get("status") == "pending"
    )
    if pending_ref:
        out.append(
            {
                "path": "/reference",
                "message": f"{pending_ref} proposition(s) du serveur de référence sans décision",
            }
        )
    if out:
        return out
    contract = build_contract(conn, draft)
    try:
        registry.validate_template(contract)
        registry.check_template_references(
            contract,
            lambda s, v: row["contract"] if (row := _component_row(conn, s, v)) else None,
            lambda k, s, v: registry.artifact(conn, k, s, v),
        )
    except registry.CatalogError as exc:
        out.append({"path": "/", "message": str(exc)})
    return out


def content_digest(content: dict[str, Any]) -> str:
    return digest(content)


# --- Modifications (auteur ou suggestion acceptée) -------------------------------------------


def _text(path: str, value: Any, low: int, high: int) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high or TEXT_RE.search(value):
        raise ApiError(422, "invalid_value", f"texte brut de {low} à {high} caractères attendu", field=path)
    return value.strip()


def apply_op(conn: Conn, content: dict[str, Any], path: Any, value: Any) -> dict[str, Any]:
    """Seuls les chemins de conception existent ; tout le reste (placement, réseau, taille, module…) est
    hérité de la base et refusé ici, quelle que soit la source."""
    if not isinstance(path, str):
        raise ApiError(422, "path_not_allowed", "chemin attendu", field="/path")
    out = copy.deepcopy(content)
    comps = out["components"]
    if path == "/title":
        out["title"] = _text(path, value, 3, 80)
    elif path == "/description":
        out["description"] = _text(path, value, 3, 200)
    elif path == "/waves":
        try:
            if not isinstance(value, dict):
                raise registry.CatalogError("objet attendu")
            registry.check_waves(value)
        except registry.CatalogError as exc:
            raise ApiError(422, "invalid_value", str(exc), field=path) from exc
        out["waves"] = dict(value)
    elif m := COMPONENT_PATH.match(path):
        slug = m.group(1)
        if value is None:
            if not any(c["component"] == slug for c in comps):
                raise ApiError(422, "invalid_value", f"composant {slug} absent du brouillon", field=path)
            out["components"] = [c for c in comps if c["component"] != slug]
        else:
            if not isinstance(value, dict) or set(value) != {"version"} or not isinstance(value["version"], str):
                raise ApiError(422, "invalid_value", 'valeur attendue : {"version": "x.y.z"} ou null', field=path)
            row = _component_row(conn, slug, value["version"])
            if row is None:
                raise ApiError(
                    422, "unknown_component", f"composant {slug}@{value['version']} absent du catalogue", field=path
                )
            existing = next((c for c in comps if c["component"] == slug), None)
            if existing is None:
                comps.append({"component": slug, "version": value["version"], "config": {}})
            else:
                params = row["contract"]["parameters"]
                existing["version"] = value["version"]
                existing["config"] = {
                    k: v
                    for k, v in existing["config"].items()
                    if k in params and registry.check_parameter(params[k], v) is None
                }
    elif m := PARAM_PATH.match(path):
        slug, name = m.groups()
        entry = next((c for c in comps if c["component"] == slug), None)
        if entry is None:
            raise ApiError(422, "invalid_value", f"composant {slug} absent du brouillon", field=path)
        row = _component_row(conn, slug, entry["version"])
        spec = (row["contract"]["parameters"] if row else {}).get(name)
        if spec is None:
            raise ApiError(
                422, "unknown_parameter", f"paramètre « {name} » non déclaré par {slug}@{entry['version']}", field=path
            )
        if spec["type"] == "secret_ref":
            raise ApiError(
                422, "path_not_allowed", "une référence de secret ne se règle pas dans l'atelier", field=path
            )
        error = registry.check_parameter(spec, value)
        if error is None and isinstance(value, str) and (len(value) > 200 or TEXT_RE.search(value)):
            error = "texte brut de 200 caractères au plus attendu"
        if error:
            raise ApiError(422, "invalid_value", f"{slug}.{name} : {error}", field=path)
        entry["config"][name] = value
    else:
        raise ApiError(
            422,
            "path_not_allowed",
            "réglage hors de la conception d'un template (hérité de la base, non modifiable dans l'atelier)",
            field=path,
        )
    return out


def _provenance(
    provenance: dict[str, Any],
    path: str,
    value: Any,
    source: str,
    principal: Principal,
    suggestion_id: str | None = None,
) -> dict[str, Any]:
    out = {k: v for k, v in provenance.items() if not (k == path or k.startswith(path + "/"))}
    if not (value is None and COMPONENT_PATH.match(path)):
        out[path] = {
            "source": source,
            "by": str(principal.user_id),
            "at": _now(),
            **({"suggestion": suggestion_id} if suggestion_id else {}),
        }
    return out


def _event(
    conn: Conn,
    draft_id: UUID,
    action: str,
    actor_type: str,
    actor_id: str | None,
    data: dict[str, Any],
    content: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO template_draft_events (draft_id, action, actor_type, actor_id, data, content_digest) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (draft_id, action, actor_type, actor_id, Jsonb(data), content_digest(content)),
    )


def _save(conn: Conn, draft: dict[str, Any], **fields: Any) -> dict[str, Any]:
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [
        Jsonb(v) if k in ("content", "provenance", "proposals", "questions", "sources") else v
        for k, v in fields.items()
    ]
    return conn.execute(
        f"UPDATE template_drafts SET {sets}, updated_at = now(), row_version = row_version + 1 "
        "WHERE id = %s RETURNING *",
        (*values, draft["id"]),
    ).fetchone()


def edit(conn: Conn, principal: Principal, draft_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    extra = set(body) - {"row_version", "ops"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    ops = body.get("ops")
    if not isinstance(ops, list) or not 1 <= len(ops) <= 20:
        raise ApiError(422, "invalid_ops", "1 à 20 opérations attendues", field="/ops")
    content, provenance = draft["content"], draft["provenance"]
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or set(op) != {"path", "value"}:
            raise ApiError(422, "invalid_ops", "opération attendue : {path, value}", field=f"/ops/{index}")
        content = apply_op(conn, content, op["path"], op["value"])
        provenance = _provenance(provenance, op["path"], op["value"], "author", principal)
    proposals = [
        {**s, "status": "superseded"} if s["status"] == "pending" and any(s["path"] == op["path"] for op in ops) else s
        for s in draft["proposals"]
    ]
    draft = _save(conn, draft, content=content, provenance=provenance, proposals=proposals)
    _event(conn, draft["id"], "edited", "user", str(principal.user_id), {"paths": [op["path"] for op in ops]}, content)
    return draft


# --- Assistant de conception -----------------------------------------------------------------


def _assistant_context(conn: Conn, draft: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Catalogue et conception : aucun inventaire, aucune cible, aucun secret, aucune donnée de projet.

    Les observations de référence n'entrent que sous forme assainie (pas d'identité d'hôte).
    """
    os_ = base["contract"]["system_profile"]["os"]
    return {
        "base": {"slug": base["slug"], "version": base["version"], "title": base["contract"]["title"]},
        "design": draft["content"],
        "catalog": [
            {k: c[k] for k in ("slug", "version", "title", "description", "parameters")}
            for c in catalog(conn)
            if os_ in c["supports"]["os"]
        ],
        "waves_limits": {k: list(v) for k, v in registry.WAVE_LIMITS.items()},
        "reference": refmod.sanitized_for_assistant(draft.get("sources") or []),
    }


def _latest(context: dict[str, Any], slug: str) -> str | None:
    versions = [c["version"] for c in context["catalog"] if c["slug"] == slug]
    return max(versions, key=registry.semver_key) if versions else None


EXTENSION_HINTS = (
    (
        ("antivirus", "clamav"),
        "new_component",
        "antivirus",
        "Composant antivirus (ClamAV) : paquets, mise à jour des signatures depuis le miroir local, contrôle de santé "
        "du démon. À instruire : contrat, rôle, tests, preuve en laboratoire.",
    ),
    (
        ("postfix", "messagerie", "relais mail"),
        "new_component",
        "relais-mail",
        "Composant relais de messagerie : aucun composant du catalogue ne le couvre.",
    ),
    (
        ("pays", "geoip", "géolocalisation"),
        "component_parameter",
        "fail2ban",
        "Paramètre de filtrage géographique pour fail2ban : non déclaré au contrat 1.0.0 ; exigerait une base GeoIP "
        "locale et un nouveau contrôle de santé.",
    ),
)


def fake_design_turn(scenario: str, message: str, context: dict[str, Any]) -> str:
    """Adaptateur fictif déterministe (démonstration et tests) ; mêmes contrôles serveur que le modèle."""
    if scenario == "unavailable":
        raise chat.AdapterUnavailable("assistant indisponible (scénario de démonstration)")
    if scenario == "timeout":
        raise chat.AdapterUnavailable("délai de réponse du modèle dépassé")
    if scenario == "invalid_json":
        return "{ réponse tronquée"
    if scenario == "schema_violation":
        return json.dumps(
            {
                "reply": "",
                "questions": [],
                "suggestions": [],
                "extensions": [],
                "command": "apt-get install -y fail2ban",
            }
        )
    if scenario == "override_attempt":
        return json.dumps(
            {
                "reply": "Je place les serveurs sur le réseau DMZ.",
                "questions": [],
                "extensions": [],
                "suggestions": [
                    {"path": "/environments/dev/placement/network_role", "value": "dmz", "why": "réseau exposé"}
                ],
            }
        )
    if scenario == "unknown_option":
        return json.dumps(
            {
                "reply": "J'ajoute Apache.",
                "questions": [],
                "extensions": [],
                "suggestions": [{"path": "/components/apache2", "value": {"version": "2.4.0"}, "why": "serveur web"}],
            }
        )
    text = message.lower()
    design = context["design"]
    present = {c["component"]: c for c in design["components"]}
    suggestions: list[dict[str, Any]] = []
    questions: list[dict[str, str]] = []
    extensions: list[dict[str, str]] = []
    hardening = any(k in text for k in ("fail2ban", "durci", "brute", "ssh", "intrusion", "bannir", "bloquer"))
    f2b = _latest(context, "fail2ban")
    if hardening and f2b and "fail2ban" not in present:
        suggestions.append(
            {
                "path": "/components/fail2ban",
                "value": {"version": f2b},
                "why": "Fail2ban bloque les adresses qui accumulent des échecs de connexion.",
            }
        )
        suggestions.append(
            {
                "path": "/components/fail2ban/config/jail_sshd",
                "value": True,
                "why": "Surveille les échecs de connexion SSH (jail sshd).",
            }
        )
        if "nginx" in present and not any(k in text for k in ("http", "authentification web")):
            questions.append(
                {
                    "text": "Faut-il aussi surveiller les échecs d'authentification HTTP de Nginx "
                    "(jail nginx-http-auth) ?"
                }
            )
    if (hardening or "fail2ban" in present) and any(k in text for k in ("strict", "3 essais", "trois essais")):
        suggestions.append(
            {
                "path": "/components/fail2ban/config/maxretry",
                "value": 3,
                "why": "Trois échecs avant blocage, comme demandé.",
            }
        )
    if any(k in text for k in ("authentification http", "http auth", "nginx-http-auth")) and (
        "fail2ban" in present or hardening
    ):
        suggestions.append(
            {
                "path": "/components/fail2ban/config/jail_nginx_http_auth",
                "value": True,
                "why": "Surveille les échecs d'authentification HTTP de Nginx.",
            }
        )
    if (
        any(k in text for k in ("vague", "pilote", "progressi", "par étape", "une machine d'abord"))
        and design["waves"].get("mode") != "pilot"
    ):
        suggestions.append(
            {
                "path": "/waves",
                "value": {"mode": "pilot", "pilot_size": 1, "wave_size": 1, "stop_on_failure": True},
                "why": "Une machine pilote d'abord ; si elle échoue, les suivantes ne sont pas touchées.",
            }
        )
    if hardening and design["title"] == context["base"]["title"]:
        suggestions.append(
            {
                "path": "/title",
                "value": f"{context['base']['title']} durci",
                "why": "Le titre distingue ce service de sa base.",
            }
        )
        suggestions.append(
            {
                "path": "/description",
                "value": "Ubuntu 24.04, Nginx et Fail2ban, appliqué par vagues après une machine pilote",
                "why": "Description lisible par les demandeurs.",
            }
        )
    for keys, kind, subject, summary in EXTENSION_HINTS:
        if any(k in text for k in keys):
            extensions.append({"kind": kind, "subject": subject, "summary": summary})
    if suggestions:
        reply = (
            f"Je propose {len(suggestions)} réglage(s). Chacun attend votre décision : rien n'est appliqué "
            "sans vous, et la publication reste l'affaire du publicateur."
        )
    elif extensions:
        reply = "Le catalogue ne couvre pas cette demande : je l'ai notée comme proposition d'extension à revoir."
    else:
        reply = "Je n'ai pas de réglage à proposer. Décrivez le résultat attendu (protection, déploiement progressif…)."
    if extensions and suggestions:
        reply += " Une partie dépasse le catalogue : elle est notée comme proposition d'extension."
    return json.dumps(
        {"reply": reply, "questions": questions[:3], "suggestions": suggestions[:12], "extensions": extensions[:3]},
        ensure_ascii=False,
    )


def validate_design_turn(conn: Conn, raw: str, content: dict[str, Any]) -> dict[str, Any]:
    """Revalidation serveur : schéma strict, chemins de conception, catalogue et bornes, textes bruts."""
    try:
        turn = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TurnInvalid("invalid_json", "réponse non JSON") from exc
    if not isinstance(turn, dict) or set(turn) != set(DESIGN_KEYS):
        raise TurnInvalid("schema_violation", "clés inattendues ou manquantes")
    if not isinstance(turn["reply"], str) or len(turn["reply"]) > 1200:
        raise TurnInvalid("schema_violation", "réponse trop longue")
    for key, high in (("questions", 3), ("suggestions", 12), ("extensions", 3)):
        if not isinstance(turn[key], list) or len(turn[key]) > high:
            raise TurnInvalid("schema_violation", f"{key} : liste de {high} éléments au plus attendue")
    for q in turn["questions"]:
        if not isinstance(q, dict) or set(q) != {"text"} or not isinstance(q["text"], str) or len(q["text"]) > 300:
            raise TurnInvalid("schema_violation", "question mal formée")
    scratch = content
    for s in turn["suggestions"]:
        if (
            not isinstance(s, dict)
            or set(s) != {"path", "value", "why"}
            or not isinstance(s["why"], str)
            or len(s["why"]) > 300
        ):
            raise TurnInvalid("schema_violation", "suggestion mal formée")
        try:
            scratch = apply_op(conn, scratch, s["path"], s["value"])
        except ApiError as exc:
            code = "out_of_scope_path" if exc.code == "path_not_allowed" else "invalid_value"
            raise TurnInvalid(code, f"{s['path']} : {exc.message}") from exc
    for e in turn["extensions"]:
        if (
            not isinstance(e, dict)
            or set(e) != {"kind", "subject", "summary"}
            or e["kind"] not in ("component_parameter", "new_component")
            or not isinstance(e["subject"], str)
            or not 1 <= len(e["subject"]) <= 80
            or not isinstance(e["summary"], str)
            or not 1 <= len(e["summary"]) <= 600
        ):
            raise TurnInvalid("schema_violation", "proposition d'extension mal formée")
    return turn


def assist(conn: Conn, principal: Principal, draft_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    extra = set(body) - {"row_version", "message"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    message = str(body.get("message") or "").strip()
    if not message or len(message) > 2000:
        raise ApiError(422, "invalid_message", "message vide ou trop long", field="/message")
    return _run_assistant(conn, principal, draft, message)


def _run_assistant(conn: Conn, principal: Principal, draft: dict[str, Any], message: str) -> dict[str, Any]:
    redactor = Redactor()
    base = _base(conn, draft)
    context = _assistant_context(conn, draft, base)
    cfg = chat.assistant_settings(conn)
    outcome, code, notice, turn = "ok", None, None, None
    try:
        if cfg["adapter"] == "disabled":
            raise chat.AdapterUnavailable("assistant désactivé par l'administrateur")
        if cfg["adapter"] == "ollama":
            raw = chat.model_call(
                conn, ASSISTANT_SYSTEM, {"message": redactor.text(message), "context": context}, DESIGN_SCHEMA
            )
        else:
            raw = fake_design_turn(cfg["scenario"], message, context)
        turn = validate_design_turn(conn, raw, draft["content"])
    except chat.AdapterUnavailable as exc:
        outcome, notice = "unavailable", f"Assistant indisponible : {exc}. Le brouillon reste modifiable à la main."
    except TurnInvalid as exc:
        outcome, code = "rejected", exc.code
        notice = f"Réponse de l'assistant refusée par le serveur ({exc}). Rien n'a été modifié."
        audit.record(
            conn,
            actor_type="assistant",
            actor_id=cfg["adapter"],
            action="studio.suggestion_rejected",
            outcome=exc.code,
            targets={"draft": str(draft["id"])},
        )
    turn_no = (
        1
        + conn.execute(
            "SELECT count(*) AS n FROM template_draft_events WHERE draft_id = %s AND action = 'assistant_turn'",
            (draft["id"],),
        ).fetchone()["n"]
    )
    proposals = [{**s, "status": "superseded"} if s["status"] == "pending" else s for s in draft["proposals"]]
    questions = draft["questions"]
    if turn is not None:
        for index, s in enumerate(turn["suggestions"]):
            proposals.append(
                {
                    "id": f"t{turn_no}s{index + 1}",
                    "turn": turn_no,
                    "path": s["path"],
                    "value": s["value"],
                    "why": redactor.text(s["why"]),
                    "status": "pending",
                }
            )
        questions = [{"turn": turn_no, "text": redactor.text(q["text"])} for q in turn["questions"]]
        for e in turn["extensions"]:
            conn.execute(
                "INSERT INTO extension_proposals (id, draft_id, kind, subject, summary, proposal, origin, "
                "created_by) VALUES (%s, %s, %s, %s, %s, %s, 'assistant', %s)",
                (
                    uuid7(),
                    draft["id"],
                    e["kind"],
                    redactor.text(e["subject"]),
                    redactor.text(e["summary"]),
                    Jsonb({"turn": turn_no, "expected": ["contrat", "rôle", "tests", "preuve en laboratoire"]}),
                    principal.user_id,
                ),
            )
        turn["reply"] = redactor.text(turn["reply"])
    draft = _save(conn, draft, proposals=proposals, questions=questions)
    _event(
        conn,
        draft["id"],
        "assistant_turn",
        "assistant",
        cfg["adapter"],
        {
            "message": redactor.text(message)[:500],
            "outcome": outcome,
            "code": code,
            "turn": turn_no,
            "reply": (turn or {}).get("reply") or notice,
            "suggestions": len((turn or {}).get("suggestions", [])),
            "extensions": len((turn or {}).get("extensions", [])),
            "simulated": cfg["simulated"],
        },
        draft["content"],
    )
    return {
        "status": outcome,
        "code": code,
        "notice": notice,
        "reply": (turn or {}).get("reply"),
        "simulated": cfg["simulated"],
        "adapter": cfg["adapter"],
        "draft": draft,
    }


def decide_suggestion(
    conn: Conn, principal: Principal, draft_id: UUID, suggestion_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    extra = set(body) - {"row_version", "decision"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    decision = body.get("decision")
    if decision not in ("accepted", "rejected"):
        raise ApiError(422, "invalid_decision", "décision attendue : accepted ou rejected", field="/decision")
    suggestion = next((s for s in draft["proposals"] if s["id"] == suggestion_id), None)
    if suggestion is None:
        raise not_found("suggestion")
    if suggestion["status"] != "pending":
        raise conflict("already_decided", "cette suggestion a déjà fait l'objet d'une décision")
    content, provenance = draft["content"], draft["provenance"]
    if decision == "accepted":
        content = apply_op(conn, content, suggestion["path"], suggestion["value"])
        provenance = _provenance(
            provenance, suggestion["path"], suggestion["value"], "assistant", principal, suggestion_id
        )
    proposals = [
        {**s, "status": decision, "decided_at": _now()} if s["id"] == suggestion_id else s for s in draft["proposals"]
    ]
    draft = _save(conn, draft, content=content, provenance=provenance, proposals=proposals)
    _event(
        conn,
        draft["id"],
        f"suggestion_{decision}",
        "user",
        str(principal.user_id),
        {"suggestion": suggestion_id, "path": suggestion["path"]},
        content,
    )
    return draft


# --- Cycle de vie ----------------------------------------------------------------------------


def _next_version(conn: Conn, slug: str) -> str:
    rows = conn.execute("SELECT version FROM template_versions WHERE slug = %s", (slug,)).fetchall()
    if not rows:
        return "1.0.0"
    major, minor, _ = registry.semver_key(max((r["version"] for r in rows), key=registry.semver_key))
    return f"{major}.{minor + 1}.0"


def create(conn: Conn, principal: Principal, body: dict[str, Any]) -> dict[str, Any]:
    require_author(principal)
    extra = set(body) - {"slug", "base", "brief", "reference"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    slug = body.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ApiError(422, "invalid_slug", "identifiant attendu : minuscules, chiffres et tirets", field="/slug")
    base_ref = body.get("base")
    if not isinstance(base_ref, dict) or set(base_ref) != {"slug", "version"}:
        raise ApiError(422, "invalid_base", "base attendue : {slug, version} d'une version publiée", field="/base")
    base = registry.template(conn, str(base_ref["slug"]), str(base_ref["version"]))
    if base is None or base["status"] != "published":
        raise ApiError(422, "invalid_base", "la base doit être une version publiée", field="/base")
    brief = str(body.get("brief") or "").strip()
    if len(brief) > 2000:
        raise ApiError(422, "invalid_message", "description du besoin trop longue", field="/brief")
    reference = body.get("reference")
    if reference is not None and brief:
        raise ApiError(
            422,
            "conflicting_origin",
            "choisir soit un brief d'assistant, soit un serveur de référence, pas les deux",
            field="/reference",
        )
    if conn.execute(
        "SELECT 1 FROM template_drafts WHERE slug = %s AND status IN ('editing', 'in_review')", (slug,)
    ).fetchone():
        raise conflict("draft_exists", f"un brouillon de « {slug} » est déjà ouvert")
    if (
        slug != base["slug"]
        and conn.execute("SELECT 1 FROM template_versions WHERE slug = %s LIMIT 1", (slug,)).fetchone()
    ):
        raise conflict("slug_taken", f"« {slug} » existe déjà : partez de sa dernière version publiée")
    c = base["contract"]
    content = {
        "title": c["title"],
        "description": c["description"],
        "components": [
            {"component": x["component"], "version": x["version"], "config": dict(x.get("config", {}))}
            for x in c["components"]
        ],
        "waves": dict(c["deployment_policy"]["waves"]),
    }
    provenance = {
        p: {"source": "base", "by": None, "at": _now()}
        for p in ["/title", "/description", "/waves"] + [f"/components/{x['component']}" for x in c["components"]]
    }
    origin = "reference_host" if reference is not None else "assistant"
    sources: list[dict[str, Any]] = []
    if reference is not None:
        observation = refmod.load_observation(conn, principal, reference)
        analysis = refmod.analyze(conn, observation, c)
        sources = [analysis]
        # Titre / description initiaux dérivés de l'observation (sans identité d'hôte).
        content["title"] = f"{c['title']} (depuis serveur de référence)"
        content["description"] = (
            "Brouillon dérivé d'observations "
            + ("fictives explicites" if observation.get("simulated") else "en lecture seule")
            + ". Chaque proposition « À standardiser » attend une décision de l'auteur."
        )
        provenance["/title"] = {"source": "reference_host", "by": str(principal.user_id), "at": _now()}
        provenance["/description"] = {"source": "reference_host", "by": str(principal.user_id), "at": _now()}
    draft = conn.execute(
        "INSERT INTO template_drafts (id, slug, version, origin, base_template_version_id, content, provenance, "
        "sources, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            uuid7(),
            slug,
            _next_version(conn, slug),
            origin,
            base["id"],
            Jsonb(content),
            Jsonb(provenance),
            Jsonb(sources),
            principal.user_id,
        ),
    ).fetchone()
    _event(
        conn,
        draft["id"],
        "created",
        "user",
        str(principal.user_id),
        {
            "base": f"{base['slug']}@{base['version']}",
            "origin": origin,
            **(
                {
                    "reference": sources[0]["source"],
                    "host_unchanged": sources[0]["host_unchanged"],
                    "simulated": sources[0]["source"]["simulated"],
                }
                if sources
                else {}
            ),
        },
        content,
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="studio.draft.create",
        outcome="ok",
        targets={"draft": str(draft["id"]), "template": slug},
        versions={"base": f"{base['slug']}@{base['version']}", "origin": origin},
    )
    result = {"draft": draft, "turn": None}
    if brief:
        result["turn"] = _run_assistant(conn, principal, draft, brief)
        result["draft"] = result["turn"]["draft"]
    return result


def decide_reference(
    conn: Conn, principal: Principal, draft_id: UUID, item_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Décide une proposition issue du serveur de référence.

    « À standardiser » acceptée → apply_op + provenance reference_host.
    « À confirmer » acceptée → extension (jamais appliquée).
    « Propre à cet hôte » → refusée (422).
    """
    extra = set(body) - {"row_version", "decision"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    decision = body.get("decision")
    if decision not in ("accepted", "rejected"):
        raise ApiError(422, "invalid_decision", "decision attendue : accepted | rejected", field="/decision")
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    found = refmod.find_item(draft.get("sources") or [], item_id)
    if found is None:
        raise not_found("proposition de référence")
    _analysis, item = found
    if item["category"] == "host_specific" or not item.get("decidable"):
        raise ApiError(
            422,
            "not_decidable",
            "valeur propre à l'hôte : non recopiable dans le brouillon",
            field="/item",
        )
    if item.get("status") != "pending":
        raise conflict("already_decided", "cette proposition a déjà une décision")
    sources = copy.deepcopy(draft["sources"])
    analysis, item = refmod.find_item(sources, item_id)  # type: ignore[misc]
    assert analysis is not None and item is not None
    content, provenance = draft["content"], draft["provenance"]
    if decision == "accepted" and item["category"] == "standardize":
        # Le composant doit exister avant un réglage de config.
        path, value = item["path"], item["value"]
        if path and path.startswith("/components/") and "/config/" in path:
            slug = path.split("/")[2]
            if not any(c["component"] == slug for c in content["components"]):
                raise ApiError(
                    422,
                    "component_required",
                    f"acceptez d'abord le composant « {slug} » avant son réglage",
                    field="/item",
                )
        content = apply_op(conn, content, path, value)
        provenance = _provenance(provenance, path, value, "reference_host", principal)
    elif decision == "accepted" and item["category"] == "confirm":
        ext = item.get("extension") or {}
        conn.execute(
            "INSERT INTO extension_proposals (id, draft_id, kind, subject, summary, proposal, origin, "
            "created_by) VALUES (%s, %s, %s, %s, %s, %s, 'reference_host', %s)",
            (
                uuid7(),
                draft["id"],
                ext.get("kind", "component_parameter"),
                str(ext.get("subject", "reference"))[:80],
                str(ext.get("summary", item["deduction"]))[:600],
                Jsonb({"observation": item["observation"], "deduction": item["deduction"]}),
                principal.user_id,
            ),
        )
    item["status"] = decision
    item["decided_at"] = _now()
    draft = _save(conn, draft, content=content, provenance=provenance, sources=sources)
    _event(
        conn,
        draft["id"],
        f"reference_{decision}",
        "user",
        str(principal.user_id),
        {"item": item_id, "category": item["category"], "path": item.get("path")},
        content,
    )
    return draft


def submit(conn: Conn, principal: Principal, draft_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    """Soumet le contenu vu par l'auteur : version immuable `in_review`, rien n'est publié ni déployé."""
    extra = set(body) - {"row_version", "digest"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    if body.get("digest") != content_digest(draft["content"]):
        raise conflict("stale_digest", "le contenu a changé depuis l'affichage : recharger")
    found = problems(conn, draft)
    if found:
        raise ApiError(422, "draft_invalid", "le brouillon ne passe pas les contrôles", details=found)
    if registry.template(conn, draft["slug"], draft["version"]) is not None:
        raise conflict("version_exists", f"{draft['slug']}@{draft['version']} existe déjà au registre")
    contract = build_contract(conn, draft)
    tid = uuid7()
    conn.execute(
        "INSERT INTO template_versions (id, slug, version, contract, digest, source_ref, status, submitted_by, "
        "submitted_at, draft_id) VALUES (%s, %s, %s, %s, %s, %s, 'in_review', %s, now(), %s)",
        (
            tid,
            draft["slug"],
            draft["version"],
            Jsonb(contract),
            digest(contract),
            f"atelier/{draft['id']}",
            principal.user_id,
            draft["id"],
        ),
    )
    conn.execute(
        "INSERT INTO template_events (template_version_id, action, actor_id, reason) VALUES (%s, 'submitted', %s, %s)",
        (tid, principal.user_id, "soumis depuis l'atelier"),
    )
    draft = _save(conn, draft, status="in_review", template_version_id=tid)
    _event(
        conn,
        draft["id"],
        "submitted",
        "user",
        str(principal.user_id),
        {"version": draft["version"], "template_digest": digest(contract)},
        draft["content"],
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="studio.draft.submit",
        outcome="ok",
        targets={"draft": str(draft["id"]), "template": draft["slug"]},
        versions={"template": draft["version"], "digest": digest(contract)},
    )
    return {
        "slug": draft["slug"],
        "version": draft["version"],
        "status": "in_review",
        "digest": digest(contract),
        "draft": draft,
    }


def abandon(conn: Conn, principal: Principal, draft_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    draft = _own(conn, principal, draft_id, body.get("row_version"))
    draft = _save(conn, draft, status="abandoned")
    _event(conn, draft["id"], "abandoned", "user", str(principal.user_id), {}, draft["content"])
    return draft


# --- Vues ------------------------------------------------------------------------------------


def _value_label(conn: Conn, content: dict[str, Any], path: str) -> str:
    if path in ("/title", "/description"):
        return str(content[path[1:]])
    if path == "/waves":
        return waves_label(content["waves"])
    if m := COMPONENT_PATH.match(path):
        entry = next((c for c in content["components"] if c["component"] == m.group(1)), None)
        return f"{m.group(1)} {entry['version']}" if entry else "retiré"
    if m := PARAM_PATH.match(path):
        entry = next((c for c in content["components"] if c["component"] == m.group(1)), None)
        return json.dumps((entry or {}).get("config", {}).get(m.group(2)), ensure_ascii=False)
    return ""


def waves_label(waves: dict[str, Any]) -> str:
    if waves.get("mode") != "pilot":
        return "toutes les machines ensemble"
    return f"pilote de {waves['pilot_size']} machine(s), puis vagues de {waves['wave_size']} ; un échec arrête la suite"


def suggestion_label(s: dict[str, Any]) -> str:
    path, value = s["path"], s["value"]
    if m := COMPONENT_PATH.match(path):
        return f"Retirer {m.group(1)}" if value is None else f"Ajouter {m.group(1)} {value['version']}"
    if m := PARAM_PATH.match(path):
        return f"{m.group(1)} · {m.group(2)} = {json.dumps(value, ensure_ascii=False)}"
    if path == "/waves":
        return f"Vagues : {waves_label(value)}"
    return f"{'Titre' if path == '/title' else 'Description'} : {value}"


def _users(conn: Conn, ids: set[str]) -> dict[str, str]:
    clean = [i for i in ids if i]
    if not clean:
        return {}
    rows = conn.execute("SELECT id, display_name FROM users WHERE id = ANY(%s::uuid[])", (clean,)).fetchall()
    return {str(r["id"]): r["display_name"] for r in rows}


def view(conn: Conn, principal: Principal, draft: dict[str, Any]) -> dict[str, Any]:
    base = _base(conn, draft)
    content = draft["content"]
    names = _users(conn, {str(draft["created_by"])} | {p.get("by") or "" for p in draft["provenance"].values()})
    comps = []
    for c in content["components"]:
        row = _component_row(conn, c["component"], c["version"])
        contract = row["contract"] if row else {"parameters": {}, "title": c["component"]}
        full = _config(contract, c["config"]) if row else dict(c["config"])
        comps.append(
            {
                "component": c["component"],
                "version": c["version"],
                "title": contract["title"],
                "verification_level": row["verification_level"] if row else None,
                "evidence": registry.evidence_summary(conn, row["id"]) if row else None,
                "not_covered": contract.get("not_covered", []),
                "parameters": [
                    {
                        "name": k,
                        "label": spec.get("label", k),
                        "type": spec["type"],
                        "value": full.get(k),
                        "explicit": k in c["config"],
                        **{key: spec[key] for key in ("min", "max", "values") if key in spec},
                    }
                    for k, spec in contract["parameters"].items()
                    if spec["type"] != "secret_ref"
                ],
            }
        )
    found = problems(conn, draft)
    contract = build_contract(conn, draft, base)
    owner = draft["created_by"] == principal.user_id and principal.has_role("template_author")
    editable = owner and draft["status"] == "editing"
    not_editable = (
        None
        if editable
        else ("brouillon d'une autre personne" if not owner else f"brouillon « {STATUS_LABELS[draft['status']]} »")
    )
    cfg = chat.assistant_settings(conn)
    extensions = conn.execute(
        "SELECT * FROM extension_proposals WHERE draft_id = %s ORDER BY created_at", (draft["id"],)
    ).fetchall()
    returned = conn.execute(
        "SELECT e.reason, e.at, t.version FROM template_events e JOIN template_versions t ON t.id = "
        "e.template_version_id WHERE t.draft_id = %s AND e.action = 'returned' ORDER BY e.at DESC LIMIT 1",
        (draft["id"],),
    ).fetchone()
    history = conn.execute(
        "SELECT * FROM template_draft_events WHERE draft_id = %s ORDER BY id", (draft["id"],)
    ).fetchall()
    return {
        "id": str(draft["id"]),
        "slug": draft["slug"],
        "version": draft["version"],
        "status": draft["status"],
        "status_label": STATUS_LABELS[draft["status"]],
        "origin": draft["origin"],
        "origin_label": (
            "Depuis un serveur de référence" if draft["origin"] == "reference_host" else "Assisté par l'IA"
        ),
        "row_version": draft["row_version"],
        "author": names.get(str(draft["created_by"])),
        "mine": draft["created_by"] == principal.user_id,
        "base": {"slug": base["slug"], "version": base["version"], "title": base["contract"]["title"]},
        "inherited": {
            "terraform": f"{contract['terraform']['module']}@{contract['terraform']['version']}",
            "os": contract["system_profile"]["os"],
            "environments": sorted(contract["environments"]),
            "note": "Placement, réseau, tailles, nommage et entrées viennent de la base : l'atelier ne "
            "les modifie pas.",
        },
        "design": {
            "title": content["title"],
            "description": content["description"],
            "waves": content["waves"],
            "waves_label": waves_label(content["waves"]),
            "components": comps,
        },
        "reference": next(
            (refmod.public_analysis(a) for a in (draft.get("sources") or []) if a.get("kind") == "reference_analysis"),
            None,
        ),
        "health_checks": contract["health_checks"],
        "provenance": [
            {
                "path": path,
                "value": _value_label(conn, content, path),
                "source": p["source"],
                "source_label": SOURCE_LABELS.get(p["source"], p["source"]),
                "by": names.get(p.get("by") or ""),
                "at": p.get("at"),
            }
            for path, p in sorted(draft["provenance"].items())
        ],
        "suggestions": [{**s, "label": suggestion_label(s)} for s in draft["proposals"]],
        "questions": draft["questions"],
        "extensions": [
            {
                "id": str(e["id"]),
                "kind": e["kind"],
                "subject": e["subject"],
                "summary": e["summary"],
                "status": e["status"],
                "origin": e["origin"],
            }
            for e in extensions
        ],
        "problems": found,
        "digest": content_digest(content),
        "returned": {"version": returned["version"], "reason": returned["reason"], "at": returned["at"].isoformat()}
        if returned and draft["status"] == "editing"
        else None,
        "assistant": {"adapter": cfg["adapter"], "simulated": cfg["simulated"], "available": cfg["available"]},
        "history": [
            {"action": h["action"], "actor_type": h["actor_type"], "data": h["data"], "at": h["at"].isoformat()}
            for h in history
        ],
        "allowed_actions": {
            "edit": not_editable,
            "assist": not_editable or (None if cfg["available"] else "assistant désactivé"),
            "submit": not_editable or ("; ".join(p["message"] for p in found) if found else None),
            "abandon": not_editable,
        },
    }


def review_view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    """Ce que voit le publicateur : origine, auteur, écarts avec la base, provenance et points ouverts."""
    draft = _draft(conn, row["draft_id"])
    base = _base(conn, draft)
    names = _users(conn, {str(row["submitted_by"] or "")} | {p.get("by") or "" for p in draft["provenance"].values()})
    contract = row["contract"]
    before = {c["component"]: c for c in base["contract"]["components"]}
    changes = []
    for c in contract["components"]:
        old = before.pop(c["component"], None)
        if old is None:
            changes.append(f"composant ajouté : {c['component']} {c['version']}")
        elif old["version"] != c["version"] or old.get("config", {}) != c.get("config", {}):
            changes.append(f"composant modifié : {c['component']} {old['version']} → {c['version']}")
        for key, value in c.get("config", {}).items():
            if (old or {}).get("config", {}).get(key) != value:
                changes.append(f"réglage {c['component']}.{key} = {json.dumps(value, ensure_ascii=False)}")
    changes += [f"composant retiré : {slug}" for slug in before]
    if contract["deployment_policy"]["waves"] != base["contract"]["deployment_policy"]["waves"]:
        changes.append(f"vagues : {waves_label(contract['deployment_policy']['waves'])}")
    extensions = conn.execute(
        "SELECT kind, subject, summary, status FROM extension_proposals WHERE draft_id = %s ORDER BY created_at",
        (draft["id"],),
    ).fetchall()
    return {
        "draft_id": str(draft["id"]),
        "origin": draft["origin"],
        "origin_label": (
            "Brouillon depuis un serveur de référence : chaque valeur standardisée a été décidée par l'auteur"
            if draft["origin"] == "reference_host"
            else "Brouillon assisté par l'IA : chaque valeur a été décidée par l'auteur"
        ),
        "author": names.get(str(row["submitted_by"])),
        "submitted_at": row["submitted_at"].isoformat() if row["submitted_at"] else None,
        "base": {"slug": base["slug"], "version": base["version"]},
        "changes_from_base": changes,
        "provenance": [
            {
                "path": path,
                "source_label": SOURCE_LABELS.get(p["source"], p["source"]),
                "by": names.get(p.get("by") or ""),
            }
            for path, p in sorted(draft["provenance"].items())
            if p["source"] != "base"
        ],
        "open_questions": [q["text"] for q in draft["questions"]],
        "extensions": [dict(e) for e in extensions],
        "notice": "Une suggestion de l'assistant n'est ni un réglage, ni une approbation, ni un ordre : l'auteur a "
        "accepté ou saisi chaque valeur listée. Publier ne crée aucun job.",
    }
