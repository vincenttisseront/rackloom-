"""F15 : certificats SSH à courte durée — CA, périmètre, expiration, matériel runner.

Niveau de preuve : testé localement (cryptographie OpenSSH réelle via cryptography ;
refus d'un certificat expiré ou hors périmètre). Connexion SSH réelle : test_lab_flow /
test_f15_ssh_lab (docker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rackloom import clock, secrets, security, sshca
from rackloom.config import get_settings


@pytest.fixture
def keys(tmp_path, monkeypatch):
    security.generate_keys(tmp_path)
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_emission_courte_identite_et_perimetre(conn, keys, tmp_path):
    pub = tmp_path / "lab-keys"
    ca = sshca.ensure_lab_ca(conn, pub)
    assert (pub / "ca.pub").read_text(encoding="utf-8").strip() == ca["public_key"].strip()
    assert secrets.exists(conn, sshca.CA_SECRET_REF)

    issued = sshca.issue_for_job(
        conn,
        job_id=None,
        identity="rackloom-config",
        host_names=["lab-node-1", "lab-node-2"],
        ttl_seconds=600,
        key_id="job:test-f15",
    )
    conn.commit()

    secret = issued.runner_secret()
    assert set(secret) == {"ssh_private_key", "ssh_certificate"}
    assert "BEGIN OPENSSH PRIVATE KEY" in secret["ssh_private_key"]
    assert "ssh-ed25519-cert-v01@openssh.com" in secret["ssh_certificate"]
    # La clé de signature de la CA ne voyage jamais vers le runner.
    ca_pem = secrets.reveal(conn, sshca.CA_SECRET_REF).decode()
    assert ca_pem not in secret["ssh_private_key"]
    assert ca_pem not in secret["ssh_certificate"]
    assert ca_pem.splitlines()[0] not in secret["ssh_certificate"]

    assert issued.principals == (
        "rackloom-config@lab-node-1",
        "rackloom-config@lab-node-2",
    )
    row = conn.execute(
        "SELECT identity, principals, key_id FROM ssh_certificate_issues WHERE serial = %s",
        (issued.serial,),
    ).fetchone()
    assert row["identity"] == "rackloom-config"
    assert list(row["principals"]) == list(issued.principals)
    assert row["key_id"] == "job:test-f15"

    sshca.verify_for_host(
        issued.certificate,
        ca_public_key=ca["public_key"],
        identity="rackloom-config",
        host_name="lab-node-1",
    )


def test_certificat_expire_refuse(conn, keys, tmp_path):
    ca = sshca.ensure_lab_ca(conn, tmp_path / "lab-keys")
    now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    issued = sshca.issue_for_job(
        conn,
        job_id=None,
        identity="rackloom-collect",
        host_names=["lab-node-1"],
        valid_after=now - timedelta(hours=2),
        valid_before=now - timedelta(hours=1),
    )
    conn.commit()
    with pytest.raises(sshca.CertRejected) as exc:
        sshca.verify_for_host(
            issued.certificate,
            ca_public_key=ca["public_key"],
            identity="rackloom-collect",
            host_name="lab-node-1",
            at=now,
        )
    assert exc.value.code == "expired"


def test_mauvais_perimetre_refuse(conn, keys, tmp_path):
    ca = sshca.ensure_lab_ca(conn, tmp_path / "lab-keys")
    issued = sshca.issue_for_job(
        conn,
        job_id=None,
        identity="rackloom-config",
        host_names=["lab-node-1"],
    )
    conn.commit()
    with pytest.raises(sshca.CertRejected) as exc:
        sshca.verify_for_host(
            issued.certificate,
            ca_public_key=ca["public_key"],
            identity="rackloom-config",
            host_name="lab-node-2",
        )
    assert exc.value.code == "wrong_perimeter"

    with pytest.raises(sshca.CertRejected) as exc2:
        sshca.verify_for_host(
            issued.certificate,
            ca_public_key=ca["public_key"],
            identity="rackloom-collect",
            host_name="lab-node-1",
        )
    assert exc2.value.code == "wrong_perimeter"


def test_lab_keys_ecrit_ca_pub(conn, keys, tmp_path):
    from rackloom import lab

    pub = tmp_path / "pubs"
    with clock.frozen_at(datetime(2026, 11, 27, 14, 0, tzinfo=UTC)):
        publics = lab.generate_identities(conn, pub)
    conn.commit()
    assert "ca" in publics
    assert (pub / "ca.pub").is_file()
    assert lab.identities_present(conn, pub)
    assert sshca.ca_present(conn, pub)
