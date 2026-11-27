"""F41–F45 : SCIM, revue d'accès, rotation, copie d'audit, certificats TLS.

Niveau de preuve : testé localement. IdP / stockage / vCenter réels : non (D2).
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import flows
from rackloom import audit, clock, secrets, security
from rackloom.audit import export as audit_export
from rackloom.changes import service
from rackloom.connectors import rotation, tls_registry
from rackloom.db import Jsonb
from rackloom.identity import access_review, scim
from rackloom.identity import service as identity
from rackloom.identity.authz import Principal, load_bindings
from rackloom.ids import uuid7
from rackloom.security import hash_password

API = "/api/v1"


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from rackloom.config import get_settings

    keys_dir = tmp_path / "keys"
    security.generate_keys(keys_dir)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(keys_dir))
    monkeypatch.setenv("RACKLOOM_DATA_DIR", str(data))
    get_settings.cache_clear()
    return keys_dir


def _principal_for(conn, user_row: dict, *, step_up: bool = False) -> Principal:
    return Principal(
        user_id=user_row["id"],
        email=user_row["email"],
        display_name=user_row["display_name"],
        session_id=uuid7(),
        auth_method="step_up" if step_up else "password",
        auth_time=datetime.now(UTC),
        bindings=load_bindings(conn, user_row["id"]),
        acr="urn:rackloom:step-up" if step_up else "pwd",
        amr=["pwd", "otp"] if step_up else ["pwd"],
    )


# --- F41 SCIM ---------------------------------------------------------------------------------


def test_f41_scim_deactivate_revokes_session_and_blocks_queued(conn, demo, api, keys):
    admin = api("admin")
    minted = admin.post(f"{API}/admin/scim/tokens", json={"label": "annuaire-test"})
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]

    bare = api(None)
    created = bare.post(
        f"{API}/scim/v2/Users",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"},
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "lea@demo.invalid",
            "displayName": "Léa SCIM",
            "externalId": "ext-lea-1",
            "active": True,
        },
    )
    assert created.status_code == 201, created.text
    user_id = UUID(created.json()["id"])
    conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (hash_password("mot-de-passe-de-test-xxxx"), user_id),
    )
    project_id = demo.project("vitrine")["id"]
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, 'operator', %s)",
        (uuid7(), user_id, project_id),
    )
    conn.commit()

    lea = api(None)
    login = lea.post(
        f"{API}/auth/login",
        json={"email": "lea@demo.invalid", "password": "mot-de-passe-de-test-xxxx"},
    )
    assert login.status_code == 200, login.text
    lea.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    session = lea.get(f"{API}/session")
    assert session.status_code == 200, session.text

    user_row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    lea_p = _principal_for(conn, user_row)
    change = service.create_change(conn, lea_p, demo.project("vitrine"), "SCIM départ")
    res = service.add_version(conn, lea_p, change["id"], {"need": flows.need(), "base_version_no": 0})
    assert res["revision"]["outcome"] == "resolved", res["revision"]
    sub = service.submit(conn, lea_p, change["id"], res["revision_no"])
    conn.commit()
    job_id = sub["job_id"]
    assert conn.execute("SELECT state FROM jobs WHERE id = %s", (job_id,)).fetchone()["state"] == "queued"

    # Job déjà envoyé : ne doit pas être bloqué par la désactivation.
    dispatched_id = uuid7()
    src = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO jobs (id, kind, class, state, change_id, project_id, execution_id, stack_id,
                          idempotency_key, attempts, dispatched_at, payload)
        VALUES (%s, 'tf_apply', 'write', 'running', %s, %s, %s, %s, %s, 1, now(), '{}'::jsonb)
        """,
        (
            dispatched_id,
            src["change_id"],
            src["project_id"],
            src["execution_id"],
            src["stack_id"],
            f"disp-{dispatched_id}",
        ),
    )
    conn.commit()

    patch = bare.patch(
        f"{API}/scim/v2/Users/{user_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"},
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "path": "active", "value": False}],
        },
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["active"] is False

    assert lea.get(f"{API}/session").status_code == 401

    queued = conn.execute("SELECT state, blocked_reason FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert queued["state"] == "blocked"
    assert queued["blocked_reason"] == "identity_disabled"

    still = conn.execute("SELECT state FROM jobs WHERE id = %s", (dispatched_id,)).fetchone()
    assert still["state"] == "running"


def test_f41_scim_fixture_sync(conn, demo, keys):
    result = scim.apply_fixture(
        conn,
        [
            {"externalId": "ext-a", "userName": "a-scim@demo.invalid", "displayName": "A", "active": True},
            {"externalId": "ext-b", "userName": "b-scim@demo.invalid", "displayName": "B", "active": False},
        ],
    )
    assert result["created"] == 2
    assert conn.execute("SELECT status FROM users WHERE external_id = 'ext-b'").fetchone()["status"] == "disabled"
    conn.commit()


# --- F42 revue --------------------------------------------------------------------------------


def test_f42_access_review_scope_and_audit(conn, demo, keys):
    admin = demo.principal("admin", step_up=True)
    conn.execute("UPDATE projects SET referent_user_id = NULL WHERE slug = 'compta'")
    past = clock.now() - timedelta(days=1)
    conn.execute(
        """
        INSERT INTO policy_exceptions
            (id, rule_id, project_id, environment_id, owner_user_id, reason, expires_at, created_by)
        VALUES (%s, 'ORG001', %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid7(),
            demo.project("vitrine")["id"],
            demo.env("vitrine", "dev")["id"],
            demo.user("alice")["id"],
            "ancienne dérogation",
            past,
            demo.user("admin")["id"],
        ),
    )
    ghost = identity.create_user(conn, "ghost@demo.invalid", "Ghost", "mot-de-passe-de-test-xxxx")
    conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (ghost,))
    binding_id = uuid7()
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, 'operator', %s)",
        (binding_id, ghost, demo.project("vitrine")["id"]),
    )
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, 'project_admin', %s)",
        (uuid7(), demo.user("alice")["id"], demo.project("vitrine")["id"]),
    )
    conn.commit()

    campaign = access_review.prepare(conn, admin, title="Revue trimestrielle labo")
    assert campaign["item_count"] >= 2
    campaign_id = UUID(campaign["id"])
    conn.commit()

    alice = demo.principal("alice", step_up=True)
    viewed = access_review.get_campaign(conn, alice, campaign_id)
    for item in viewed["items"]:
        assert item["project_slug"] == "vitrine"

    admin_view = access_review.get_campaign(conn, admin, campaign_id)
    compta_items = [i for i in admin_view["items"] if i["project_slug"] == "compta"]
    assert compta_items
    with pytest.raises(Exception) as exc:
        access_review.get_item(conn, alice, UUID(compta_items[0]["id"]))
    assert getattr(exc.value, "status", None) == 404

    unjustified = next(i for i in viewed["items"] if i["kind"] == "unjustified_binding")
    decided = access_review.decide(conn, alice, UUID(unjustified["id"]), decision="revoked", note="compte parti")
    assert decided["status"] == "revoked"
    assert (
        conn.execute("SELECT revoked_at FROM role_bindings WHERE id = %s", (binding_id,)).fetchone()["revoked_at"]
        is not None
    )
    proof = conn.execute(
        "SELECT action, outcome FROM audit_log WHERE action = 'access_review.revoked' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert proof["outcome"] == "ok"
    conn.commit()


# --- F43 rotation -----------------------------------------------------------------------------


def test_f43_rotation_invalid_refused_valid_cutover(conn, demo, keys, api):
    connector = conn.execute("SELECT * FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    secrets.put(conn, "secret://connectors/vsphere-fake/password", b"ancien-secret-ok", demo.user("admin")["id"])
    conn.execute(
        "UPDATE connectors SET secret_refs = %s WHERE id = %s",
        (Jsonb({"password": "secret://connectors/vsphere-fake/password"}), connector["id"]),
    )
    conn.commit()

    admin = api("admin")
    canary = "RLCANARY-deadbeefdeadbeef"
    bad = admin.post(
        f"{API}/connectors/{connector['id']}/rotations",
        json={"secret_name": "password", "value": f"invalid-{canary}"},
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "credential_invalid"
    assert canary not in bad.text
    body = json.dumps(bad.json())
    assert "invalid-" not in body
    assert canary not in body

    refs = conn.execute("SELECT secret_refs FROM connectors WHERE id = %s", (connector["id"],)).fetchone()
    assert refs["secret_refs"]["password"] == "secret://connectors/vsphere-fake/password"

    good = admin.post(
        f"{API}/connectors/{connector['id']}/rotations",
        json={"secret_name": "password", "value": "nouveau-secret-valide"},
    )
    assert good.status_code == 200, good.text
    rot_id = good.json()["id"]
    assert "value" not in good.json()
    assert good.json()["candidate_status"] == "défini"

    validated = admin.post(f"{API}/connector-rotations/{rot_id}/validate")
    assert validated.status_code == 200, validated.text

    cut = admin.post(f"{API}/connector-rotations/{rot_id}/cutover")
    assert cut.status_code == 200, cut.text
    new_refs = conn.execute("SELECT secret_refs FROM connectors WHERE id = %s", (connector["id"],)).fetchone()
    assert new_refs["secret_refs"]["password"].startswith("secret://connectors/vsphere-fake/password/pending/")

    retired = admin.post(f"{API}/connector-rotations/{rot_id}/retire")
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "retired"


def test_f43_retire_blocked_while_job_running(conn, demo, keys):
    connector = conn.execute("SELECT * FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    secrets.put(conn, "secret://connectors/vsphere-fake/password", b"ancien", demo.user("admin")["id"])
    conn.execute(
        "UPDATE connectors SET secret_refs = %s WHERE id = %s",
        (Jsonb({"password": "secret://connectors/vsphere-fake/password"}), connector["id"]),
    )
    admin = demo.principal("admin", step_up=True)
    rot = rotation.propose(conn, admin, connector["id"], secret_name="password", value="nouveau-ok")
    rot_id = UUID(rot["id"])
    rotation.validate(conn, admin, rot_id)
    rotation.cutover(conn, admin, rot_id)
    conn.execute(
        """
        INSERT INTO jobs (id, kind, class, state, idempotency_key, attempts, dispatched_at, payload)
        VALUES (%s, 'tf_apply', 'write', 'running', %s, 1, now(), '{}'::jsonb)
        """,
        (uuid7(), f"run-{uuid7()}"),
    )
    with pytest.raises(Exception) as exc:
        rotation.retire(conn, admin, rot_id)
    assert getattr(exc.value, "code", None) == "jobs_in_flight"
    conn.commit()


# --- F44 audit export -------------------------------------------------------------------------


def test_f44_audit_export_resume_no_dup_and_lock(conn, demo, keys, tmp_path, monkeypatch):
    from rackloom.config import get_settings

    monkeypatch.setenv("RACKLOOM_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    admin = demo.principal("admin", step_up=True)
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(admin.user_id),
        action="test.f44",
        outcome="ok",
        targets={"note": "export"},
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(admin.user_id),
        action="test.f44.canary",
        outcome="ok",
        targets={"password": "RLCANARY-cafecafecafe"},
    )
    conn.commit()

    dest = audit_export.create_destination(conn, admin, slug="lab-worm", relative_path="lab-worm", object_lock=True)
    dest_id = UUID(dest["id"])
    conn.commit()
    first = audit_export.run_export(conn, admin, dest_id)
    assert first["delivered"] >= 1
    conn.commit()
    ids_after_first = {
        r["audit_id"]
        for r in conn.execute(
            "SELECT audit_id FROM audit_export_deliveries WHERE destination_id = %s AND status = 'delivered'",
            (dest_id,),
        ).fetchall()
    }

    # Reprise : les mêmes lignes ne sont pas redéposées (dedupe_key).
    second = audit_export.run_export(conn, admin, dest_id)
    conn.commit()
    ids_after_second = {
        r["audit_id"]
        for r in conn.execute(
            "SELECT audit_id FROM audit_export_deliveries WHERE destination_id = %s AND status = 'delivered'",
            (dest_id,),
        ).fetchall()
    }
    # run_export ajoute un événement d'audit : au plus une nouvelle livraison, jamais de doublon.
    assert ids_after_first <= ids_after_second
    assert second["already_present"] >= 0
    dupes = conn.execute(
        "SELECT dedupe_key, count(*) AS n FROM audit_export_deliveries WHERE destination_id = %s "
        "GROUP BY dedupe_key HAVING count(*) > 1",
        (dest_id,),
    ).fetchall()
    assert dupes == []

    delivered = conn.execute(
        "SELECT audit_id, object_key FROM audit_export_deliveries "
        "WHERE destination_id = %s AND status = 'delivered' ORDER BY audit_id DESC LIMIT 5",
        (dest_id,),
    ).fetchall()
    assert delivered
    root = get_settings().data_dir / "audit-export" / "lab-worm"
    for d in delivered:
        content = (root / d["object_key"]).read_text(encoding="utf-8")
        assert "RLCANARY" not in content
        assert "cafecafe" not in content

    with pytest.raises(Exception) as exc:
        audit_export.try_overwrite(conn, admin, dest_id, delivered[0]["audit_id"])
    assert getattr(exc.value, "code", None) == "object_locked"
    conn.commit()


# --- F45 TLS registry -------------------------------------------------------------------------


def _ca_and_leaf(host: str, days: int = 5):
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lab-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return ca_key, ca_cert, leaf_key, leaf_cert


@contextmanager
def _tls_server(chain: Path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain)
    srv = socket.create_server(("127.0.0.1", 0))
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                client, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with client:
                try:
                    with ctx.wrap_socket(client, server_side=True):
                        pass
                except (ssl.SSLError, OSError):
                    pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        srv.close()


def test_f45_tls_expiring_alert_owner(conn, demo, keys, tmp_path):
    _ca_key, ca_cert, leaf_key, leaf_cert = _ca_and_leaf("localhost", days=5)
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    chain = tmp_path / "leaf.pem"
    chain.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        + leaf_cert.public_bytes(serialization.Encoding.PEM)
    )

    connector = conn.execute("SELECT id FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    with _tls_server(chain) as port:
        ep_id = uuid7()
        conn.execute(
            "INSERT INTO approved_endpoints (id, connector_id, scheme, host, port, ca_bundle_pem) "
            "VALUES (%s, %s, 'https', 'localhost', %s, %s)",
            (ep_id, connector["id"], port, ca_pem),
        )
        conn.commit()
        admin = demo.principal("admin", step_up=True)
        owner = demo.user("bruno")["id"]
        record = tls_registry.scan_endpoint(conn, admin, ep_id, owner_user_id=owner, alert_days=30)
        assert record["alert_status"] == "expiring_soon"
        assert record["owner_user_id"] == str(owner)
        assert record["private_key_collected"] is False
        assert "BEGIN PRIVATE KEY" not in json.dumps(record)
        alerts = tls_registry.list_alerts(conn, admin)
        assert any(a["endpoint_id"] == str(ep_id) for a in alerts)
        conn.commit()

        bad = tls_registry._scan_endpoint("localhost", port, None)
        assert bad["status"] == "untrusted"
