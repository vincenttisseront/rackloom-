"""F23 : passerelle de télémétrie — agents mTLS, isolation projet, quotas.

Réception HTTPS de métriques ou journaux d'agents enrôlés. L'identité authentifiée
(empreinte du certificat client) prime sur toute identité déclarée dans le contenu :
un agent lié au projet A ne peut pas écrire pour B même s'il le déclare. Quotas,
rétention et limite de taille par projet. La clé privée du certificat n'est affichée
qu'une fois à l'enrôlement et n'est jamais journalisée.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .. import audit, clock
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_admin, require_project
from ..ids import uuid7

log = logging.getLogger("rackloom.telemetry")

CERT_HEADER = "x-client-cert-sha256"

KINDS = frozenset({"metrics", "logs"})

DEFAULT_MAX_PAYLOAD_BYTES = 65_536
DEFAULT_QUOTA_BYTES = 1_048_576
DEFAULT_WINDOW_SECONDS = 3_600
DEFAULT_RETENTION_DAYS = 7


def _ca_paths(keys_dir: Path) -> tuple[Path, Path]:
    return keys_dir / "telemetry-ca.key", keys_dir / "telemetry-ca.pem"


def ensure_telemetry_ca(keys_dir: Path) -> tuple[Path, Path]:
    """CA locale pour certificats clients d'agents de télémétrie (0600)."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = _ca_paths(keys_dir)
    if key_path.exists() and cert_path.exists():
        return key_path, cert_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Rackloom"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Rackloom Telemetry CA"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)
    return key_path, cert_path


def issue_client_cert(
    keys_dir: Path,
    *,
    common_name: str,
    days: int = 365,
) -> dict[str, Any]:
    """Émet un certificat client signé par la CA télémétrie. Clé privée jamais stockée."""
    key_path, _ = ensure_telemetry_ca(keys_dir)
    ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(ca_key, rsa.RSAPrivateKey)
    ca_cert = x509.load_pem_x509_certificate((_ca_paths(keys_dir)[1]).read_bytes())
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = clock.now()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Rackloom"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64]),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "subject": cert.subject.rfc4514_string(),
        "not_before": cert.not_valid_before_utc,
        "not_after": cert.not_valid_after_utc,
    }


def fingerprint_pem(pem: bytes | str) -> str:
    data = pem.encode() if isinstance(pem, str) else pem
    cert = x509.load_pem_x509_certificate(data)
    return cert.fingerprint(hashes.SHA256()).hex()


def ensure_limits(conn: Conn, project_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM telemetry_project_limits WHERE project_id = %s", (project_id,)).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        """
        INSERT INTO telemetry_project_limits (
            project_id, max_payload_bytes, quota_bytes_per_window, window_seconds, retention_days
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (project_id) DO NOTHING
        RETURNING *
        """,
        (
            project_id,
            DEFAULT_MAX_PAYLOAD_BYTES,
            DEFAULT_QUOTA_BYTES,
            DEFAULT_WINDOW_SECONDS,
            DEFAULT_RETENTION_DAYS,
        ),
    ).fetchone()
    if row is not None:
        return row
    row = conn.execute("SELECT * FROM telemetry_project_limits WHERE project_id = %s", (project_id,)).fetchone()
    assert row is not None
    return row


def set_limits(
    conn: Conn,
    actor: Principal,
    project_id: UUID,
    *,
    max_payload_bytes: int | None = None,
    quota_bytes_per_window: int | None = None,
    window_seconds: int | None = None,
    retention_days: int | None = None,
) -> dict[str, Any]:
    require_admin(actor)
    current = ensure_limits(conn, project_id)
    row = conn.execute(
        """
        UPDATE telemetry_project_limits SET
            max_payload_bytes = COALESCE(%s, max_payload_bytes),
            quota_bytes_per_window = COALESCE(%s, quota_bytes_per_window),
            window_seconds = COALESCE(%s, window_seconds),
            retention_days = COALESCE(%s, retention_days),
            updated_at = now()
        WHERE project_id = %s
        RETURNING *
        """,
        (
            max_payload_bytes,
            quota_bytes_per_window,
            window_seconds,
            retention_days,
            project_id,
        ),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(actor.user_id),
        action="telemetry.limits_updated",
        outcome="succeeded",
        project_id=project_id,
        correlation_id=None,
        targets={
            "before": {
                "max_payload_bytes": current["max_payload_bytes"],
                "quota_bytes_per_window": current["quota_bytes_per_window"],
                "window_seconds": current["window_seconds"],
                "retention_days": current["retention_days"],
            },
            "after": {
                "max_payload_bytes": row["max_payload_bytes"],
                "quota_bytes_per_window": row["quota_bytes_per_window"],
                "window_seconds": row["window_seconds"],
                "retention_days": row["retention_days"],
            },
        },
    )
    return limits_view(row)


def limits_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": str(row["project_id"]),
        "max_payload_bytes": row["max_payload_bytes"],
        "quota_bytes_per_window": row["quota_bytes_per_window"],
        "window_seconds": row["window_seconds"],
        "retention_days": row["retention_days"],
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def agent_view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    project = conn.execute("SELECT slug, name FROM projects WHERE id = %s", (row["project_id"],)).fetchone()
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "project_id": str(row["project_id"]),
        "project_slug": project["slug"] if project else None,
        "project_name": project["name"] if project else None,
        "cert_fingerprint_sha256": row["cert_fingerprint_sha256"],
        "cert_subject": row["cert_subject"],
        "cert_not_before": row["cert_not_before"].isoformat() if row["cert_not_before"] else None,
        "cert_not_after": row["cert_not_after"].isoformat() if row["cert_not_after"] else None,
        "status": row["status"],
        "last_seen_at": row["last_seen_at"].isoformat() if row.get("last_seen_at") else None,
        "enrolled_at": row["enrolled_at"].isoformat() if row.get("enrolled_at") else None,
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
    }


def enroll(
    conn: Conn,
    actor: Principal,
    *,
    slug: str,
    name: str,
    project_id: UUID,
    keys_dir: Path,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Enrôle un agent de télémétrie pour un projet : certificat client (clé une seule fois)."""
    require_admin(actor)
    project = conn.execute("SELECT id, slug FROM projects WHERE id = %s", (project_id,)).fetchone()
    if project is None:
        raise ApiError(404, "not_found", "projet introuvable")
    ensure_limits(conn, project_id)
    issued = issue_client_cert(keys_dir, common_name=f"telemetry:{slug}")
    agent_id = uuid7()
    row = conn.execute(
        """
        INSERT INTO telemetry_agents (
            id, slug, name, project_id, cert_fingerprint_sha256,
            cert_subject, cert_not_before, cert_not_after, status, enrolled_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'enrolled', %s)
        RETURNING *
        """,
        (
            agent_id,
            slug,
            name,
            project_id,
            issued["fingerprint_sha256"],
            issued["subject"],
            issued["not_before"],
            issued["not_after"],
            actor.user_id,
        ),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(actor.user_id),
        action="telemetry.agent_enrolled",
        outcome="succeeded",
        project_id=project_id,
        correlation_id=None,
        request_id=request_id,
        targets={"agent_id": str(agent_id), "slug": slug, "project": project["slug"]},
    )
    return {
        "agent": agent_view(conn, row),
        "client_certificate": {
            "cert_pem": issued["cert_pem"],
            "key_pem": issued["key_pem"],
            "fingerprint_sha256": issued["fingerprint_sha256"],
            "not_before": issued["not_before"].isoformat(),
            "not_after": issued["not_after"].isoformat(),
        },
        "ca_pem": (_ca_paths(keys_dir)[1]).read_text(encoding="utf-8"),
    }


def list_agents(conn: Conn, actor: Principal, *, project_id: UUID | None = None) -> list[dict[str, Any]]:
    if not (actor.is_platform_admin or actor.is_global_auditor):
        if project_id is None:
            raise ApiError(403, "forbidden", "rôle administrateur ou auditeur requis")
        require_project(actor, project_id)
    if project_id is not None:
        rows = conn.execute(
            "SELECT * FROM telemetry_agents WHERE project_id = %s ORDER BY enrolled_at DESC",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM telemetry_agents ORDER BY enrolled_at DESC").fetchall()
    return [agent_view(conn, r) for r in rows]


def revoke(
    conn: Conn,
    actor: Principal,
    agent_id: UUID,
    *,
    reason: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    require_admin(actor)
    row = conn.execute(
        """
        UPDATE telemetry_agents
        SET status = 'revoked', revoked_at = now(), revoked_by = %s, revoke_reason = %s
        WHERE id = %s AND status <> 'revoked'
        RETURNING *
        """,
        (actor.user_id, reason, agent_id),
    ).fetchone()
    if row is None:
        existing = conn.execute("SELECT * FROM telemetry_agents WHERE id = %s", (agent_id,)).fetchone()
        if existing is None:
            raise ApiError(404, "not_found", "agent de télémétrie introuvable")
        return agent_view(conn, existing)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(actor.user_id),
        action="telemetry.agent_revoked",
        outcome="succeeded",
        project_id=row["project_id"],
        correlation_id=None,
        request_id=request_id,
        targets={"agent_id": str(agent_id), "slug": row["slug"], "reason": reason},
    )
    return agent_view(conn, row)


def resolve_by_fingerprint(conn: Conn, fingerprint: str) -> dict[str, Any] | None:
    fp = fingerprint.strip().lower().replace(":", "")
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        return None
    return conn.execute(
        "SELECT a.*, p.slug AS project_slug "
        "FROM telemetry_agents a JOIN projects p ON p.id = a.project_id "
        "WHERE a.cert_fingerprint_sha256 = %s",
        (fp,),
    ).fetchone()


def authenticate_agent(conn: Conn, fingerprint: str | None) -> dict[str, Any]:
    """Authentifie un agent par empreinte de certificat client (mTLS)."""
    if not fingerprint:
        raise ApiError(401, "unauthenticated", "certificat client mTLS absent")
    row = resolve_by_fingerprint(conn, fingerprint)
    if row is None:
        raise ApiError(401, "unauthenticated", "agent non enrôlé ou empreinte inconnue")
    if row["status"] == "revoked":
        raise ApiError(403, "agent_revoked", "agent de télémétrie révoqué")
    now = clock.now()
    if row["cert_not_after"] < now:
        raise ApiError(403, "agent_cert_expired", "certificat client expiré")
    if row["cert_not_before"] > now:
        raise ApiError(403, "agent_cert_not_yet_valid", "certificat client pas encore valide")
    return row


def _window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    aligned = epoch - (epoch % window_seconds)
    return datetime.fromtimestamp(aligned, tz=UTC)


def _payload_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def purge_expired(conn: Conn, project_id: UUID | None = None) -> int:
    """Supprime les échantillons expirés (rétention). Retourne le nombre de lignes."""
    now = clock.now()
    if project_id is None:
        cur = conn.execute("DELETE FROM telemetry_samples WHERE expires_at <= %s", (now,))
    else:
        cur = conn.execute(
            "DELETE FROM telemetry_samples WHERE project_id = %s AND expires_at <= %s",
            (project_id, now),
        )
    return cur.rowcount or 0


def ingest(
    conn: Conn,
    agent: dict[str, Any],
    *,
    kind: str,
    payload: Any,
    declared_project: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Ingestion d'un échantillon. L'identité mTLS fixe le projet ; un projet usurpé est refusé."""
    if kind not in KINDS:
        raise ApiError(422, "invalid_kind", f"kind « {kind} » inconnu", field="/kind")
    if payload is None:
        raise ApiError(422, "invalid_payload", "payload requis", field="/payload")

    project_id: UUID = agent["project_id"]
    project_slug: str = agent["project_slug"]

    # L'identité authentifiée prime : un projet déclaré différent est refusé.
    if declared_project is not None and declared_project.strip() and declared_project.strip() != project_slug:
        audit.record(
            conn,
            actor_type="telemetry_agent",
            actor_id=str(agent["id"]),
            action="telemetry.ingest_refused",
            outcome="refused",
            project_id=project_id,
            correlation_id=None,
            request_id=request_id,
            targets={
                "code": "project_spoofed",
                "declared_project": declared_project.strip(),
                "authenticated_project": project_slug,
            },
        )
        raise ApiError(
            403,
            "project_spoofed",
            "le projet déclaré ne correspond pas à l'identité authentifiée de l'agent",
            field="/project",
            commit=True,
        )

    limits = ensure_limits(conn, project_id)
    byte_size = _payload_bytes(payload)
    if byte_size > limits["max_payload_bytes"]:
        raise ApiError(
            413,
            "payload_too_large",
            f"échantillon de {byte_size} octets au-delà de la limite ({limits['max_payload_bytes']} octets)",
            field="/payload",
        )

    now = clock.now()
    window_start = _window_start(now, limits["window_seconds"])
    usage = conn.execute(
        "SELECT bytes_accepted FROM telemetry_usage_windows WHERE project_id = %s AND window_start = %s",
        (project_id, window_start),
    ).fetchone()
    used = int(usage["bytes_accepted"]) if usage else 0
    if used + byte_size > limits["quota_bytes_per_window"]:
        audit.record(
            conn,
            actor_type="telemetry_agent",
            actor_id=str(agent["id"]),
            action="telemetry.ingest_refused",
            outcome="refused",
            project_id=project_id,
            correlation_id=None,
            request_id=request_id,
            targets={
                "code": "quota_exceeded",
                "used": used,
                "limit": limits["quota_bytes_per_window"],
                "byte_size": byte_size,
                "window_start": window_start.isoformat(),
            },
        )
        raise ApiError(
            429,
            "quota_exceeded",
            "quota de télémétrie du projet dépassé pour la fenêtre courante",
            details={
                "used_bytes": used,
                "limit_bytes": limits["quota_bytes_per_window"],
                "window_start": window_start.isoformat(),
                "window_seconds": limits["window_seconds"],
            },
            commit=True,
        )

    purge_expired(conn, project_id)
    expires_at = now + timedelta(days=int(limits["retention_days"]))
    sample_id = uuid7()
    row = conn.execute(
        """
        INSERT INTO telemetry_samples (
            id, project_id, agent_id, kind, received_at, expires_at,
            byte_size, payload, declared_project, request_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            sample_id,
            project_id,
            agent["id"],
            kind,
            now,
            expires_at,
            byte_size,
            Jsonb(payload),
            declared_project.strip() if declared_project else None,
            request_id,
        ),
    ).fetchone()
    assert row is not None

    conn.execute(
        """
        INSERT INTO telemetry_usage_windows (project_id, window_start, bytes_accepted, samples_accepted)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (project_id, window_start) DO UPDATE SET
            bytes_accepted = telemetry_usage_windows.bytes_accepted + EXCLUDED.bytes_accepted,
            samples_accepted = telemetry_usage_windows.samples_accepted + 1
        """,
        (project_id, window_start, byte_size),
    )
    conn.execute(
        """
        UPDATE telemetry_agents
        SET last_seen_at = %s,
            status = CASE WHEN status IN ('enrolled', 'active') THEN 'active' ELSE status END
        WHERE id = %s AND status <> 'revoked'
        """,
        (now, agent["id"]),
    )

    return {
        "id": str(row["id"]),
        "project_slug": project_slug,
        "agent_slug": agent["slug"],
        "kind": row["kind"],
        "byte_size": row["byte_size"],
        "received_at": row["received_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "quota": {
            "used_bytes": used + byte_size,
            "limit_bytes": limits["quota_bytes_per_window"],
            "window_start": window_start.isoformat(),
            "window_seconds": limits["window_seconds"],
        },
    }


def sample_view(row: dict[str, Any], *, include_payload: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "agent_id": str(row["agent_id"]),
        "kind": row["kind"],
        "byte_size": row["byte_size"],
        "received_at": row["received_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "declared_project": row.get("declared_project"),
    }
    if include_payload:
        out["payload"] = row["payload"]
    return out


def list_samples(
    conn: Conn,
    actor: Principal,
    project_id: UUID,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    require_project(actor, project_id)
    purge_expired(conn, project_id)
    limits = ensure_limits(conn, project_id)
    lim = max(1, min(limit, 200))
    if kind is not None:
        if kind not in KINDS:
            raise ApiError(422, "invalid_kind", f"kind « {kind} » inconnu", field="/kind")
        rows = conn.execute(
            "SELECT * FROM telemetry_samples WHERE project_id = %s AND kind = %s "
            "AND expires_at > %s ORDER BY received_at DESC LIMIT %s",
            (project_id, kind, clock.now(), lim),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM telemetry_samples WHERE project_id = %s AND expires_at > %s "
            "ORDER BY received_at DESC LIMIT %s",
            (project_id, clock.now(), lim),
        ).fetchall()
    now = clock.now()
    window_start = _window_start(now, limits["window_seconds"])
    usage = conn.execute(
        "SELECT bytes_accepted, samples_accepted FROM telemetry_usage_windows "
        "WHERE project_id = %s AND window_start = %s",
        (project_id, window_start),
    ).fetchone()
    return {
        "limits": limits_view(limits),
        "quota": {
            "used_bytes": int(usage["bytes_accepted"]) if usage else 0,
            "samples_accepted": int(usage["samples_accepted"]) if usage else 0,
            "limit_bytes": limits["quota_bytes_per_window"],
            "window_start": window_start.isoformat(),
            "window_seconds": limits["window_seconds"],
        },
        "samples": [sample_view(r) for r in rows],
    }
