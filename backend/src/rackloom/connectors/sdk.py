"""F30 : SDK d'adaptateurs — interfaces, capacités, erreurs, réseau et événements.

Un adaptateur de connecteur déclare une matrice de capacités, refuse les secrets dans
la configuration, n'appelle que des endpoints approuvés, émet des événements scrubbés
et passe par les mêmes contrôles de périmètre, d'audit et d'approbation que les
connecteurs livrés. Voir `docs/adapter-sdk.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

CapabilityMode = Literal["real", "simulated", "request"]
CapabilityName = Literal[
    "configure", "test_connection", "inventory", "plan", "execute", "track", "cancel"
]

CAPABILITIES: tuple[CapabilityName, ...] = (
    "configure",
    "test_connection",
    "inventory",
    "plan",
    "execute",
    "track",
    "cancel",
)

# Codes d'erreur stables exposés par un adaptateur (jamais un secret, jamais une URL libre).
ERROR_CODES = frozenset(
    {
        "not_configured",
        "endpoint_not_approved",
        "tls_untrusted",
        "unreachable",
        "scope_denied",
        "capability_unavailable",
        "invalid_config",
        "secret_in_config",
        "adapter_not_approved",
        "adapter_untrusted",
        "adapter_tampered",
        "not_implemented",
    }
)

# Sources d'événements autorisées pour un adaptateur (sous-ensemble ADR-0006 + alias).
ADAPTER_EVENT_SOURCES = frozenset(
    {
        "vcenter",
        "terraform",
        "ansible",
        "system",
        "job",
        "verification",
    }
)

SECRET_HINTS = ("password", "secret", "token", "passphrase", "private_key", "api_key", "credential")


@dataclass(frozen=True, slots=True)
class Capability:
    """Une entrée de la matrice de capacités."""

    available: bool
    mode: CapabilityMode | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "mode": self.mode, "note": self.note}


def capability(mode: CapabilityMode | None, note: str = "") -> Capability:
    return Capability(available=mode is not None, mode=mode, note=note)


@dataclass(frozen=True, slots=True)
class NetworkConstraint:
    """Contrainte réseau : seuls des endpoints approuvés (scheme/host/port + CA) sont joignables."""

    schemes: tuple[str, ...] = ("https",)
    require_hostname_verify: bool = True
    allow_user_supplied_url: bool = False
    note: str = "endpoints approuvés uniquement ; TLS vérifié ; pas d'ignore"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemes": list(self.schemes),
            "require_hostname_verify": self.require_hostname_verify,
            "allow_user_supplied_url": self.allow_user_supplied_url,
            "note": self.note,
        }


DEFAULT_NETWORK = NetworkConstraint()


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """Descripteur statique d'un adaptateur (contrat déclaré, pas l'instance runtime)."""

    kind: str
    title: str
    capabilities: dict[str, Capability]
    network: NetworkConstraint = DEFAULT_NETWORK
    event_sources: frozenset[str] = field(default_factory=lambda: frozenset({"system"}))
    # True = l'adaptateur impose check_scope avant toute action d'écriture / config.
    enforces_scope: bool = True
    # True = chaque mutation est auditée (connector.* / adapter.*).
    enforces_audit: bool = True
    # True = installation depuis l'UI exige approbation + provenance (F30).
    requires_install_approval: bool = True
    notes: str = ""

    def capabilities_dict(self) -> dict[str, dict[str, Any]]:
        return {name: self.capabilities[name].as_dict() for name in CAPABILITIES if name in self.capabilities}


class AdapterConfigBase(BaseModel):
    """Base Pydantic : extra interdit ; les secrets passent par la route dédiée."""

    model_config = ConfigDict(extra="forbid", strict=True)


@runtime_checkable
class Adapter(Protocol):
    """Contrat runtime minimal qu'un adaptateur doit exposer."""

    kind: str

    def descriptor(self) -> AdapterDescriptor: ...

    def check_scope(self, principal: Any, action: str) -> None:
        """Refuse (403) si le principal n'a pas le droit d'agir sur ce connecteur."""
        ...

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Valide et normalise la configuration ; refuse secrets et champs libres."""
        ...


def reject_secrets_in_config(config: dict[str, Any]) -> None:
    """Refuse tout champ dont le nom évoque un secret (même chemin que routes_admin)."""
    from ..errors import ApiError

    for key in config:
        if any(h in key.lower() for h in SECRET_HINTS):
            raise ApiError(
                422,
                "secret_in_config",
                "un secret se saisit par la route des secrets, jamais dans la configuration",
                field=f"/config/{key}",
            )


def require_approved_endpoint(
    *,
    scheme: str,
    host: str,
    port: int,
    approved: bool,
) -> None:
    """Bloque un appel sortant hors `approved_endpoints`."""
    from ..errors import ApiError

    if not approved:
        raise ApiError(
            422,
            "endpoint_not_approved",
            f"endpoint non approuvé : {scheme}://{host}:{port}",
            field="/config/url",
        )
    if scheme not in DEFAULT_NETWORK.schemes:
        raise ApiError(422, "invalid_config", f"schéma {scheme} refusé (https obligatoire)", field="/config/url")
