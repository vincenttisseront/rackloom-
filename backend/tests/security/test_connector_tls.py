"""T19 : certificat vCenter invalide.

Serveur TLS local jetable sur 127.0.0.1, CA de test générée à chaque exécution : aucun vCenter réel n'est
contacté. Niveau de preuve : testé localement.
"""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from rackloom.connectors import capabilities_for
from rackloom.db import Jsonb
from rackloom.ids import uuid7

NOW = datetime.now(UTC)
type Pair = tuple[ec.EllipticCurvePrivateKey, x509.Certificate]


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str) -> Pair:
    key = ec.generate_private_key(ec.SECP256R1())
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca: Pair, host: str, *, expired: bool = False) -> Pair:
    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    start, end = ((NOW - timedelta(days=60), NOW - timedelta(days=30)) if expired
                  else (NOW - timedelta(days=1), NOW + timedelta(days=30)))
    cert = (x509.CertificateBuilder().subject_name(_name(host)).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@contextmanager
def tls_server(tmp_path: Path, leaf: Pair) -> Iterator[int]:
    key, cert = leaf
    chain = tmp_path / f"srv-{cert.serial_number}.pem"
    chain.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()) + _pem(cert).encode())
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain)
    srv = socket.create_server(("127.0.0.1", 0))
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            client.settimeout(5)
            with client:
                try:
                    with ctx.wrap_socket(client, server_side=True):
                        pass
                except (ssl.SSLError, OSError):
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        thread.join(2)
        srv.close()


@pytest.fixture
def vcenter(conn) -> str:
    """Connecteur vSphere réel (mode `real`), sans URL ni CA ; le profil de démo n'en contient pas."""
    cid = uuid7()
    conn.execute("INSERT INTO connectors (id, slug, kind, mode, capabilities) VALUES (%s, 'vsphere-t19', 'vsphere', "
                 "'real', %s)", (cid, Jsonb(capabilities_for("vsphere", "real"))))
    conn.commit()
    return str(cid)


def _configure(admin, cid: str, config: dict):
    return admin.put(f"/api/v1/connectors/{cid}", json={"config": config})


def _test(admin, cid: str) -> dict:
    r = admin.post(f"/api/v1/connectors/{cid}/test")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("field", ["insecure", "verify_ssl", "tls_verify", "skip_tls_verify", "thumbprint"])
def test_t19_aucun_champ_ne_desactive_tls(api, vcenter, field):
    r = _configure(api("admin"), vcenter, {"url": "https://localhost:8443", field: True})
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_config" and r.json()["field"] == f"/config/{field}"
    assert "ne se désactive pas" in r.json()["message"]


@pytest.mark.parametrize("url", ["http://localhost:8443", "https://user@localhost", "https://localhost/sdk?x=1"])
def test_t19_url_https_stricte(api, vcenter, url):
    r = _configure(api("admin"), vcenter, {"url": url})
    assert (r.status_code, r.json()["code"], r.json()["field"]) == (422, "invalid_config", "/config/url")


def test_t19_ca_importee_doit_etre_une_autorite(api, vcenter):
    leaf = _leaf(_ca("Rackloom T19 CA"), "localhost")
    r = _configure(api("admin"), vcenter, {"url": "https://localhost:8443", "ca_pem": _pem(leaf[1])})
    assert (r.status_code, r.json()["field"]) == (422, "/config/ca_pem")
    r = _configure(api("admin"), vcenter, {"url": "https://localhost:8443", "ca_pem": "pas un certificat"})
    assert (r.status_code, r.json()["field"]) == (422, "/config/ca_pem")


def test_t19_certificats_invalides_refuses_puis_ca_importee_acceptee(api, conn, vcenter, tmp_path):
    ca, other = _ca("Rackloom T19 CA"), _ca("Autre CA T19")
    admin = api("admin")
    cases = [  # certificat présenté, CA importée, motif attendu
        (_leaf(ca, "localhost"), None, "unknown_ca"),
        (_leaf(ca, "localhost"), other, "unknown_ca"),
        (_leaf(ca, "vcenter.demo.invalid"), ca, "hostname_mismatch"),
        (_leaf(ca, "localhost", expired=True), ca, "certificate_expired"),
    ]
    for leaf, imported, code in cases:
        with tls_server(tmp_path, leaf) as port:
            config = {"url": f"https://localhost:{port}"}
            if imported is not None:
                config["ca_pem"] = _pem(imported[1])
            assert _configure(admin, vcenter, config).status_code == 200
            res = _test(admin, vcenter)
            assert (res["status"], res["code"], res["simulated"]) == ("failed", code, False), res
            assert res["message"]

    row = conn.execute("SELECT error_count, last_test_result FROM connectors WHERE id = %s", (vcenter,)).fetchone()
    assert row["error_count"] == len(cases) and row["last_test_result"]["code"] == "certificate_expired"
    audit = conn.execute("SELECT outcome FROM audit_log WHERE action = 'connector.test' "
                         "AND targets->>'connector' = 'vsphere-t19' ORDER BY id").fetchall()
    assert [a["outcome"] for a in audit] == ["failed"] * len(cases)

    with tls_server(tmp_path, _leaf(ca, "localhost")) as port:
        r = _configure(admin, vcenter, {"url": f"https://localhost:{port}", "ca_pem": _pem(ca[1])})
        assert r.status_code == 200 and r.json()["config"] == {"url": f"https://localhost:{port}", "ca_imported": True}
        res = _test(admin, vcenter)
    assert (res["status"], res["simulated"]) == ("ok", False) and "nom d'hôte" in res["message"]
    endpoint = conn.execute("SELECT scheme, host, port, ca_bundle_pem FROM approved_endpoints WHERE connector_id = %s "
                            "AND port = %s", (vcenter, port)).fetchone()
    assert (endpoint["scheme"], endpoint["host"], endpoint["ca_bundle_pem"]) == ("https", "localhost", _pem(ca[1]))
    after = conn.execute("SELECT error_count FROM connectors WHERE id = %s", (vcenter,)).fetchone()
    assert after["error_count"] == len(cases)


def test_t19_endpoint_non_approuve(api, conn, vcenter):
    conn.execute("UPDATE connectors SET config = %s WHERE id = %s", (Jsonb({"url": "https://localhost:9"}), vcenter))
    conn.commit()
    res = _test(api("admin"), vcenter)
    assert (res["status"], res["code"]) == ("failed", "endpoint_not_approved")


def test_t19_connecteur_fictif_reste_simule(api, conn):
    fake = conn.execute("SELECT id FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    res = _test(api("admin"), str(fake["id"]))
    assert (res["status"], res["simulated"]) == ("ok", True)
    r = _configure(api("admin"), str(fake["id"]), {"insecure": True})
    assert (r.status_code, r.json()["field"]) == (422, "/config/insecure")
