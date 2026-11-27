"""F16 : runners par site ou zone — mTLS, refus de zone, heartbeat → uncertain.

Niveau de preuve : testé localement (deux runners locaux de zones différentes ;
PKI de test sous keys_dir ; pas de site distant réel).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rackloom import admission, runners
from rackloom.db import transaction
from rackloom.executions import queue
from rackloom.identity.authz import Principal, load_bindings
from rackloom.ids import uuid7
from rackloom.policy import BUILTIN_VERSION
from rackloom.worker.main import Worker


def _admin_principal(conn, demo) -> Principal:
    admin = demo.user("admin")
    return Principal(
        user_id=admin["id"],
        email=admin["email"],
        display_name=admin["display_name"],
        session_id=admin["id"],
        auth_method="test",
        auth_time=datetime.now(UTC),
        bindings=load_bindings(conn, admin["id"]),
    )


def test_f16_bundle_version():
    assert BUILTIN_VERSION == "builtin@10"


def test_f16_enroll_mtls_et_zones(conn, demo, api, tmp_path, monkeypatch):
    """Enrôlement admin : certificat client émis, zones dev/prod distinctes."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    conn.commit()

    zones = api("admin").get("/api/v1/admin/execution-zones").json()
    slugs = {z["slug"] for z in zones}
    assert {"dev", "prod"} <= slugs
    kinds = {z["slug"]: set(z["environment_kinds"]) for z in zones}
    assert "prod" not in kinds["dev"]
    assert "dev" not in kinds["prod"]

    body = (
        api("admin")
        .post(
            "/api/v1/admin/runners/enroll",
            json={
                "slug": "lab-dev",
                "name": "Runner labo dev",
                "zone": "dev",
                "capabilities": ["terraform", "ansible"],
            },
        )
        .json()
    )
    assert body["runner"]["zone"] == "dev"
    assert body["runner"]["status"] == "enrolled"
    assert "tf_apply" in body["runner"]["capabilities"]
    cert = body["client_certificate"]
    assert cert["fingerprint_sha256"] == body["runner"]["cert_fingerprint_sha256"]
    assert "BEGIN CERTIFICATE" in cert["cert_pem"]
    assert "BEGIN RSA PRIVATE KEY" in cert["key_pem"] or "BEGIN PRIVATE KEY" in cert["key_pem"]
    assert runners.fingerprint_pem(cert["cert_pem"]) == cert["fingerprint_sha256"]


def test_f16_claim_filtre_zone(conn, demo, tmp_path, monkeypatch):
    """queue.claim avec zone=dev ignore les jobs required_zone=prod."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    vit = demo.project("vitrine")["id"]
    prod = demo.env("vitrine", "prod")
    dev = demo.env("vitrine", "dev")
    alice = demo.user("alice")["id"]

    def _change(env):
        cid = uuid7()
        conn.execute(
            "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by) "
            "VALUES (%s, %s, %s, 'create', 'F16', 'approved', %s)",
            (cid, vit, env["id"], alice),
        )
        return {"id": cid, "project_id": vit, "environment_id": env["id"]}

    prod_job = queue.enqueue(
        conn,
        kind="tf_apply",
        change=_change(prod),
        idempotency_key=f"f16-p-{uuid7()}",
        payload={"t": 1},
    )
    dev_job = queue.enqueue(
        conn,
        kind="tf_apply",
        change=_change(dev),
        idempotency_key=f"f16-d-{uuid7()}",
        payload={"t": 1},
    )
    conn.commit()
    assert prod_job["required_zone"] == "prod"
    assert dev_job["required_zone"] == "dev"

    with transaction() as tx:
        claimed = queue.claim(tx, "runner-dev-1", 30, kinds=["tf_apply"], zone="dev")
    assert claimed is not None
    assert claimed["id"] == dev_job["id"]
    assert claimed["required_zone"] == "dev"

    row = conn.execute("SELECT state FROM jobs WHERE id = %s", (prod_job["id"],)).fetchone()
    assert row["state"] == "queued"


def test_f16_admission_r130_refuse_zone(conn, demo, tmp_path, monkeypatch):
    """R130 : runner enrôlé en zone prod + job dev → refus runner_zone_refused.

    On évite l'environnement `prod` ici : R010 (connecteur simulé) refuserait avant R130.
    """
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    actor = _admin_principal(conn, demo)
    result = runners.enroll(
        conn,
        actor,
        slug="r130-prod",
        name="R130 prod",
        zone_slug="prod",
        capabilities=["terraform", "inventory_refresh"],
        keys_dir=tmp_path / "keys",
    )
    runner_view = result["runner"]
    runners.heartbeat(
        conn,
        runners.resolve_by_fingerprint(conn, runner_view["cert_fingerprint_sha256"]),
        worker_id="worker-r130-prod",
    )

    vit = demo.project("vitrine")["id"]
    dev = demo.env("vitrine", "dev")
    alice = demo.user("alice")["id"]
    cid = uuid7()
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by) "
        "VALUES (%s, %s, %s, 'create', 'R130', 'approved', %s)",
        (cid, vit, dev["id"], alice),
    )
    # Job `read` : les règles write (approbation) ne masquent pas R130.
    job = queue.enqueue(
        conn,
        kind="inventory_refresh",
        change={"id": cid, "project_id": vit, "environment_id": dev["id"]},
        idempotency_key=f"f16-r130-{uuid7()}",
    )
    conn.execute(
        "UPDATE jobs SET state = 'leased', lease_owner = %s, lease_epoch = 1, required_zone = 'dev' WHERE id = %s",
        ("worker-r130-prod", job["id"]),
    )
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (cid,)).fetchone()
    env = conn.execute("SELECT * FROM environments WHERE id = %s", (dev["id"],)).fetchone()
    conn.commit()

    ok, reason, code = runners.admission_zone_verdict(conn, dict(job), dict(env))
    assert ok is False and code == "runner_zone_refused"

    with transaction() as tx:
        ctx = admission.Context(
            conn=tx,
            job=dict(job),
            change=dict(change),
            environment=dict(env),
            execution=None,
            approval=None,
            plan=None,
            dry_run=True,
        )
        decision = admission.evaluate(ctx, stage="start")
    assert decision["outcome"] == "refused"
    assert decision["refusal"]["code"] == "runner_zone_refused"


def test_f16_heartbeat_perdu_uncertain_pas_de_double_ecriture(conn, demo, tmp_path, monkeypatch):
    """Perte de heartbeat → runner stale, job write dispatched → uncertain + reconcile."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    actor = _admin_principal(conn, demo)
    result = runners.enroll(
        conn,
        actor,
        slug="hb-dev",
        name="HB dev",
        zone_slug="dev",
        capabilities=["terraform"],
        keys_dir=tmp_path / "keys",
    )
    fp = result["runner"]["cert_fingerprint_sha256"]
    runners.heartbeat(conn, runners.resolve_by_fingerprint(conn, fp), worker_id="worker-hb-dev")

    vit = demo.project("vitrine")["id"]
    env = demo.env("vitrine", "dev")
    alice = demo.user("alice")["id"]
    cid = uuid7()
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by, current_version_no) "
        "VALUES (%s, %s, %s, 'create', 'HB', 'executing', %s, 1)",
        (cid, vit, env["id"], alice),
    )
    job = queue.enqueue(
        conn,
        kind="tf_apply",
        change={"id": cid, "project_id": vit, "environment_id": env["id"]},
        idempotency_key=f"f16-hb-{uuid7()}",
    )
    conn.execute(
        "UPDATE jobs SET state = 'running', lease_owner = %s, lease_epoch = 1, "
        "dispatched_at = now(), class = 'write' WHERE id = %s",
        ("worker-hb-dev", job["id"]),
    )
    conn.execute(
        "UPDATE site_runners SET last_heartbeat_at = %s, status = 'active' WHERE cert_fingerprint_sha256 = %s",
        (datetime.now(UTC) - timedelta(seconds=120), fp),
    )
    conn.commit()

    stats = runners.mark_stale_and_uncertain(conn, stale_after=timedelta(seconds=90))
    conn.commit()
    assert stats["stale_runners"] == 1
    assert stats["uncertain_jobs"] == 1

    job_row = conn.execute("SELECT state FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    assert job_row["state"] == "uncertain"
    runner_row = conn.execute("SELECT status FROM site_runners WHERE cert_fingerprint_sha256 = %s", (fp,)).fetchone()
    assert runner_row["status"] == "stale"
    change = conn.execute("SELECT state FROM changes WHERE id = %s", (cid,)).fetchone()
    assert change["state"] == "uncertain"
    reconcile = conn.execute(
        "SELECT kind, state FROM jobs WHERE kind = 'reconcile' AND payload->>'job_id' = %s",
        (str(job["id"]),),
    ).fetchone()
    assert reconcile is not None
    assert reconcile["state"] == "queued"
    second = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind = 'tf_apply' AND change_id = %s AND state = 'queued'",
        (cid,),
    ).fetchone()
    assert second["n"] == 0


def test_f16_runner_api_claim_mtls(conn, demo, api, tmp_path, monkeypatch):
    """Le runner initie claim via API mTLS ; un job prod n'est pas servi à un runner dev."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    conn.commit()

    enrolled = (
        api("admin")
        .post(
            "/api/v1/admin/runners/enroll",
            json={
                "slug": "api-dev",
                "name": "API dev",
                "zone": "dev",
                "capabilities": ["terraform", "ansible", "reconcile"],
            },
        )
        .json()
    )
    fp = enrolled["client_certificate"]["fingerprint_sha256"]

    vit = demo.project("vitrine")["id"]
    prod = demo.env("vitrine", "prod")
    dev = demo.env("vitrine", "dev")
    alice = demo.user("alice")["id"]

    def _enq(env, key):
        cid = uuid7()
        conn.execute(
            "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by) "
            "VALUES (%s, %s, %s, 'create', 'API', 'approved', %s)",
            (cid, vit, env["id"], alice),
        )
        return queue.enqueue(
            conn,
            kind="tf_apply",
            change={"id": cid, "project_id": vit, "environment_id": env["id"]},
            idempotency_key=key,
        )

    prod_job = _enq(prod, f"f16-api-p-{uuid7()}")
    dev_job = _enq(dev, f"f16-api-d-{uuid7()}")
    conn.commit()

    client = api("admin")
    hb = client.post(
        "/api/v1/runners/me/heartbeat",
        json={},
        headers={runners.CERT_HEADER: fp},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["status"] == "active"

    claimed = client.post(
        "/api/v1/runners/me/claim",
        json={"lease_seconds": 30, "kinds": ["tf_apply"]},
        headers={runners.CERT_HEADER: fp},
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["job"] is not None
    assert body["job"]["id"] == str(dev_job["id"])
    assert body["job"]["required_zone"] == "dev"

    leftover = conn.execute("SELECT state FROM jobs WHERE id = %s", (prod_job["id"],)).fetchone()
    assert leftover["state"] == "queued"


def test_f16_reap_appelle_stale(conn, demo, tmp_path, monkeypatch):
    """La faucheuse du worker déclenche mark_stale_and_uncertain."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("RACKLOOM_RUNNER_STALE_SECONDS", "1")
    from rackloom.config import get_settings

    get_settings.cache_clear()
    runners.ensure_zones(conn)
    actor = _admin_principal(conn, demo)
    result = runners.enroll(
        conn,
        actor,
        slug="reap-dev",
        name="Reap",
        zone_slug="dev",
        capabilities=["terraform"],
        keys_dir=tmp_path / "keys",
    )
    fp = result["runner"]["cert_fingerprint_sha256"]
    runners.heartbeat(conn, runners.resolve_by_fingerprint(conn, fp), worker_id="w-reap")
    conn.execute(
        "UPDATE site_runners SET last_heartbeat_at = %s WHERE cert_fingerprint_sha256 = %s",
        (datetime.now(UTC) - timedelta(seconds=30), fp),
    )
    conn.commit()

    class _Noop:
        def images(self) -> dict:
            return {}

    # Pas de register() : reap() suffit pour mark_stale_and_uncertain.
    w = Worker(worker_id="faucheur-f16", launcher=_Noop())  # type: ignore[arg-type]
    w.reap()
    row = conn.execute("SELECT status FROM site_runners WHERE cert_fingerprint_sha256 = %s", (fp,)).fetchone()
    assert row["status"] == "stale"
