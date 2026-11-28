"""Lot 4 : chiffrement client_secret OIDC et normalisation ACR (sans base)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rackloom import security
from rackloom.config import get_settings
from rackloom.identity import oidc


@pytest.fixture
def keys(tmp_path, monkeypatch):
    security.generate_keys(tmp_path)
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


def test_client_secret_roundtrip(keys):
    blob = oidc.encrypt_client_secret("lab-secret-value")
    assert b"lab-secret-value" not in blob
    assert oidc.decrypt_client_secret(blob) == "lab-secret-value"


def test_normalize_acr_reauth_elevates(keys):
    now = datetime.now(UTC)
    acr = oidc._normalize_acr(  # noqa: SLF001
        None,
        purpose="reauth",
        step_up_acr="urn:rackloom:step-up",
        auth_time=now,
    )
    assert acr == "urn:rackloom:step-up"


def test_normalize_acr_reauth_stale(keys):
    stale = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(Exception) as exc:
        oidc._normalize_acr(  # noqa: SLF001
            "pwd",
            purpose="reauth",
            step_up_acr="urn:rackloom:step-up",
            auth_time=stale,
        )
    assert getattr(exc.value, "code", None) == "reauth_stale"
