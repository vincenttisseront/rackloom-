"""F20 : comparaison Debian/Ubuntu — rétroportages vs amont."""

from __future__ import annotations

from rackloom.software_inventory import (
    STATUS_NOT_AFFECTED,
    STATUS_VULNERABLE,
    compare_debian_versions,
    debian_ge,
    evaluate_advisory,
)


def test_compare_equal() -> None:
    assert compare_debian_versions("3.0.2-0ubuntu1.18", "3.0.2-0ubuntu1.18") == 0


def test_compare_backport_revision() -> None:
    assert compare_debian_versions("3.0.2-0ubuntu1.17", "3.0.2-0ubuntu1.18") < 0
    assert compare_debian_versions("3.0.2-0ubuntu1.18", "3.0.2-0ubuntu1.17") > 0


def test_tilde_sorts_before_end() -> None:
    assert compare_debian_versions("1.0~beta", "1.0") < 0


def test_backport_not_vulnerable_despite_older_upstream() -> None:
    """Installée 3.0.2-0ubuntu1.18 ; amont corrigé à 3.0.13 — verdict distribution = ok."""
    verdict = evaluate_advisory(
        installed_version="3.0.2-0ubuntu1.18",
        fixed_version="3.0.2-0ubuntu1.18",
        upstream_fixed_version="3.0.13",
    )
    assert verdict["status"] == STATUS_NOT_AFFECTED
    assert verdict["used_upstream_alone"] is False
    assert verdict["naive_upstream_would_flag"] is True
    assert debian_ge("3.0.2-0ubuntu1.18", "3.0.2-0ubuntu1.18")


def test_still_vulnerable_when_below_distro_fix() -> None:
    verdict = evaluate_advisory(
        installed_version="3.0.2-0ubuntu1.10",
        fixed_version="3.0.2-0ubuntu1.18",
        upstream_fixed_version="3.0.13",
    )
    assert verdict["status"] == STATUS_VULNERABLE
    assert verdict["used_upstream_alone"] is False
