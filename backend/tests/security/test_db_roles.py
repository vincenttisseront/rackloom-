"""Droits des rôles de connexion PostgreSQL de l'API et du worker.

Les autres tests se connectent en propriétaire ; ceux-ci rejouent les requêtes sous
`SET ROLE` pour vérifier les GRANT de la migration. Niveau de preuve : testé localement.
"""

from __future__ import annotations

import psycopg
import pytest

ALLOWED = {
    "rackloom_api": [
        "SELECT version_num FROM alembic_version",
        "SELECT id, ref, key_id FROM secrets",
        "SELECT id_hash FROM sessions",
        "SELECT count(*) FROM audit_log",
        "SELECT id, reason FROM environment_freezes",
        "UPDATE environment_freezes SET lifted_at = lifted_at WHERE false",
        "SELECT timezone FROM environment_calendars",
        "SELECT id FROM change_windows",
        "SELECT id, reason FROM calendar_freezes",
        "SELECT id, reviewed FROM catalog_source_pins",
        "SELECT environment_kind FROM template_promotions",
        "SELECT slug FROM ipam_pools",
        "SELECT state FROM ipam_reservations",
        "SELECT status FROM drift_scans",
        "SELECT kind FROM drift_findings",
        "SELECT rule_id FROM policy_exceptions",
        "SELECT role FROM temporary_grants",
        "SELECT reason FROM emergency_interventions",
        "SELECT user_id FROM emergency_allowlist",
    ],
    "rackloom_worker": [
        "SELECT version_num FROM alembic_version",
        "SELECT ciphertext FROM secrets",
        "SELECT id, user_id, revoked_at FROM sessions",
        "SELECT count(*) FROM audit_log",
        "SELECT id, reason FROM environment_freezes",
        "SELECT timezone FROM environment_calendars",
        "SELECT id FROM change_windows",
        "SELECT id, reason FROM calendar_freezes",
        "SELECT id, reviewed FROM catalog_source_pins",
        "SELECT environment_kind FROM template_promotions",
        "SELECT slug FROM ipam_pools",
        "SELECT state FROM ipam_reservations",
        "SELECT status FROM drift_scans",
        "SELECT kind FROM drift_findings",
        "SELECT rule_id FROM policy_exceptions",
        "SELECT role FROM temporary_grants",
        "SELECT reason FROM emergency_interventions",
        "SELECT user_id FROM emergency_allowlist",
    ],
}

DENIED = {
    "rackloom_api": [
        "SELECT ciphertext FROM secrets",
        "UPDATE audit_log SET action = action",
        "DELETE FROM audit_log",
        "DELETE FROM secrets",
        "UPDATE alembic_version SET version_num = version_num",
        "DELETE FROM environment_freezes",
        "DELETE FROM change_windows",
        "DELETE FROM calendar_freezes",
    ],
    "rackloom_worker": [
        "SELECT id_hash FROM sessions",
        "SELECT csrf_hash FROM sessions",
        "SELECT * FROM login_attempts",
        "SELECT * FROM bootstrap_tokens",
        "SELECT * FROM idempotency_keys",
        "UPDATE audit_log SET action = action",
        "DELETE FROM audit_log",
        "UPDATE alembic_version SET version_num = version_num",
        # Le worker lit le gel (R080) et le calendrier (R090) sans pouvoir les modifier.
        "INSERT INTO environment_freezes (id) VALUES (gen_random_uuid())",
        "UPDATE environment_freezes SET lifted_at = now()",
        "INSERT INTO change_windows (id) VALUES (gen_random_uuid())",
        "INSERT INTO calendar_freezes (id) VALUES (gen_random_uuid())",
    ],
}


def _run_as(conn: psycopg.Connection, role: str, query: str) -> None:
    with conn.transaction(force_rollback=True):
        conn.execute(f"SET LOCAL ROLE {role}")
        conn.execute(query)


@pytest.mark.parametrize(("role", "query"), [(r, q) for r, qs in ALLOWED.items() for q in qs])
def test_role_allowed(conn: psycopg.Connection, role: str, query: str) -> None:
    _run_as(conn, role, query)


@pytest.mark.parametrize(("role", "query"), [(r, q) for r, qs in DENIED.items() for q in qs])
def test_role_denied(conn: psycopg.Connection, role: str, query: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run_as(conn, role, query)


def test_api_scelle_et_remplace_un_secret_sans_le_relire(conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    """Saisie d'un secret de connecteur par l'API (route admin) : création puis remplacement."""
    from rackloom import secrets, security
    from rackloom.config import get_settings

    security.generate_keys(tmp_path)
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path))
    get_settings.cache_clear()
    with conn.transaction(force_rollback=True):
        conn.execute("SET LOCAL ROLE rackloom_api")
        secrets.put(conn, "secret://connectors/test/password", b"premier")
        secrets.put(conn, "secret://connectors/test/password", b"second")
        assert secrets.exists(conn, "secret://connectors/test/password")
    get_settings.cache_clear()
