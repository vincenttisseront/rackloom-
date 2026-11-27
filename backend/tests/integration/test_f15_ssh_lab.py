"""F15 : refus SSH réel (certificat expiré / mauvais périmètre) contre un nœud de labo.

Exige Docker et l'image `rackloom/lab-node:dev`. Niveau de preuve : testé localement
lorsque le moteur de conteneurs est disponible (CI `make test` / e2e).
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rackloom import sshca

pytestmark = [pytest.mark.docker, pytest.mark.ansible]


def _ssh_try(
    *,
    address: str,
    user: str,
    private_key: str,
    certificate: str,
    known_hosts: str,
) -> tuple[int, str]:
    """Tentative SSH réelle ; code de retour OpenSSH (255 = échec d'auth/connexion)."""
    with tempfile.TemporaryDirectory(prefix="rl-f15-") as raw:
        home = Path(raw)
        key = home / "id"
        key.write_text(private_key if private_key.endswith("\n") else private_key + "\n", encoding="utf-8")
        key.chmod(0o600)
        cert = home / "id-cert.pub"
        cert.write_text(certificate if certificate.endswith("\n") else certificate + "\n", encoding="utf-8")
        kh = home / "known_hosts"
        kh.write_text(known_hosts, encoding="utf-8")
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                f"UserKnownHostsFile={kh}",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"IdentityFile={key}",
                "-o",
                f"CertificateFile={cert}",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                f"{user}@{address}",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return proc.returncode, (proc.stderr or "")[-400:]


def test_ssh_cert_ok_expire_et_mauvais_perimetre(conn, worker_env, lab, tmp_path):
    lab.start(2)
    node_a, node_b = lab.nodes[0], lab.nodes[1]
    ca_pub = sshca.active_lab_ca_public(conn)

    # Ancres déjà importées par lab.start → known_hosts.
    from rackloom import hosts as hosts_mod

    kh = hosts_mod.known_hosts(conn, [node_a["address"], node_b["address"]])

    good = sshca.issue_for_job(
        conn,
        job_id=None,
        identity="rackloom-config",
        host_names=[node_a["name"]],
        ttl_seconds=600,
    )
    conn.commit()
    rc, err = _ssh_try(
        address=node_a["address"],
        user="rackloom-config",
        private_key=good.private_key,
        certificate=good.certificate,
        known_hosts=kh,
    )
    assert rc == 0, err

    # Même certificat sur l'autre nœud (mauvais périmètre) → refusé.
    rc_bad, err_bad = _ssh_try(
        address=node_b["address"],
        user="rackloom-config",
        private_key=good.private_key,
        certificate=good.certificate,
        known_hosts=kh,
    )
    assert rc_bad != 0, "certificat d'un autre périmètre accepté à tort"
    assert "Permission denied" in err_bad or rc_bad == 255

    # Certificat expiré → refusé même sur le bon nœud.
    now = datetime.now(UTC)
    expired = sshca.issue_for_job(
        conn,
        job_id=None,
        identity="rackloom-config",
        host_names=[node_a["name"]],
        valid_after=now - timedelta(hours=2),
        valid_before=now - timedelta(minutes=5),
    )
    conn.commit()
    with pytest.raises(sshca.CertRejected) as logic:
        sshca.verify_for_host(
            expired.certificate,
            ca_public_key=ca_pub,
            identity="rackloom-config",
            host_name=node_a["name"],
            at=now,
        )
    assert logic.value.code == "expired"
    rc_exp, err_exp = _ssh_try(
        address=node_a["address"],
        user="rackloom-config",
        private_key=expired.private_key,
        certificate=expired.certificate,
        known_hosts=kh,
    )
    assert rc_exp != 0, "certificat expiré accepté à tort"
    assert "Permission denied" in err_exp or rc_exp == 255

    # Port 22 joignable (évite un faux positif réseau).
    with socket.create_connection((node_a["address"], 22), timeout=2):
        pass
