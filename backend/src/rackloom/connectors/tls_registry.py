"""F45 : registre des certificats TLS des endpoints approuvés.

Scanne la chaîne présentée (vérification TLS toujours active), enregistre empreinte,
sujet, émetteur et dates — jamais de clé privée. Alerte le propriétaire lorsque
l'expiration approche.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .. import audit, clock
from ..db import Conn, Jsonb
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7


def _pem_list(der_list: list[bytes]) -> list[x509.Certificate]:
    out: list[x509.Certificate] = []
    for der in der_list:
        out.append(x509.load_der_x509_certificate(der))
    return out


def _scan_endpoint(host: str, port: int, ca_pem: str | None, timeout: float = 5.0) -> dict[str, Any]:
    """Négocie TLS avec vérification. Aucune option pour l'ignorer."""
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Récupérer la chaîne même après vérification réussie.
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                if not der:
                    return {"status": "untrusted", "message": "certificat pair absent"}
                cert = x509.load_der_x509_certificate(der)
                # getpeercert_chain n'existe qu'en 3.13+ ; repli sur le leaf seul.
                chain_der: list[bytes] = [der]
                get_chain = getattr(tls, "get_unverified_chain", None) or getattr(tls, "getpeercert_chain", None)
                if callable(get_chain):
                    try:
                        raw = get_chain()
                        if raw:
                            chain_der = [
                                c.public_bytes(serialization.Encoding.DER) if hasattr(c, "public_bytes") else bytes(c)
                                for c in raw
                            ]
                    except (TypeError, AttributeError, ValueError, OSError):
                        chain_der = [der]
    except ssl.SSLCertVerificationError as exc:
        return {
            "status": "untrusted",
            "message": f"chaîne ou nom d'hôte refusé ({exc.verify_message})",
            "code": "tls_untrusted",
        }
    except ssl.SSLError:
        return {"status": "untrusted", "message": "erreur TLS", "code": "tls_error"}
    except OSError:
        return {"status": "unreachable", "message": "endpoint injoignable", "code": "unreachable"}

    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    sans: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans = [str(n.value) if not isinstance(n.value, str) else n.value for n in ext.value]
    except x509.ExtensionNotFound:
        sans = []
    if hasattr(cert, "not_valid_before_utc"):
        not_before = cert.not_valid_before_utc
    else:
        not_before = cert.not_valid_before.replace(tzinfo=UTC)
    if hasattr(cert, "not_valid_after_utc"):
        not_after = cert.not_valid_after_utc
    else:
        not_after = cert.not_valid_after.replace(tzinfo=UTC)
    return {
        "status": "ok",
        "fingerprint_sha256": fingerprint,
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": not_before,
        "not_after": not_after,
        "san": sans,
        "chain_depth": max(1, len(chain_der)),
        # Preuve : aucune clé privée collectée.
        "private_key_collected": False,
    }


def _alert_status(not_after: datetime, *, alert_days: int, now: datetime) -> tuple[str, str]:
    if not_after <= now:
        return "expired", f"certificat expiré depuis {now - not_after}"
    remaining = not_after - now
    if remaining <= timedelta(days=alert_days):
        days = max(0, remaining.days)
        return "expiring_soon", f"certificat expire dans {days} jour(s) (seuil {alert_days})"
    return "ok", ""


def scan_endpoint(
    conn: Conn,
    principal: Principal,
    endpoint_id: UUID,
    *,
    owner_user_id: UUID | None = None,
    alert_days: int = 30,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not (
        principal.is_platform_admin or any(b.role in ("connector_admin", "project_admin") for b in principal.bindings)
    ):
        raise forbidden("rôle connector_admin ou administrateur requis")
    if not 1 <= alert_days <= 365:
        raise ApiError(422, "invalid_alert_days", "alert_days entre 1 et 365", field="/alert_days")
    ep = conn.execute("SELECT * FROM approved_endpoints WHERE id = %s", (endpoint_id,)).fetchone()
    if ep is None:
        raise not_found("endpoint")
    owner = owner_user_id
    if owner is None:
        existing = conn.execute(
            "SELECT owner_user_id FROM tls_certificate_records WHERE endpoint_id = %s", (endpoint_id,)
        ).fetchone()
        owner = existing["owner_user_id"] if existing else principal.user_id
    result = _scan_endpoint(ep["host"], ep["port"], ep.get("ca_bundle_pem"))
    now = clock.now()
    if result["status"] == "unreachable":
        alert_status, message = "unreachable", result["message"]
        record = _upsert_failure(conn, ep, owner, alert_days, alert_status, message, now)
    elif result["status"] == "untrusted":
        alert_status, message = "untrusted", result["message"]
        record = _upsert_failure(conn, ep, owner, alert_days, alert_status, message, now)
    else:
        alert_status, message = _alert_status(result["not_after"], alert_days=alert_days, now=now)
        record = _upsert_ok(conn, ep, owner, alert_days, result, alert_status, message, now)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="tls.certificate.scan",
        outcome=alert_status,
        targets={
            "endpoint_id": str(endpoint_id),
            "host": ep["host"],
            "port": ep["port"],
            "alert_status": alert_status,
            "owner_user_id": str(owner) if owner else None,
            "private_key_collected": False,
        },
        request_id=request_id,
    )
    return record


def _upsert_failure(
    conn: Conn,
    ep: dict[str, Any],
    owner: UUID | None,
    alert_days: int,
    alert_status: str,
    message: str,
    now: datetime,
) -> dict[str, Any]:
    existing = conn.execute("SELECT id FROM tls_certificate_records WHERE endpoint_id = %s", (ep["id"],)).fetchone()
    # Placeholder dates pour les échecs (contraintes NOT NULL) — pas un certificat réel.
    placeholder = now
    if existing:
        conn.execute(
            """
            UPDATE tls_certificate_records
            SET alert_status = %s, alert_message = %s, last_scanned_at = %s,
                owner_user_id = COALESCE(%s, owner_user_id), alert_days = %s
            WHERE endpoint_id = %s
            """,
            (alert_status, message[:500], now, owner, alert_days, ep["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO tls_certificate_records
                (id, endpoint_id, fingerprint_sha256, subject, issuer, not_before, not_after, san,
                 chain_depth, owner_user_id, alert_days, last_scanned_at, alert_status, alert_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
            """,
            (
                uuid7(),
                ep["id"],
                "0" * 64,
                "(scan failed)",
                "(scan failed)",
                placeholder,
                placeholder,
                Jsonb([]),
                owner,
                alert_days,
                now,
                alert_status,
                message[:500],
            ),
        )
    return get_record(conn, ep["id"])


def _upsert_ok(
    conn: Conn,
    ep: dict[str, Any],
    owner: UUID | None,
    alert_days: int,
    result: dict[str, Any],
    alert_status: str,
    message: str,
    now: datetime,
) -> dict[str, Any]:
    existing = conn.execute("SELECT id FROM tls_certificate_records WHERE endpoint_id = %s", (ep["id"],)).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE tls_certificate_records SET
                fingerprint_sha256 = %s, subject = %s, issuer = %s, not_before = %s, not_after = %s,
                san = %s, chain_depth = %s, owner_user_id = COALESCE(%s, owner_user_id),
                alert_days = %s, last_scanned_at = %s, alert_status = %s, alert_message = %s,
                simulated = false
            WHERE endpoint_id = %s
            """,
            (
                result["fingerprint_sha256"],
                result["subject"][:500],
                result["issuer"][:500],
                result["not_before"],
                result["not_after"],
                Jsonb(result["san"]),
                result["chain_depth"],
                owner,
                alert_days,
                now,
                alert_status,
                message[:500],
                ep["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO tls_certificate_records
                (id, endpoint_id, fingerprint_sha256, subject, issuer, not_before, not_after, san,
                 chain_depth, owner_user_id, alert_days, last_scanned_at, alert_status, alert_message,
                 simulated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)
            """,
            (
                uuid7(),
                ep["id"],
                result["fingerprint_sha256"],
                result["subject"][:500],
                result["issuer"][:500],
                result["not_before"],
                result["not_after"],
                Jsonb(result["san"]),
                result["chain_depth"],
                owner,
                alert_days,
                now,
                alert_status,
                message[:500],
            ),
        )
    return get_record(conn, ep["id"])


def get_record(conn: Conn, endpoint_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, e.host, e.port, e.scheme, e.connector_id, u.email AS owner_email, u.display_name AS owner_name
        FROM tls_certificate_records r
        JOIN approved_endpoints e ON e.id = r.endpoint_id
        LEFT JOIN users u ON u.id = r.owner_user_id
        WHERE r.endpoint_id = %s
        """,
        (endpoint_id,),
    ).fetchone()
    if row is None:
        raise not_found("enregistrement TLS")
    return {
        "id": str(row["id"]),
        "endpoint_id": str(row["endpoint_id"]),
        "connector_id": str(row["connector_id"]),
        "host": row["host"],
        "port": row["port"],
        "scheme": row["scheme"],
        "fingerprint_sha256": row["fingerprint_sha256"],
        "subject": row["subject"],
        "issuer": row["issuer"],
        "not_before": row["not_before"].isoformat(),
        "not_after": row["not_after"].isoformat(),
        "san": row["san"],
        "chain_depth": row["chain_depth"],
        "owner_user_id": str(row["owner_user_id"]) if row["owner_user_id"] else None,
        "owner_email": row.get("owner_email"),
        "owner_name": row.get("owner_name"),
        "alert_days": row["alert_days"],
        "alert_status": row["alert_status"],
        "alert_message": row["alert_message"],
        "last_scanned_at": row["last_scanned_at"].isoformat(),
        "private_key_collected": False,
    }


def list_records(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    if not (
        principal.is_platform_admin
        or principal.is_global_auditor
        or any(b.role in ("connector_admin", "project_admin") for b in principal.bindings)
    ):
        raise forbidden("rôle requis")
    rows = conn.execute("SELECT endpoint_id FROM tls_certificate_records ORDER BY not_after").fetchall()
    return [get_record(conn, r["endpoint_id"]) for r in rows]


def list_alerts(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    records = list_records(conn, principal)
    return [r for r in records if r["alert_status"] in ("expiring_soon", "expired", "untrusted", "unreachable")]
