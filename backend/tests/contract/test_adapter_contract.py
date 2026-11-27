"""F30 : suite de contrat — connecteurs livrés + adaptateur de référence.

Un adaptateur sans contrôle de périmètre est rejeté par la suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from rackloom.connectors import MATRIX, contract, reference
from rackloom.connectors.sdk import AdapterDescriptor, NetworkConstraint, capability


def test_suite_verte_sur_connecteurs_existants_et_reference() -> None:
    reports = contract.run_suite()
    kinds = {r.kind for r in reports}
    assert kinds >= set(MATRIX) | {"reference"}
    assert all(r.ok for r in reports), [r.as_dict() for r in reports if not r.ok]


def test_adaptateur_sans_perimetre_rejete() -> None:
    """Recette F30 : adaptateur qui omet le contrôle de périmètre → suite rouge."""
    bad = AdapterDescriptor(
        kind="rogue",
        title="Adaptateur hors contrat",
        capabilities={name: capability("real") for name in ("configure", "test_connection")}
        | {name: capability(None) for name in ("inventory", "plan", "execute", "track", "cancel")},
        network=NetworkConstraint(),
        enforces_scope=False,
        enforces_audit=True,
    )
    issues = contract.validate_descriptor(bad)
    assert any(i.code == "scope_not_enforced" for i in issues)


def test_adaptateur_url_libre_rejete() -> None:
    bad = AdapterDescriptor(
        kind="ssrf",
        title="Adaptateur SSRF",
        capabilities={name: capability(None) for name in contract.CAPABILITIES}
        | {"configure": capability("real"), "test_connection": capability("real")},
        network=NetworkConstraint(allow_user_supplied_url=True),
        enforces_scope=True,
        enforces_audit=True,
    )
    issues = contract.validate_descriptor(bad)
    assert any(i.code == "free_url_forbidden" for i in issues)


def test_instance_reference_refuse_hors_perimetre() -> None:
    adapter = reference.ReferenceAdapter()
    issues = contract.validate_adapter_instance(adapter)
    assert not issues, issues


class _NoScopeAdapter:
    kind = "noscope"

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            kind="noscope",
            title="Sans périmètre",
            capabilities={name: capability(None) for name in contract.CAPABILITIES}
            | {"configure": capability("real"), "test_connection": capability("real")},
            enforces_scope=True,  # déclaré, mais check_scope est un no-op
            enforces_audit=True,
        )

    def check_scope(self, principal: Any, action: str) -> None:
        return None

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return config


def test_instance_sans_check_scope_effectif_rejetee() -> None:
    issues = contract.validate_adapter_instance(_NoScopeAdapter())
    assert any(i.code == "scope_not_checked" for i in issues)


def test_codes_erreur_documentes() -> None:
    assert "adapter_not_approved" in contract.DOCUMENTED_ERROR_CODES
    assert "scope_denied" in contract.DOCUMENTED_ERROR_CODES
    assert "endpoint_not_approved" in contract.DOCUMENTED_ERROR_CODES


def test_assert_suite_green() -> None:
    contract.assert_suite_green()
    with pytest.raises(AssertionError):
        contract.assert_suite_green(
            [
                AdapterDescriptor(
                    kind="bad",
                    title="bad",
                    capabilities={name: capability(None) for name in contract.CAPABILITIES},
                    enforces_scope=False,
                    enforces_audit=False,
                )
            ]
        )
