"""Connecteur Rudder (RAC-36) : HTTP lecture seule vers des endpoints approuvés.

Configure URL + CA ; le jeton d'API passe uniquement par `secret_refs` (écriture seule).
`test_connection` vérifie TLS (comme vSphere). L'import d'observations est un job
`rudder_import` (classe read) exécuté par le worker, qui seul peut révéler le secret.
`execute` / écriture : indisponibles. Labo Rudder réel : non vérifié (D2).
"""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, field_validator

from ..db import Conn
from ..errors import ApiError, forbidden
from ..identity.authz import Principal
from . import vsphere
from .sdk import (
    AdapterDescriptor,
    NetworkConstraint,
    capability,
    reject_secrets_in_config,
)

KIND = "rudder"
TOKEN_SECRET_NAME = "token"  # noqa: S105 — nom de référence, pas une valeur secrète

# Chemins HTTP autorisés côté serveur Rudder (pas d'URL libre utilisateur).
NODES_PATH = "/api/latest/nodes/{node_id}"

DESCRIPTOR = AdapterDescriptor(
    kind=KIND,
    title="Rudder (conformité, lecture seule)",
    capabilities={
        "configure": capability("real", "URL et jeton (référence de secret)"),
        "test_connection": capability("real", "TLS : CA importée, nom d'hôte vérifié"),
        "inventory": capability("real", "rapports de conformité → observations"),
        "plan": capability(None, "remédiation Rudder hors périmètre ; admission Rackloom"),
        "execute": capability(None, "aucune écriture via Rudder"),
        "track": capability(None),
        "cancel": capability(None),
    },
    network=NetworkConstraint(),
    event_sources=frozenset({"system"}),
    enforces_scope=True,
    enforces_audit=True,
    requires_install_approval=False,
    notes=(
        "Lecture seule. Remédiation éventuelle toujours derrière l'admission Rackloom. "
        "Labo Rudder réel : non vérifié (D2)."
    ),
)


class RudderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str | None = None
    ca_pem: str | None = None
    default_node_id: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("https obligatoire")
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("URL attendue sous la forme https://hôte[:port]")
        if parts.path not in ("", "/"):
            raise ValueError("URL attendue sous la forme https://hôte[:port]")
        _ = parts.port
        return f"https://{parts.netloc}"

    @field_validator("ca_pem")
    @classmethod
    def _ca(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Réutilise la validation vSphere (PEM d'autorité uniquement).
        return vsphere.VsphereConfig.model_validate({"ca_pem": value}).ca_pem

    @field_validator("default_node_id")
    @classmethod
    def _node(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or len(text) > 128 or "/" in text or ".." in text:
            raise ValueError("identifiant de nœud Rudder invalide")
        return text


def endpoint(url: str) -> tuple[str, int]:
    return vsphere.endpoint(url)


def failure(code: str, message: str | None = None) -> dict[str, Any]:
    reasons = {
        **vsphere.REASONS,
        "unauthorized": "jeton Rudder refusé ou absent",
        "node_not_found": "nœud Rudder introuvable",
        "invalid_report": "réponse Rudder illisible ou incomplète",
        "secret_missing": "référence de secret « token » absente",
    }
    return {
        "status": "failed",
        "simulated": False,
        "code": code,
        "message": message or reasons.get(code, code),
    }


class RudderAdapter:
    """Adaptateur HTTP Rudder (protocole Adapter F30)."""

    kind = KIND

    def descriptor(self) -> AdapterDescriptor:
        return DESCRIPTOR

    def check_scope(self, principal: Principal, action: str) -> None:
        if principal.is_platform_admin:
            return
        if any(b.role in ("connector_admin", "project_admin") and b.project_id for b in principal.bindings):
            return
        raise forbidden(
            f"rôle connector_admin, project_admin ou administrateur requis pour {action}",
            code="scope_denied",
        )

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        reject_secrets_in_config(config)
        from pydantic import ValidationError

        try:
            cfg = RudderConfig.model_validate(config)
        except ValidationError as exc:
            err = exc.errors()[0]
            field = "/config/" + "/".join(str(p) for p in err["loc"])
            raise ApiError(
                422,
                "invalid_config",
                str(err["msg"]).removeprefix("Value error, "),
                field=field,
            ) from None
        out: dict[str, Any] = {}
        if cfg.url is not None:
            out["url"] = cfg.url
        if cfg.ca_pem is not None:
            out["ca_imported"] = True
        if cfg.default_node_id is not None:
            out["default_node_id"] = cfg.default_node_id
        # Conserve ca_pem uniquement pour register_endpoint (jamais stocké en config).
        if cfg.ca_pem is not None:
            out["_ca_pem"] = cfg.ca_pem
        return out

    def register_endpoint(self, conn: Conn, connector_id: Any, config: dict[str, Any]) -> None:
        from ..ids import uuid7

        url = config.get("url")
        if not url:
            return
        host, port = endpoint(str(url))
        ca_pem = config.get("_ca_pem")
        ca_value = str(ca_pem) if isinstance(ca_pem, str) else None
        conn.execute(
            "INSERT INTO approved_endpoints (id, connector_id, scheme, host, port, ca_bundle_pem) "
            "VALUES (%s, %s, 'https', %s, %s, %s) ON CONFLICT (connector_id, scheme, host, port) "
            "DO UPDATE SET ca_bundle_pem = COALESCE(EXCLUDED.ca_bundle_pem, approved_endpoints.ca_bundle_pem)",
            (uuid7(), connector_id, host, port, ca_value),
        )


def test_connection(host: str, port: int, ca_pem: str | None, timeout: float = 5.0) -> dict[str, Any]:
    """Négociation TLS uniquement (le jeton n'est jamais lu par l'API)."""
    result = vsphere.test_connection(host, port, ca_pem, timeout=timeout)
    if result["status"] == "ok":
        return {
            **result,
            "message": ("TLS vérifié : chaîne, validité et nom d'hôte ; authentification API à l'import (worker)"),
        }
    return result


def _ssl_context(ca_pem: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _https_get(
    *,
    base_url: str,
    path: str,
    token: str,
    ca_pem: str | None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """GET HTTPS vers un chemin fixé ; refuse tout schéma autre que https."""
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ApiError(422, "invalid_config", "URL https obligatoire", field="/config/url")
    if "://" in path or path.startswith("//") or ".." in path:
        raise ApiError(422, "invalid_config", "chemin Rudder interdit")
    url = f"https://{parts.netloc}{path}"
    req = Request(
        url,
        headers={
            "X-API-Token": token,
            "Accept": "application/json",
            "User-Agent": "rackloom-rudder/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(req, context=_ssl_context(ca_pem), timeout=timeout) as resp:  # noqa: S310 — URL bornée https + CA
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ApiError(502, "unauthorized", "jeton Rudder refusé ou absent") from None
        if exc.code == 404:
            raise ApiError(404, "node_not_found", "nœud Rudder introuvable") from None
        raise ApiError(502, "unreachable", f"Rudder a répondu HTTP {exc.code}") from None
    except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError):
            raise ApiError(502, "tls_untrusted", "certificat Rudder non fiable") from None
        raise ApiError(502, "unreachable", "serveur Rudder injoignable") from None
    if status >= 400:
        raise ApiError(502, "unreachable", f"Rudder a répondu HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(502, "invalid_report", "réponse Rudder non JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError(502, "invalid_report", "réponse Rudder : objet attendu")
    return payload


def normalize_node_payload(payload: dict[str, Any], *, node_id: str) -> dict[str, Any]:
    """Normalise une réponse Rudder (fixture / API documentée) en rapport d'observations.

    Mapping volontairement étroit (propriétés `rackloom.*` ou blocs logiciels) :
    labo réel non vérifié (D2).
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    nodes = data.get("nodes") if isinstance(data, dict) else None
    node: dict[str, Any]
    if isinstance(nodes, list) and nodes:
        first = nodes[0]
        if not isinstance(first, dict):
            raise ApiError(502, "invalid_report", "nœud Rudder illisible")
        node = first
    elif isinstance(data, dict) and ("software" in data or "properties" in data or "packages" in data):
        node = data
    else:
        raise ApiError(502, "invalid_report", "réponse Rudder sans nœud exploitable")

    packages: dict[str, Any] = {}
    services: dict[str, Any] = {}
    settings: dict[str, Any] = {}

    # 1) Propriétés explicites rackloom.* (fixtures et politiques Rudder ciblées).
    props = node.get("properties")
    if isinstance(props, list):
        for item in props:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = item.get("value")
            if name == "rackloom.packages" and isinstance(value, dict):
                packages.update({str(k): v for k, v in value.items()})
            elif name == "rackloom.services" and isinstance(value, dict):
                services.update({str(k): v for k, v in value.items()})
            elif name == "rackloom.settings" and isinstance(value, dict):
                settings.update({str(k): str(v) if v is not None else None for k, v in value.items()})
    elif isinstance(props, dict):
        if isinstance(props.get("rackloom.packages"), dict):
            packages.update(props["rackloom.packages"])
        if isinstance(props.get("rackloom.services"), dict):
            services.update(props["rackloom.services"])
        if isinstance(props.get("rackloom.settings"), dict):
            settings.update({k: str(v) if v is not None else None for k, v in props["rackloom.settings"].items()})

    # 2) Logiciels Rudder (name → version) si présents.
    software = node.get("software")
    if isinstance(software, dict):
        for name, meta in software.items():
            if isinstance(meta, dict) and meta.get("version") is not None:
                packages.setdefault(str(name), str(meta["version"]))
            elif isinstance(meta, str):
                packages.setdefault(str(name), meta)

    # 3) Blocs déjà normalisés (fixtures contractuelles).
    if isinstance(node.get("packages"), dict):
        packages.update(node["packages"])
    if isinstance(node.get("services"), dict):
        services.update(node["services"])
    if isinstance(node.get("settings"), dict):
        settings.update({k: str(v) if v is not None else None for k, v in node["settings"].items()})

    resolved_id = str(node.get("id") or node.get("node_id") or node_id)
    if not packages and not services and not settings:
        raise ApiError(502, "invalid_report", "rapport Rudder sans paquets/services/réglages")

    return {
        "node_id": resolved_id,
        "simulated": False,
        "packages": packages,
        "services": services,
        "settings": settings,
    }


def fetch_node_report(
    *,
    base_url: str,
    token: str,
    node_id: str,
    ca_pem: str | None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Récupère et normalise le rapport d'un nœud (lecture seule)."""
    if not node_id or "/" in node_id or ".." in node_id or len(node_id) > 128:
        raise ApiError(422, "invalid_node_id", "identifiant de nœud Rudder invalide", field="/node_id")
    path = NODES_PATH.format(node_id=node_id)
    payload = _https_get(base_url=base_url, path=path, token=token, ca_pem=ca_pem, timeout=timeout)
    return normalize_node_payload(payload, node_id=node_id)


def capabilities_dict() -> dict[str, dict[str, Any]]:
    return DESCRIPTOR.capabilities_dict()
