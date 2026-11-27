"""F16 : runners par site ou zone — enrôlement mTLS, capacités, heartbeat.

Le runner initie la connexion (pull). Le serveur filtre les jobs par zone et capacités.
Un runner de zone `dev` ne peut pas prendre un job d'environnement `prod`. Une perte de
heartbeat marque le runner `stale` et les écritures déjà envoyées passent en `uncertain`
avec une réconciliation en lecture seule — jamais une seconde écriture non vérifiée.

L'identité mTLS est l'empreinte SHA-256 du certificat client. En production, le proxy
(Caddy) termine le mTLS et injecte `X-Client-Cert-SHA256`. Les tests et la démo émettent
des certificats via la CA locale (jamais commitée ; clés sous `.lab/` ou `keys_dir`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .. import audit, events
from ..db import Conn
from ..errors import ApiError
from ..identity.authz import Principal
from ..ids import uuid7

log = logging.getLogger("rackloom.runners")

# Empreinte transmise par le proxy après vérification mTLS (ou en test).
CERT_HEADER = "x-client-cert-sha256"

CAPABILITIES = frozenset(
    {
        "inventory_refresh",
        "preflight",
        "tf_plan",
        "tf_apply",
        "tf_state_read",
        "guest_ready",
        "ansible_configure",
        "verify",
        "reconcile",
        "drift_scan",
        "vm_restore",
        "service_restart",
        "lease_retire",
        "tf_import",
        "host_patch",
        "patch_check",
        # Alias de famille pour l'enrôlement (étendus aux kinds concrets).
        "terraform",
        "ansible",
        "probe",
    }
)

FAMILY_EXPAND: dict[str, tuple[str, ...]] = {
    "terraform": ("preflight", "tf_plan", "tf_apply", "tf_state_read", "tf_import"),
    "ansible": ("guest_ready", "ansible_configure", "verify"),
    "probe": ("verify", "patch_check"),
}

# Au-delà de ce délai sans heartbeat, le runner est marqué stale.
STALE_AFTER = timedelta(seconds=90)

DEFAULT_ZONES: tuple[dict[str, Any], ...] = (
    {
        "slug": "dev",
        "name": "Zone de développement",
        "environment_kinds": ["dev", "test"],
        "id": UUID("01940000-f16a-7000-8000-000000000001"),
    },
    {
        "slug": "prod",
        "name": "Zone de production",
        "environment_kinds": ["preprod", "prod"],
        "id": UUID("01940000-f16a-7000-8000-000000000002"),
    },
)


def ensure_zones(conn: Conn) -> list[dict[str, Any]]:
    """Garantit les zones builtin (idempotent si la migration les a déjà créées)."""
    out = []
    for z in DEFAULT_ZONES:
        row = conn.execute("SELECT * FROM execution_zones WHERE slug = %s", (z["slug"],)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO execution_zones (id, slug, name, environment_kinds, is_builtin) "
                "VALUES (%s, %s, %s, %s, true) RETURNING *",
                (z["id"], z["slug"], z["name"], z["environment_kinds"]),
            ).fetchone()
            assert row is not None
        out.append(row)
    return out


def zone_by_slug(conn: Conn, slug: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM execution_zones WHERE slug = %s", (slug,)).fetchone()


def zone_for_environment_kind(conn: Conn, kind: str) -> dict[str, Any] | None:
    """Zone unique dont `environment_kinds` contient ce kind (déterministe par slug)."""
    return conn.execute(
        "SELECT * FROM execution_zones WHERE %s = ANY(environment_kinds) ORDER BY slug LIMIT 1",
        (kind,),
    ).fetchone()


def required_zone_for_change(conn: Conn, change: dict[str, Any] | None) -> str | None:
    """Slug de zone requis pour un changement, ou None pour un job sans environnement."""
    if change is None or not change.get("environment_id"):
        return None
    env = conn.execute("SELECT kind FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
    if env is None:
        return None
    zone = zone_for_environment_kind(conn, env["kind"])
    return zone["slug"] if zone else None


def expand_capabilities(declared: list[str]) -> list[str]:
    out: set[str] = set()
    for cap in declared:
        if cap not in CAPABILITIES:
            raise ApiError(422, "invalid_capability", f"capacité « {cap} » inconnue", field="/capabilities")
        if cap in FAMILY_EXPAND:
            out.update(FAMILY_EXPAND[cap])
        else:
            out.add(cap)
    return sorted(out)


def fingerprint_pem(pem: bytes | str) -> str:
    data = pem.encode() if isinstance(pem, str) else pem
    cert = x509.load_pem_x509_certificate(data)
    return cert.fingerprint(hashes.SHA256()).hex()


def _ca_paths(keys_dir: Path) -> tuple[Path, Path]:
    return keys_dir / "runner-ca.key", keys_dir / "runner-ca.pem"


def ensure_runner_ca(keys_dir: Path) -> tuple[Path, Path]:
    """CA locale pour certificats clients de runners (créée une fois, 0600)."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = _ca_paths(keys_dir)
    if key_path.exists() and cert_path.exists():
        return key_path, cert_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Rackloom"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Rackloom Runner CA"),
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
    """Émet un certificat client signé par la CA runners. La clé privée n'est jamais stockée."""
    key_path, _ = ensure_runner_ca(keys_dir)
    ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(ca_key, rsa.RSAPrivateKey)
    ca_cert = x509.load_pem_x509_certificate((_ca_paths(keys_dir)[1]).read_bytes())
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
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


def enroll(
    conn: Conn,
    actor: Principal,
    *,
    slug: str,
    name: str,
    zone_slug: str,
    capabilities: list[str],
    keys_dir: Path,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Enrôle un runner : enregistrement + certificat client (clé affichée une seule fois)."""
    if not actor.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    ensure_zones(conn)
    zone = zone_by_slug(conn, zone_slug)
    if zone is None:
        raise ApiError(422, "unknown_zone", f"zone « {zone_slug} » inconnue", field="/zone")
    caps = expand_capabilities(capabilities)
    issued = issue_client_cert(keys_dir, common_name=f"runner:{slug}")
    runner_id = uuid7()
    row = conn.execute(
        """
        INSERT INTO site_runners (
            id, slug, name, zone_id, capabilities, cert_fingerprint_sha256,
            cert_subject, cert_not_before, cert_not_after, status, enrolled_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'enrolled', %s)
        RETURNING *
        """,
        (
            runner_id,
            slug,
            name,
            zone["id"],
            caps,
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
        action="runner.enrolled",
        outcome="succeeded",
        project_id=None,
        correlation_id=None,
        request_id=request_id,
        targets={"runner_id": str(runner_id), "slug": slug, "zone": zone_slug, "capabilities": caps},
    )
    return {
        "runner": view(conn, row),
        # Clé privée et PEM : une seule fois, jamais journalisés.
        "client_certificate": {
            "cert_pem": issued["cert_pem"],
            "key_pem": issued["key_pem"],
            "fingerprint_sha256": issued["fingerprint_sha256"],
            "not_before": issued["not_before"].isoformat(),
            "not_after": issued["not_after"].isoformat(),
        },
        "ca_pem": (_ca_paths(keys_dir)[1]).read_text(encoding="utf-8"),
    }


def view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    zone = conn.execute(
        "SELECT slug, name, environment_kinds FROM execution_zones WHERE id = %s", (row["zone_id"],)
    ).fetchone()
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "zone": zone["slug"] if zone else None,
        "zone_name": zone["name"] if zone else None,
        "environment_kinds": list(zone["environment_kinds"]) if zone else [],
        "capabilities": list(row["capabilities"]),
        "cert_fingerprint_sha256": row["cert_fingerprint_sha256"],
        "cert_subject": row["cert_subject"],
        "cert_not_before": row["cert_not_before"].isoformat() if row["cert_not_before"] else None,
        "cert_not_after": row["cert_not_after"].isoformat() if row["cert_not_after"] else None,
        "status": row["status"],
        "last_heartbeat_at": row["last_heartbeat_at"].isoformat() if row["last_heartbeat_at"] else None,
        "enrolled_at": row["enrolled_at"].isoformat() if row.get("enrolled_at") else None,
        "worker_id": row.get("worker_id"),
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
    }


def list_runners(conn: Conn, actor: Principal) -> list[dict[str, Any]]:
    if not (actor.is_platform_admin or actor.is_global_auditor):
        raise ApiError(403, "forbidden", "rôle administrateur ou auditeur requis")
    rows = conn.execute("SELECT * FROM site_runners ORDER BY enrolled_at DESC").fetchall()
    return [view(conn, r) for r in rows]


def list_zones(conn: Conn, actor: Principal) -> list[dict[str, Any]]:
    if not (actor.is_platform_admin or actor.is_global_auditor):
        raise ApiError(403, "forbidden", "rôle administrateur ou auditeur requis")
    ensure_zones(conn)
    rows = conn.execute("SELECT * FROM execution_zones ORDER BY slug").fetchall()
    return [
        {
            "id": str(r["id"]),
            "slug": r["slug"],
            "name": r["name"],
            "environment_kinds": list(r["environment_kinds"]),
            "is_builtin": r["is_builtin"],
        }
        for r in rows
    ]


def revoke(
    conn: Conn,
    actor: Principal,
    runner_id: UUID,
    *,
    reason: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not actor.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    row = conn.execute(
        "UPDATE site_runners SET status = 'revoked', revoked_at = now(), revoked_by = %s, "
        "revoke_reason = %s WHERE id = %s AND status <> 'revoked' RETURNING *",
        (actor.user_id, reason, runner_id),
    ).fetchone()
    if row is None:
        existing = conn.execute("SELECT * FROM site_runners WHERE id = %s", (runner_id,)).fetchone()
        if existing is None:
            raise ApiError(404, "not_found", "runner introuvable")
        return view(conn, existing)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(actor.user_id),
        action="runner.revoked",
        outcome="succeeded",
        project_id=None,
        correlation_id=None,
        request_id=request_id,
        targets={"runner_id": str(runner_id), "slug": row["slug"], "reason": reason},
    )
    return view(conn, row)


def resolve_by_fingerprint(conn: Conn, fingerprint: str) -> dict[str, Any] | None:
    fp = fingerprint.strip().lower().replace(":", "")
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        return None
    return conn.execute(
        "SELECT r.*, z.slug AS zone_slug, z.environment_kinds AS zone_kinds "
        "FROM site_runners r JOIN execution_zones z ON z.id = r.zone_id "
        "WHERE r.cert_fingerprint_sha256 = %s",
        (fp,),
    ).fetchone()


def authenticate_runner(conn: Conn, fingerprint: str | None) -> dict[str, Any]:
    """Authentifie un runner par empreinte de certificat client (mTLS)."""
    if not fingerprint:
        raise ApiError(401, "unauthenticated", "certificat client mTLS absent")
    row = resolve_by_fingerprint(conn, fingerprint)
    if row is None:
        raise ApiError(401, "unauthenticated", "runner non enrôlé ou empreinte inconnue")
    if row["status"] == "revoked":
        raise ApiError(403, "runner_revoked", "runner révoqué")
    now = datetime.now(UTC)
    if row["cert_not_after"] < now:
        raise ApiError(403, "runner_cert_expired", "certificat client expiré")
    if row["cert_not_before"] > now:
        raise ApiError(403, "runner_cert_not_yet_valid", "certificat client pas encore valide")
    return row


def heartbeat(
    conn: Conn,
    runner: dict[str, Any],
    *,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Le runner initie le heartbeat. Passe enrolled → active."""
    wid = worker_id or runner.get("worker_id") or f"runner-{runner['slug']}"
    row = conn.execute(
        """
        UPDATE site_runners
        SET last_heartbeat_at = now(),
            status = CASE WHEN status IN ('enrolled', 'stale', 'active') THEN 'active' ELSE status END,
            worker_id = COALESCE(%s, worker_id)
        WHERE id = %s AND status <> 'revoked'
        RETURNING *
        """,
        (wid, runner["id"]),
    ).fetchone()
    if row is None:
        raise ApiError(403, "runner_revoked", "runner révoqué")
    # Assure une ligne workers pour la file (bail / faucheuse).
    conn.execute(
        "INSERT INTO workers (id, hostname, slots) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()",
        (wid, f"site-runner:{row['slug']}", 1),
    )
    return view(conn, {**row, "zone_id": row["zone_id"]})


def zone_allows_environment(zone_kinds: list[str] | Any, env_kind: str | None) -> bool:
    if env_kind is None:
        return True
    return env_kind in list(zone_kinds)


def runner_may_take_job(runner: dict[str, Any], job: dict[str, Any], env_kind: str | None) -> tuple[bool, str]:
    """Contrôle zone + capacités. Motif lisible en cas de refus."""
    if runner.get("status") == "revoked":
        return False, "runner_revoked"
    zone_kinds = runner.get("zone_kinds") or runner.get("environment_kinds") or []
    if env_kind is not None and not zone_allows_environment(zone_kinds, env_kind):
        return False, "runner_zone_refused"
    required = job.get("required_zone")
    zone_slug = runner.get("zone_slug") or runner.get("zone")
    if required and zone_slug and required != zone_slug:
        return False, "runner_zone_refused"
    caps = set(runner.get("capabilities") or [])
    if job["kind"] not in caps:
        return False, "runner_capability_missing"
    return True, ""


def mark_stale_and_uncertain(conn: Conn, *, stale_after: timedelta = STALE_AFTER) -> dict[str, int]:
    """Runners sans heartbeat → stale ; écritures déjà envoyées → uncertain + reconcile.

    Aucune remise en file d'une écriture déjà dispatchée (pas de double écriture).
    """
    from ..changes.transitions import transition
    from ..connectors import circuit as connector_circuit
    from ..executions import queue

    cutoff = datetime.now(UTC) - stale_after
    stale_runners = conn.execute(
        """
        UPDATE site_runners
        SET status = 'stale'
        WHERE status = 'active'
          AND last_heartbeat_at IS NOT NULL
          AND last_heartbeat_at < %s
        RETURNING id, slug, worker_id
        """,
        (cutoff,),
    ).fetchall()
    uncertain_jobs = 0
    for runner in stale_runners:
        events.emit(
            conn,
            type="runner.stale",
            source="system",
            project_id=None,
            correlation_id=None,
            job_id=None,
            severity="warning",
            data={
                "runner_id": str(runner["id"]),
                "slug": runner["slug"],
                "message": "Heartbeat perdu : runner marqué stale, aucune écriture relancée",
            },
            dedupe_key=f"runner.stale:{runner['id']}:{cutoff.isoformat()}",
        )
        if not runner["worker_id"]:
            continue
        jobs = conn.execute(
            """
            SELECT * FROM jobs
            WHERE lease_owner = %s
              AND state IN ('leased', 'running')
              AND class = 'write'
              AND dispatched_at IS NOT NULL
            FOR UPDATE SKIP LOCKED
            """,
            (runner["worker_id"],),
        ).fetchall()
        for job in jobs:
            conn.execute(
                "UPDATE jobs SET state = 'uncertain', lease_owner = NULL, updated_at = now() "
                "WHERE id = %s AND state IN ('leased', 'running')",
                (job["id"],),
            )
            uncertain_jobs += 1
            events.emit(
                conn,
                type="job.uncertain",
                source="system",
                project_id=job["project_id"],
                correlation_id=job["change_id"],
                job_id=job["id"],
                severity="warning",
                data={
                    "kind": job["kind"],
                    "message": "Heartbeat runner perdu après envoi : résultat incertain, "
                    "réconciliation lancée (pas de seconde écriture)",
                    "runner_id": str(runner["id"]),
                },
            )
            change = None
            if job["change_id"]:
                change = conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (job["change_id"],)).fetchone()
                if change and change["state"] in ("executing", "verifying"):
                    transition(
                        conn,
                        change,
                        "uncertain",
                        reason_code="runner_heartbeat_lost",
                        actor_type="system",
                        actor_id=None,
                        severity="warning",
                        detail={
                            "message": "Heartbeat du runner de zone perdu après envoi",
                            "job_id": str(job["id"]),
                            "runner_id": str(runner["id"]),
                        },
                    )
            for kind in connector_circuit.required_kinds(job["kind"]):
                connector_circuit.record_failure_for_kind(
                    conn,
                    kind,
                    f"runner stale après dispatch ({runner['slug']})",
                    actor_type="system",
                    actor_id=None,
                )
            queue.enqueue(
                conn,
                kind="reconcile",
                change=change,
                project_id=job["project_id"],
                stack_id=None,
                idempotency_key=f"reconcile:runner-stale:{job['id']}",
                payload={"job_id": str(job["id"]), "reason": "runner_heartbeat_lost"},
                priority=10,
            )
    return {"stale_runners": len(stale_runners), "uncertain_jobs": uncertain_jobs}


def admission_zone_verdict(
    conn: Conn,
    job: dict[str, Any],
    environment: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """R130 : la zone du runner (lease_owner) est-elle autorisée pour l'environnement ?"""
    if environment is None:
        return True, "", ""
    owner = job.get("lease_owner")
    if not owner:
        return True, "", ""
    runner = conn.execute(
        "SELECT r.*, z.slug AS zone_slug, z.environment_kinds AS zone_kinds "
        "FROM site_runners r JOIN execution_zones z ON z.id = r.zone_id "
        "WHERE r.worker_id = %s AND r.status <> 'revoked'",
        (owner,),
    ).fetchone()
    if runner is None:
        # Worker local historique sans enrôlement F16 : admis (compat lot 1).
        return True, "", ""
    ok, code = runner_may_take_job(runner, job, environment.get("kind"))
    if ok:
        return True, "", ""
    if code == "runner_zone_refused":
        return (
            False,
            f"le runner de zone « {runner['zone_slug']} » ne peut pas exécuter "
            f"un job d'environnement « {environment['kind']} »",
            "runner_zone_refused",
        )
    if code == "runner_capability_missing":
        return (
            False,
            f"le runner « {runner['slug']} » n'a pas la capacité « {job['kind']} »",
            "runner_capability_missing",
        )
    return False, "runner refusé", code
