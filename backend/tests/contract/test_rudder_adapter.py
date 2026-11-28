"""RAC-36 : contrat de l'adaptateur Rudder HTTP (serveur local + fixtures).

Niveau de preuve : testé localement. Labo Rudder réel : non vérifié (D2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rackloom.connectors import contract, rudder
from rackloom.errors import ApiError
from support.rudder_http import rudder_https_server


def test_descripteur_rudder_passe_la_suite() -> None:
    issues = contract.validate_descriptor(rudder.DESCRIPTOR)
    assert not issues, issues
    assert rudder.DESCRIPTOR.capabilities["execute"].available is False
    assert rudder.DESCRIPTOR.capabilities["inventory"].available is True
    assert rudder.DESCRIPTOR.capabilities["inventory"].mode == "real"
    assert rudder.DESCRIPTOR.network.allow_user_supplied_url is False


def test_instance_refuse_secret_et_hors_perimetre() -> None:
    issues = contract.validate_adapter_instance(rudder.RudderAdapter())
    assert not issues, issues


def test_fetch_node_report_contre_serveur_local(tmp_path: Path) -> None:
    with rudder_https_server(tmp_path) as srv:
        report = rudder.fetch_node_report(
            base_url=srv["url"],
            token=srv["token"],
            node_id=srv["node_id"],
            ca_pem=srv["ca_pem"],
        )
    assert report["simulated"] is False
    assert report["node_id"] == "rudder-node-lab-001"
    assert report["packages"]["nginx"] == "1.24.0-2ubuntu7"
    assert report["settings"]["nginx.server_tokens"] == "on"
    assert report["services"]["nginx"]["active"] is True


def test_fetch_refuse_mauvais_jeton(tmp_path: Path) -> None:
    with rudder_https_server(tmp_path) as srv:
        with pytest.raises(ApiError) as exc:
            rudder.fetch_node_report(
                base_url=srv["url"],
                token="wrong-token",
                node_id=srv["node_id"],
                ca_pem=srv["ca_pem"],
            )
    assert exc.value.code == "unauthorized"


def test_fetch_noeud_inconnu(tmp_path: Path) -> None:
    with rudder_https_server(tmp_path) as srv:
        with pytest.raises(ApiError) as exc:
            rudder.fetch_node_report(
                base_url=srv["url"],
                token=srv["token"],
                node_id="does-not-exist",
                ca_pem=srv["ca_pem"],
            )
    assert exc.value.code == "node_not_found"


def test_normalize_fixture_sans_reseau() -> None:
    from support.rudder_http import load_node_fixture

    report = rudder.normalize_node_payload(load_node_fixture(), node_id="x")
    assert report["packages"]["nginx"]
    assert report["simulated"] is False


def test_descripteur_inclus_dans_suite_globale() -> None:
    reports = contract.run_suite([contract.descriptor_from_matrix(k) for k in ("rudder",)] + [rudder.DESCRIPTOR])
    by_kind = {r.kind: r for r in reports}
    assert by_kind["rudder"].ok, by_kind["rudder"].as_dict()
