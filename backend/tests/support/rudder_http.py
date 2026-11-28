"""Serveur HTTPS local imitant l'API Rudder (fixtures enregistrées).

Aucun Rudder réel (D2). Utilisé par les tests de contrat RAC-36.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rudder"
NOW = datetime.now(UTC)
HOST = "localhost"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn: str = "Rackloom Rudder Test CA") -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(
    ca: tuple[ec.EllipticCurvePrivateKey, x509.Certificate], host: str = HOST
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def pem_of(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def load_node_fixture(name: str = "node-demo.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _RudderHandler(BaseHTTPRequestHandler):
    token: str = "test-token"
    nodes: dict[str, dict[str, Any]] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        auth = self.headers.get("X-API-Token") or ""
        if auth != self.token:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        if self.path.startswith("/api/latest/nodes/"):
            node_id = self.path.rsplit("/", 1)[-1]
            payload = self.nodes.get(node_id)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not_found"}')
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@contextmanager
def rudder_https_server(
    tmp_path: Path,
    *,
    token: str = "rudder-lab-token",  # noqa: S107 — jeton de fixture local
    node_id: str = "rudder-node-lab-001",
    fixture: str = "node-demo.json",
) -> Iterator[dict[str, Any]]:
    """Démarre un serveur HTTPS jetable servant la fixture Rudder."""
    ca = make_ca()
    leaf = make_leaf(ca, HOST)
    chain = tmp_path / "rudder-srv.pem"
    chain.write_bytes(
        leaf[0].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + pem_of(leaf[1]).encode()
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain)

    payload = load_node_fixture(fixture)
    handler = type(
        "BoundRudderHandler",
        (_RudderHandler,),
        {"token": token, "nodes": {node_id: payload}},
    )
    # Port libre sur 127.0.0.1 ; le certificat couvre localhost (SNI).
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = HTTPServer(("127.0.0.1", port), handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": f"https://{HOST}:{port}",
            "host": HOST,
            "port": port,
            "ca_pem": pem_of(ca[1]),
            "token": token,
            "node_id": node_id,
            "fixture": payload,
        }
    finally:
        httpd.shutdown()
        thread.join(2)
