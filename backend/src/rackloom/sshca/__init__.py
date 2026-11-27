"""F15 : certificats SSH utilisateur à courte durée.

La CA de labo signe une identité et un périmètre (principals) par job. La clé de
signature reste scellée côté worker (`secret://…`) et n'est jamais transmise aux
runners : seuls la clé éphémère du job et le certificat signé partent sur stdin.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import ssh

from .. import audit, clock, secrets
from ..db import Conn, Jsonb
from ..ids import uuid7

CA_SLUG = "lab-user-ca"
CA_SECRET_REF = "secret://lab/ssh-ca/signing"  # noqa: S105
CA_SETTING_KEY = "lab_ssh_ca_public_key"
DEFAULT_TTL_SECONDS = 1200
MAX_TTL_SECONDS = 3600
ALLOWED_IDENTITIES = frozenset({"rackloom-collect", "rackloom-config"})


class CertRejected(Exception):
    """Certificat expiré, hors périmètre, ou signature invalide."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class IssuedCredential:
    """Matériel remis au runner : jamais la clé de la CA."""

    private_key: str
    public_key: str
    certificate: str
    serial: int
    key_id: str
    identity: str
    principals: tuple[str, ...]
    valid_after: datetime
    valid_before: datetime
    public_key_fingerprint: str
    ca_fingerprint: str

    def runner_secret(self) -> dict[str, str]:
        return {
            "ssh_private_key": self.private_key,
            "ssh_certificate": self.certificate,
        }

    def runner_public(self) -> dict[str, str]:
        return {
            "ssh_public_key": self.public_key,
            "ssh_certificate": self.certificate,
        }


def _fingerprint_openssh(public_key_line: str) -> str:
    """Empreinte SHA256 au format OpenSSH (`SHA256:…`)."""
    blob = public_key_line.strip().split()[1]
    import base64

    raw = base64.b64decode(blob)
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _load_ca_private(conn: Conn) -> tuple[dict[str, Any], Ed25519PrivateKey]:
    row = conn.execute(
        "SELECT * FROM ssh_cas WHERE purpose = 'lab' AND active = true ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("CA SSH de labo absente (lab keys)")
    pem = secrets.reveal(conn, row["secret_ref"])
    key = serialization.load_ssh_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("CA SSH : seul ed25519 est supporté")
    return dict(row), key


def principal_for(identity: str, host_name: str) -> str:
    if identity not in ALLOWED_IDENTITIES:
        raise ValueError(f"identité SSH inconnue : {identity}")
    host = host_name.strip()
    if not host or len(host) > 80 or any(c in host for c in " \n\t@"):
        raise ValueError("nom d'hôte de périmètre invalide")
    return f"{identity}@{host}"


def ensure_lab_ca(conn: Conn, pub_dir: Path | None = None, *, rotate: bool = False) -> dict[str, str]:
    """Crée ou conserve la CA de labo ; écrit `ca.pub` si `pub_dir` est fourni."""
    existing = conn.execute(
        "SELECT * FROM ssh_cas WHERE purpose = 'lab' AND active = true ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if existing is not None and not rotate:
        if pub_dir is not None:
            pub_dir.mkdir(parents=True, exist_ok=True)
            (pub_dir / "ca.pub").write_text(existing["public_key"].rstrip() + "\n", encoding="utf-8")
        return {"public_key": existing["public_key"], "fingerprint": existing["fingerprint"]}

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public = (
        key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        + " rackloom-lab-user-ca"
    )
    fingerprint = _fingerprint_openssh(public)
    secrets.put(conn, CA_SECRET_REF, private)
    if existing is not None and rotate:
        conn.execute(
            "UPDATE ssh_cas SET active = false, rotated_at = now() WHERE id = %s",
            (existing["id"],),
        )
    ca_id = uuid7()
    conn.execute(
        "INSERT INTO ssh_cas (id, slug, public_key, fingerprint, purpose, secret_ref, active) "
        "VALUES (%s, %s, %s, %s, 'lab', %s, true)",
        (ca_id, CA_SLUG if existing is None else f"{CA_SLUG}-{ca_id.hex[:8]}", public, fingerprint, CA_SECRET_REF),
    )
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (CA_SETTING_KEY, Jsonb({"public_key": public, "fingerprint": fingerprint})),
    )
    if pub_dir is not None:
        pub_dir.mkdir(parents=True, exist_ok=True)
        (pub_dir / "ca.pub").write_text(public + "\n", encoding="utf-8")
    audit.record(
        conn,
        actor_type="system",
        actor_id="lab",
        action="lab.ssh_ca.rotate" if rotate or existing else "lab.ssh_ca.create",
        outcome="ok",
        targets={"fingerprint": fingerprint, "slug": CA_SLUG},
    )
    return {"public_key": public, "fingerprint": fingerprint}


def ca_present(conn: Conn, pub_dir: Path | None = None) -> bool:
    row = conn.execute(
        "SELECT public_key, fingerprint FROM ssh_cas WHERE purpose = 'lab' AND active = true LIMIT 1"
    ).fetchone()
    if row is None or not secrets.exists(conn, CA_SECRET_REF):
        return False
    if pub_dir is not None:
        path = pub_dir / "ca.pub"
        if not path.exists() or path.read_text(encoding="utf-8").strip() != row["public_key"].strip():
            return False
    return True


def issue_for_job(
    conn: Conn,
    *,
    job_id: UUID | None,
    identity: str,
    host_names: list[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    key_id: str | None = None,
    valid_after: datetime | None = None,
    valid_before: datetime | None = None,
) -> IssuedCredential:
    """Émet un certificat utilisateur lié au job ; la CA ne quitte pas le worker."""
    if identity not in ALLOWED_IDENTITIES:
        raise ValueError(f"identité SSH inconnue : {identity}")
    if not host_names:
        raise ValueError("au moins un hôte de périmètre est requis")
    if ttl_seconds < 60 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError("durée de certificat hors bornes")

    ca_row, ca_key = _load_ca_private(conn)
    user_key = Ed25519PrivateKey.generate()
    now = clock.now()
    after = valid_after if valid_after is not None else now - timedelta(seconds=60)
    before = valid_before if valid_before is not None else now + timedelta(seconds=ttl_seconds)
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    if before <= after:
        raise ValueError("valid_before doit être postérieur à valid_after")

    principals = tuple(principal_for(identity, name) for name in host_names)
    serial_row = conn.execute("SELECT nextval('ssh_certificate_serial') AS serial").fetchone()
    if serial_row is None:
        raise RuntimeError("séquence ssh_certificate_serial indisponible")
    serial = int(serial_row["serial"])
    kid = key_id or (f"job:{job_id}" if job_id else f"manual:{uuid7()}")

    cert = (
        ssh.SSHCertificateBuilder()
        .public_key(user_key.public_key())
        .serial(serial)
        .type(ssh.SSHCertificateType.USER)
        .key_id(kid.encode())
        .valid_principals([p.encode() for p in principals])
        .valid_after(int(after.timestamp()))
        .valid_before(int(before.timestamp()))
        .add_extension(b"permit-pty", b"")
        .add_extension(b"permit-user-rc", b"")
        .sign(ca_key)
    )
    private = user_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = (
        user_key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        + f" {kid}"
    )
    certificate = cert.public_bytes().decode().strip()
    pub_fp = _fingerprint_openssh(public)
    issue_id = uuid7()
    conn.execute(
        "INSERT INTO ssh_certificate_issues "
        "(id, ca_id, job_id, serial, key_id, identity, principals, valid_after, valid_before, "
        "public_key_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            issue_id,
            ca_row["id"],
            job_id,
            serial,
            kid,
            identity,
            list(principals),
            after,
            before,
            pub_fp,
        ),
    )
    audit.record(
        conn,
        actor_type="system",
        actor_id="worker",
        action="ssh.certificate.issue",
        outcome="ok",
        targets={
            "job_id": str(job_id) if job_id else None,
            "serial": serial,
            "identity": identity,
            "principals": list(principals),
            "valid_before": before.isoformat(),
        },
    )
    return IssuedCredential(
        private_key=private,
        public_key=public,
        certificate=certificate if certificate.endswith("\n") else certificate + "\n",
        serial=serial,
        key_id=kid,
        identity=identity,
        principals=principals,
        valid_after=after,
        valid_before=before,
        public_key_fingerprint=pub_fp,
        ca_fingerprint=ca_row["fingerprint"],
    )


def parse_certificate(certificate_line: str) -> ssh.SSHCertificate:
    raw = certificate_line.strip().encode()
    loaded = ssh.load_ssh_public_identity(raw)
    if not isinstance(loaded, ssh.SSHCertificate):
        raise CertRejected("not_a_certificate", "clé publique SSH attendue sous forme de certificat")
    return loaded


def verify_for_host(
    certificate_line: str,
    *,
    ca_public_key: str,
    identity: str,
    host_name: str,
    at: datetime | None = None,
) -> ssh.SSHCertificate:
    """Refuse un certificat expiré, mal signé ou hors périmètre (principals).

    Miroir logique de TrustedUserCAKeys + AuthorizedPrincipalsFile côté OpenSSH.
    """
    cert = parse_certificate(certificate_line)
    try:
        cert.verify_cert_signature()
    except Exception as exc:  # noqa: BLE001 — surface crypto → refus métier
        raise CertRejected("bad_signature", "signature de certificat invalide") from exc

    ca_pub = serialization.load_ssh_public_key(ca_public_key.strip().encode())
    if cert.signature_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) != ca_pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw):
        raise CertRejected("unknown_ca", "certificat signé par une CA non approuvée")

    if cert.type != ssh.SSHCertificateType.USER:
        raise CertRejected("not_user_cert", "certificat utilisateur requis")

    instant = at if at is not None else clock.now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    ts = int(instant.timestamp())
    if ts < int(cert.valid_after) or ts >= int(cert.valid_before):
        raise CertRejected("expired", "certificat SSH expiré ou pas encore valide")

    expected = principal_for(identity, host_name)
    principals = [p.decode() if isinstance(p, bytes) else p for p in cert.valid_principals]
    if expected not in principals:
        raise CertRejected(
            "wrong_perimeter",
            f"périmètre refusé : principal {expected} absent du certificat",
        )
    return cert


def active_lab_ca_public(conn: Conn) -> str:
    row = conn.execute("SELECT public_key FROM ssh_cas WHERE purpose = 'lab' AND active = true LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("CA SSH de labo absente")
    return row["public_key"]
