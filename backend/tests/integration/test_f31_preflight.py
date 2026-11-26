"""F31 : préflight de bout en bout avant `tf_plan`.

Recette : job `preflight` (classe `read`) ; TLS à CA inconnue → `tls_untrusted` ;
state injoignable → `state_unreachable` ; canari absent ; state inchangé.
Niveau de preuve : testé localement (serveurs TLS jetables, pas de vCenter réel).
"""

from __future__ import annotations

import secrets
from pathlib import Path

import flows
from rackloom import admission
from rackloom import secrets as secrets_mod
from rackloom.connectors import capabilities_for, vsphere
from rackloom.db import Jsonb
from rackloom.executions import preflight
from rackloom.ids import uuid7
from rackloom.projects import freezes
from security.test_connector_tls import _ca, _leaf, tls_server


def _add_real_vsphere(conn, *, url: str, ca_pem: str | None, slug: str = "vsphere-f31") -> str:
    cid = uuid7()
    conn.execute(
        "INSERT INTO connectors (id, slug, kind, mode, capabilities, config) "
        "VALUES (%s, %s, 'vsphere', 'real', %s, %s)",
        (cid, slug, Jsonb(capabilities_for("vsphere", "real")), Jsonb({})),
    )
    cfg = vsphere.VsphereConfig.model_validate({"url": url, "ca_pem": ca_pem})
    host, port = vsphere.endpoint(cfg.url)
    conn.execute(
        "INSERT INTO approved_endpoints (id, connector_id, scheme, host, port, ca_bundle_pem) "
        "VALUES (%s, %s, 'https', %s, %s, %s)",
        (uuid7(), cid, host, port, ca_pem),
    )
    conn.execute(
        "UPDATE connectors SET config = %s, config_version = 1 WHERE id = %s",
        (Jsonb({"url": cfg.url, "ca_imported": ca_pem is not None}), cid),
    )
    conn.commit()
    return str(cid)


def test_f31_nominal_preflight_puis_plan(conn, demo, worker):
    c = flows.create_and_submit(conn, demo)
    assert flows.job(conn, c["job_id"])["kind"] == "preflight"
    assert flows.job(conn, c["job_id"])["class"] == "read"
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    assert flows.change(conn, c["id"])["state"] == "plan_ready"
    jobs = flows.jobs_of(conn, c["id"])
    assert [j["kind"] for j in jobs[:2]] == ["preflight", "tf_plan"]
    assert all(j["state"] == "succeeded" for j in jobs[:2])
    steps = conn.execute(
        "SELECT name, simulated, state FROM job_steps WHERE job_id = %s ORDER BY seq",
        (c["job_id"],),
    ).fetchall()
    assert any(s["simulated"] and "vCenter" in s["name"] for s in steps)


def test_f31_tls_untrusted_bloque_le_plan(conn, demo, worker, tmp_path):
    ca = _ca("Rackloom F31 CA")
    leaf = _leaf(ca, "localhost")
    with tls_server(tmp_path, leaf) as port:
        canary = f"RLCANARY-{secrets.token_hex(16)}"
        _add_real_vsphere(conn, url=f"https://localhost:{port}", ca_pem=None)
        secrets_mod.put(
            conn,
            "secret://connectors/vsphere-f31/password",
            canary.encode(),
            demo.user("admin")["id"],
        )
        conn.execute(
            "UPDATE connectors SET secret_refs = %s WHERE slug = 'vsphere-f31'",
            (Jsonb({"password": "secret://connectors/vsphere-f31/password"}),),
        )
        conn.commit()

        c = flows.create_and_submit(conn, demo)
        assert worker.drain(kinds=["preflight", "tf_plan"]) == ["preflight"]
        change = flows.change(conn, c["id"])
        assert change["state"] == "plan_failed"
        tr = conn.execute(
            "SELECT reason_code, detail FROM state_transitions WHERE change_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (c["id"],),
        ).fetchone()
        assert tr["reason_code"] == "tls_untrusted"
        assert "certificat" in (tr["detail"] or {}).get("message", "").lower()
        assert canary not in str(tr["detail"])
        assert not flows.jobs_of(conn, c["id"], "tf_plan")

        dump = conn.execute(
            "SELECT string_agg(t::text, '') AS s FROM ("
            "SELECT detail::text AS t FROM state_transitions WHERE change_id = %s "
            "UNION ALL SELECT data::text FROM events WHERE correlation_id = %s "
            "UNION ALL SELECT coalesce(result::text, '') FROM jobs WHERE change_id = %s"
            ") x",
            (c["id"], c["id"], c["id"]),
        ).fetchone()["s"] or ""
        assert canary not in dump


def test_f31_state_unreachable_bloque_le_plan(conn, demo, worker, worker_env):
    c = flows.create_and_submit(conn, demo)
    dep = flows.deployment(conn, c["deployment_id"])
    stack = conn.execute("SELECT * FROM stacks WHERE id = %s", (dep["stack_id"],)).fetchone()
    before = preflight.state_fingerprint(worker.settings, stack["slug"])
    conn.execute(
        "UPDATE stacks SET backend_kind = 'http', backend_ref = %s WHERE id = %s",
        ("https://127.0.0.1:9/state", stack["id"]),
    )
    conn.commit()

    assert worker.drain(kinds=["preflight", "tf_plan"]) == ["preflight"]
    change = flows.change(conn, c["id"])
    assert change["state"] == "plan_failed"
    tr = conn.execute(
        "SELECT reason_code, detail FROM state_transitions WHERE change_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (c["id"],),
    ).fetchone()
    assert tr["reason_code"] == "state_unreachable"
    assert "state" in (tr["detail"] or {}).get("message", "").lower()
    assert not flows.jobs_of(conn, c["id"], "tf_plan")
    after = preflight.state_fingerprint(worker.settings, stack["slug"])
    assert after == before


def test_f31_state_local_inchange_apres_succes(conn, demo, worker, worker_env):
    c = flows.create_and_submit(conn, demo)
    dep = flows.deployment(conn, c["deployment_id"])
    stack = conn.execute("SELECT * FROM stacks WHERE id = %s", (dep["stack_id"],)).fetchone()
    # State du runner (ProcessBackend) : le préflight ne doit jamais l'écrire.
    state_dir = Path(worker.settings.data_dir) / "tfstate" / stack["slug"]
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "terraform.tfstate"
    payload = b'{"version":4,"serial":1,"lineage":"f31-demo","resources":[]}'
    state_file.write_bytes(payload)
    before = preflight.state_fingerprint(worker.settings, stack["slug"])
    mtime_before = state_file.stat().st_mtime_ns

    assert worker.drain(kinds=["preflight"]) == ["preflight"]
    assert state_file.read_bytes() == payload
    assert state_file.stat().st_mtime_ns == mtime_before
    assert preflight.state_fingerprint(worker.settings, stack["slug"]) == before
    assert flows.job(conn, c["job_id"])["state"] == "succeeded"
    assert flows.change(conn, c["id"])["state"] == "planning"


def test_f31_classe_read_admet_pendant_gel(conn, demo):
    c = flows.create_and_submit(conn, demo)
    env = demo.env("vitrine", "dev")
    freezes.freeze(conn, demo.principal("bruno"), env["id"], "gel F31")
    conn.commit()
    job = flows.job(conn, c["job_id"])
    change = flows.change(conn, c["id"])
    decision = admission.evaluate(
        admission.Context(
            conn=conn,
            job=job,
            change=change,
            environment=env,
            execution=None,
            approval=None,
            plan=None,
        )
    )
    assert decision["outcome"] == "admitted"
    assert "R080" not in [r["id"] for r in decision["rules"]]
    conn.rollback()
