"""Connecteur vSphere : configuration stricte et test de connexion TLS (T19).

Aucun champ ne désactive la vérification TLS : la CA du vCenter s'importe, le nom d'hôte est toujours
vérifié, et la sortie réseau n'a lieu que vers un endpoint approuvé (`approved_endpoints`).

Sous D2, l'inventaire et le suivi restent simulés (`vsphere-fake`, badge « Simulé ») ; le parcours
Lot 2-D2 s'exerce sur Terraform `lab-vm` et Ansible `lab-node` (voir `docs/lot-2-plan.md`).
La session REST, l'inventaire et le suivi réels restent hors périmètre tant qu'un laboratoire
vSphere n'est pas autorisé (`docs/lab-vsphere-checklist.md`).
"""

from __future__ import annotations

import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from pydantic import BaseModel, ConfigDict, field_validator

MAX_CA_PEM = 64 * 1024

REASONS = {
    "unknown_ca": "certificat émis par une autorité inconnue : importez la CA du vCenter",
    "hostname_mismatch": "le certificat ne correspond pas au nom d'hôte configuré",
    "certificate_expired": "certificat expiré ou pas encore valide",
    "certificate_invalid": "certificat refusé",
    "tls_error": "négociation TLS en échec",
    "unreachable": "hôte injoignable",
    "endpoint_not_approved": "endpoint non approuvé : enregistrez l'URL du connecteur",
    "not_configured": "URL du vCenter non configurée",
}
# Codes de vérification OpenSSL (X509_V_ERR_*).
VERIFY_CODES = {9: "certificate_expired", 10: "certificate_expired", 18: "unknown_ca", 19: "unknown_ca",
                20: "unknown_ca", 21: "unknown_ca", 62: "hostname_mismatch"}


class VsphereConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str | None = None
    ca_pem: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("https obligatoire")
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("URL attendue sous la forme https://hôte[:port]")
        if parts.path not in ("", "/"):
            raise ValueError("URL attendue sous la forme https://hôte[:port]")
        _ = parts.port
        return f"https://{parts.netloc}"

    @field_validator("ca_pem")
    @classmethod
    def _ca(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_CA_PEM:
            raise ValueError("CA trop volumineuse")
        try:
            certs = x509.load_pem_x509_certificates(value.encode())
        except ValueError as exc:
            raise ValueError("CA illisible : certificat PEM attendu") from exc
        for cert in certs:
            try:
                is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            except x509.ExtensionNotFound:
                is_ca = False
            if not is_ca:
                raise ValueError("le PEM importé doit contenir des certificats d'autorité")
        return value


def endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or 443


def failure(code: str) -> dict[str, Any]:
    return {"status": "failed", "simulated": False, "code": code, "message": REASONS[code]}


def test_connection(host: str, port: int, ca_pem: str | None, timeout: float = 5.0) -> dict[str, Any]:
    """Négociation TLS avec vérification complète (chaîne, validité, nom d'hôte). Aucun repli."""
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            version = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return failure(VERIFY_CODES.get(exc.verify_code, "certificate_invalid"))
    except ssl.SSLError:
        return failure("tls_error")
    except OSError:
        return failure("unreachable")
    return {
        "status": "ok",
        "simulated": False,
        "message": (
            f"TLS vérifié ({version}) : chaîne, validité et nom d'hôte ; "
            "session REST et inventaire réel après autorisation de laboratoire "
            "(docs/lab-vsphere-checklist.md)"
        ),
    }
