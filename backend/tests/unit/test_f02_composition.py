"""F02 : validation et ordonnancement des compositions multi-VM."""

from __future__ import annotations

import pytest

from rackloom import composition
from rackloom.registry import CatalogError


def _member(mid: str, *, deps: list[dict] | None = None, qty: int | None = 1, qty_from: str | None = None) -> dict:
    m: dict = {
        "id": mid,
        "title": mid.capitalize(),
        "components": [{"component": "nginx", "version": "1.0.0"}],
        "health_checks": [{"type": "package_installed", "package": "nginx"}],
        "readiness": [{"type": "package_installed", "package": "nginx"}],
    }
    if qty is not None:
        m["quantity"] = qty
    if qty_from is not None:
        m["quantity_from"] = qty_from
        m.pop("quantity", None)
    if deps:
        m["depends_on"] = deps
    return m


def _contract(members: list[dict]) -> dict:
    return {
        "components": [{"component": "nginx", "version": "1.0.0"}],
        "composition": {"members": members},
        "environments": {
            "dev": {"naming": "{project_code}-{environment}-{member}-{nn}"},
        },
    }


def test_topo_order_base_before_app():
    c = _contract(
        [
            _member("application", deps=[{"member": "base", "readiness": "ready"}], qty=None, qty_from="quantity"),
            _member("base", qty=1),
        ]
    )
    assert composition.topological_member_order(c) == ["base", "application"]


def test_cycle_refused():
    c = _contract(
        [
            _member("alpha", deps=[{"member": "bravo", "readiness": "ready"}]),
            _member("bravo", deps=[{"member": "alpha", "readiness": "ready"}]),
        ]
    )
    with pytest.raises(CatalogError, match="cycle"):
        composition.validate_composition(c)


def test_waves_follow_members():
    c = _contract(
        [
            _member("base"),
            _member("application", deps=[{"member": "base", "readiness": "ready"}], qty=None, qty_from="quantity"),
        ]
    )
    instances = [
        {"name": "vit-dev-base-01", "member": "base"},
        {"name": "vit-dev-application-01", "member": "application"},
    ]
    assert composition.plan_composition_waves(c, instances) == [
        ["vit-dev-base-01"],
        ["vit-dev-application-01"],
    ]


def test_member_missing_from_top_components():
    c = _contract([_member("base"), _member("application", deps=[{"member": "base"}], qty=None, qty_from="quantity")])
    c["composition"]["members"][0]["components"] = [{"component": "fail2ban", "version": "1.0.0"}]
    with pytest.raises(CatalogError, match="absent de components"):
        composition.validate_composition(c)


def test_collect_checks_unions_members_of_scoped_hosts():
    """Vague N re-vérifie les hôtes des vagues 1..N : la collecte doit couvrir tous les membres."""
    resolution = {
        "health_checks": [
            {"type": "package_installed", "package": "nginx"},
            {"type": "http_probe", "port": 80, "path": "/"},
        ],
        "composition": {
            "members": [
                {
                    "id": "base",
                    "health_checks": [
                        {"type": "package_installed", "package": "fail2ban"},
                        {"type": "service_active", "unit": "fail2ban", "process": "fail2ban-server"},
                    ],
                },
                {
                    "id": "application",
                    "health_checks": [
                        {"type": "package_installed", "package": "nginx"},
                        {"type": "service_active", "unit": "nginx"},
                        {"type": "http_probe", "port": 80, "path": "/"},
                    ],
                },
            ]
        },
    }
    hosts = [
        {"name": "vit-dev-application-01", "composition_member": "application"},
        {"name": "vit-dev-base-01", "composition_member": "base"},
    ]
    checks = composition.collect_checks_for_hosts(resolution, hosts)
    types_subjects = {
        (c["type"], c.get("package") or c.get("unit") or c.get("process") or c.get("port")) for c in checks
    }
    assert ("package_installed", "fail2ban") in types_subjects
    assert ("service_active", "fail2ban") in types_subjects
    assert ("package_installed", "nginx") in types_subjects
    assert ("http_probe", 80) in types_subjects
    # Un seul membre → uniquement ses checks (pas le fallback template).
    base_only = composition.collect_checks_for_hosts(
        resolution, [{"name": "vit-dev-base-01", "composition_member": "base"}]
    )
    assert {c["type"] for c in base_only} == {"package_installed", "service_active"}
    assert all(c.get("package") == "fail2ban" or c.get("unit") == "fail2ban" for c in base_only)
