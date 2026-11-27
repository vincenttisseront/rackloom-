"""F30 : adaptateur de référence (contrat minimal, sans fournisseur externe).

Démontre capacités, périmètre, audit, endpoints approuvés et refus des secrets.
Aucun appel réseau réel : `test_connection` vérifie seulement la présence d'un
endpoint approuvé enregistré. Guide d'extension : `docs/adapter-sdk.md`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import field_validator

from ..db import Conn
from ..errors import ApiError, forbidden
from ..identity.authz import Principal
from .sdk import (
    AdapterConfigBase,
    AdapterDescriptor,
    Capability,
    NetworkConstraint,
    capability,
    reject_secrets_in_config,
)

KIND = "reference"
SLUG_DEFAULT = "reference-lab"


class ReferenceConfig(AdapterConfigBase):
    url: str | None = None
    label: str = "adaptateur de référence"

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

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > 80:
            raise ValueError("libellé entre 1 et 80 caractères")
        return text


DESCRIPTOR = AdapterDescriptor(
    kind=KIND,
    title="Adaptateur de référence",
    capabilities={
        "configure": capability("real"),
        "test_connection": capability("real", "vérifie un endpoint approuvé localement"),
        "inventory": capability(None),
        "plan": capability(None),
        "execute": capability(None),
        "track": capability(None),
        "cancel": capability(None),
    },
    network=NetworkConstraint(),
    event_sources=frozenset({"system"}),
    enforces_scope=True,
    enforces_audit=True,
    requires_install_approval=True,
    notes="Référence F30 : aucun fournisseur externe ; sert de modèle Proxmox/OpenTofu/AWX.",
)


class ReferenceAdapter:
    """Implémentation de référence du protocole Adapter."""

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
            cfg = ReferenceConfig.model_validate(config)
        except ValidationError as exc:
            err = exc.errors()[0]
            field = "/config/" + "/".join(str(p) for p in err["loc"])
            raise ApiError(
                422,
                "invalid_config",
                str(err["msg"]).removeprefix("Value error, "),
                field=field,
            ) from None
        return cfg.model_dump(exclude_none=True)

    def register_endpoint(self, conn: Conn, connector_id: Any, config: dict[str, Any]) -> None:
        """Enregistre l'URL comme endpoint approuvé (même chemin que vSphere)."""
        from ..ids import uuid7

        url = config.get("url")
        if not url:
            return
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        port = parts.port or 443
        conn.execute(
            "INSERT INTO approved_endpoints (id, connector_id, scheme, host, port, ca_bundle_pem) "
            "VALUES (%s, %s, 'https', %s, %s, NULL) ON CONFLICT (connector_id, scheme, host, port) "
            "DO NOTHING",
            (uuid7(), connector_id, host, port),
        )

    def test_connection(self, conn: Conn, connector_row: dict[str, Any]) -> dict[str, Any]:
        url = (connector_row.get("config") or {}).get("url")
        if not url:
            return {
                "status": "failed",
                "simulated": False,
                "code": "not_configured",
                "message": "URL de l'adaptateur de référence non configurée",
            }
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        port = parts.port or 443
        ep = conn.execute(
            "SELECT id FROM approved_endpoints WHERE connector_id = %s AND scheme = 'https' "
            "AND host = %s AND port = %s",
            (connector_row["id"], host, port),
        ).fetchone()
        if ep is None:
            return {
                "status": "failed",
                "simulated": False,
                "code": "endpoint_not_approved",
                "message": "endpoint non approuvé : enregistrez l'URL du connecteur",
            }
        return {
            "status": "ok",
            "simulated": False,
            "message": "Endpoint approuvé présent (adaptateur de référence, aucun appel externe)",
            "host": host,
            "port": port,
        }


def capabilities() -> dict[str, Capability]:
    return DESCRIPTOR.capabilities


def capabilities_dict() -> dict[str, dict[str, Any]]:
    return DESCRIPTOR.capabilities_dict()
