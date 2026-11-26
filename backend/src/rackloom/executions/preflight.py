"""Préflight de bout en bout (F31) : contrôles en lecture seule avant `tf_plan`.

Vérifie l'accès vCenter (TLS / endpoints approuvés), le backend de state et les
prérequis du template. Aucune écriture sur la cible, aucun secret dans les motifs.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .. import registry
from ..canonical import file_tree_digest
from ..config import Settings
from ..connectors import vsphere
from ..db import Conn
from ..redact import redact

# Motifs stables exposés à l'UI et aux transitions (jamais d'identifiant ni de secret).
MESSAGES = {
    "tls_untrusted": (
        "Confiance TLS refusée vers vCenter : certificat non reconnu ou chaîne invalide. "
        "Le plan est bloqué."
    ),
    "state_unreachable": (
        "Backend de state injoignable. Le plan est bloqué."
    ),
    "template_prereq_missing": (
        "Prérequis du template introuvables dans le catalogue. Le plan est bloqué."
    ),
    "connector_not_ready": (
        "Connecteur vCenter non prêt (configuration ou endpoint manquant). Le plan est bloqué."
    ),
}

TLS_CODES = frozenset(
    {
        "unknown_ca",
        "hostname_mismatch",
        "certificate_expired",
        "certificate_invalid",
        "tls_error",
    }
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    code: str | None = None
    message: str = ""
    simulated: bool = False


@dataclass(frozen=True)
class Result:
    ok: bool
    code: str | None
    message: str
    checks: list[Check]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "code": c.code,
                    "message": c.message,
                    "simulated": c.simulated,
                }
                for c in self.checks
            ],
        }


def _msg(code: str) -> str:
    return MESSAGES[code]


def _vsphere_check(conn: Conn, row: dict[str, Any]) -> Check:
    """Test de connexion vSphere : simulé si fictif, sinon TLS vers l'endpoint approuvé."""
    if row["mode"] == "simulated":
        return Check(
            name="vcenter",
            ok=True,
            simulated=True,
            message="Test simulé : aucun appel externe (connecteur fictif)",
        )
    url = (row.get("config") or {}).get("url")
    if not url:
        return Check(name="vcenter", ok=False, code="connector_not_ready", message=_msg("connector_not_ready"))
    host, port = vsphere.endpoint(url)
    ep = conn.execute(
        "SELECT ca_bundle_pem FROM approved_endpoints WHERE connector_id = %s AND scheme = 'https' "
        "AND host = %s AND port = %s",
        (row["id"], host, port),
    ).fetchone()
    if ep is None:
        return Check(name="vcenter", ok=False, code="connector_not_ready", message=_msg("connector_not_ready"))
    result = vsphere.test_connection(host, port, ep["ca_bundle_pem"])
    if result["status"] == "ok":
        return Check(name="vcenter", ok=True, simulated=False, message=result["message"])
    code = result.get("code") or "tls_error"
    if code in TLS_CODES:
        return Check(name="vcenter", ok=False, code="tls_untrusted", message=_msg("tls_untrusted"))
    if code in ("unreachable", "endpoint_not_approved", "not_configured"):
        return Check(name="vcenter", ok=False, code="connector_not_ready", message=_msg("connector_not_ready"))
    return Check(name="vcenter", ok=False, code="tls_untrusted", message=_msg("tls_untrusted"))


def check_vcenter(conn: Conn) -> list[Check]:
    rows = conn.execute(
        "SELECT * FROM connectors WHERE kind = 'vsphere' ORDER BY slug"
    ).fetchall()
    if not rows:
        return [Check(name="vcenter", ok=False, code="connector_not_ready", message=_msg("connector_not_ready"))]
    return [_vsphere_check(conn, row) for row in rows]


def _probe_https(url: str, timeout: float = 3.0) -> Check:
    """Sonde HTTPS en lecture seule (pas d'écriture de state)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return Check(name="state", ok=False, code="state_unreachable", message=_msg("state_unreachable"))
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
    except ssl.SSLError:
        # Un backend joignable mais à CA inconnue reste un échec d'accès pour le préflight state.
        return Check(name="state", ok=False, code="state_unreachable", message=_msg("state_unreachable"))
    except OSError:
        return Check(name="state", ok=False, code="state_unreachable", message=_msg("state_unreachable"))
    return Check(name="state", ok=True, message="Backend de state joignable (sonde en lecture seule)")


def check_state(stack: dict[str, Any], settings: Settings) -> Check:
    """Accès au backend de state sans modifier le state."""
    kind = stack.get("backend_kind") or "local_demo"
    ref = stack.get("backend_ref") or ""
    if kind == "local_demo" or ref.startswith("local://"):
        path = settings.data_dir / "tfstate" / stack["slug"]
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".rackloom-preflight"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            return Check(name="state", ok=False, code="state_unreachable", message=_msg("state_unreachable"))
        return Check(
            name="state",
            ok=True,
            simulated=True,
            message="State de démo accessible (dossier local, aucun changement)",
        )
    if kind in ("http", "https", "remote_http") or ref.startswith(("http://", "https://")):
        return _probe_https(ref)
    return Check(name="state", ok=False, code="state_unreachable", message=_msg("state_unreachable"))


def check_template(conn: Conn, resolution: dict[str, Any], settings: Settings) -> list[Check]:
    """Prérequis catalogue : module Terraform et rôles Ansible présents, empreintes intactes."""
    checks: list[Check] = []
    tf = resolution.get("terraform") or {}
    module, version = tf.get("module"), tf.get("version")
    missing = _msg("template_prereq_missing")
    if not module or not version:
        return [Check(name="template", ok=False, code="template_prereq_missing", message=missing)]
    src = settings.catalog_dir / "terraform" / module / version
    digest_ok = src.is_dir() and file_tree_digest(src, ("*.tf", ".terraform.lock.hcl")) == tf.get("digest")
    if not digest_ok:
        checks.append(Check(name="template", ok=False, code="template_prereq_missing", message=missing))
    else:
        checks.append(Check(name="template", ok=True, message=f"Module Terraform {module}@{version} présent"))

    roles = (resolution.get("ansible") or {}).get("roles") or []
    if not roles:
        checks.append(Check(name="repos", ok=True, message="Aucun rôle Ansible requis"))
        return checks
    for role in roles:
        name, ver = role.get("name"), role.get("version")
        art = registry.artifact(conn, "ansible_role", name, ver) if name and ver else None
        path = settings.catalog_dir / "ansible" / "roles" / name / ver if name and ver else None
        if art is None or path is None or not path.is_dir():
            checks.append(
                Check(name="repos", ok=False, code="template_prereq_missing", message=_msg("template_prereq_missing"))
            )
        else:
            checks.append(Check(name="repos", ok=True, message=f"Rôle {name}@{ver} présent"))
    return checks


def run(conn: Conn, *, resolution: dict[str, Any], stack: dict[str, Any], settings: Settings) -> Result:
    """Exécute tous les contrôles. Ne mutile jamais le state Terraform ni la cible."""
    checks: list[Check] = []
    checks.extend(check_vcenter(conn))
    checks.append(check_state(stack, settings))
    checks.extend(check_template(conn, resolution, settings))

    failed = next((c for c in checks if not c.ok), None)
    if failed is None:
        return Result(ok=True, code=None, message="Préflight réussi : accès et prérequis vérifiés", checks=checks)
    code = failed.code or "template_prereq_missing"
    # Messages déjà stables ; redact au cas où un détail fuirait.
    raw = _msg(code) if code in MESSAGES else failed.message
    return Result(ok=False, code=code, message=str(redact(raw)), checks=checks)


def state_fingerprint(settings: Settings, stack_slug: str) -> dict[str, Any]:
    """Empreinte du state local pour prouver l'absence de modification (tests)."""
    path = settings.data_dir / "tfstate" / stack_slug / "terraform.tfstate"
    if not path.exists():
        return {"exists": False, "sha256": None, "size": 0}
    data = path.read_bytes()
    import hashlib

    return {"exists": True, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
