"""F34 : contrôle des licences (R125).

Quota épuisé → `license_unavailable` ; source injoignable → non vérifié puis blocage
selon la politique ; aucun champ de clé dans le parcours.
Niveau de preuve : testé localement (source fictive).
"""

from __future__ import annotations

import pytest

import flows
from rackloom import admission, licenses
from rackloom.db import Jsonb
from rackloom.policy import BUILTIN_VERSION


def test_f34_quota_epuise(conn):
    licenses.set_quota(conn, "lab-nginx", available=0, reachable=True)
    result = licenses.check(conn, [{"sku": "lab-nginx", "quantity": 1}])
    assert result["status"] == "unavailable"
    code, _ = licenses.refusal(result)
    assert code == "license_unavailable"


def test_f34_non_verifie_et_politique(conn):
    licenses.set_quota(conn, "lab-nginx", available=10, reachable=False)
    result = licenses.check(conn, [{"sku": "lab-nginx", "quantity": 1}])
    assert result["status"] == "unverified"
    assert licenses.unverified_policy() == "block"
    code, message = licenses.refusal(result)
    assert code == "license_unverified"
    assert "non vérifié" in message
    assert BUILTIN_VERSION == "builtin@10"


def test_f34_r125_refuse_a_admission(conn, demo):
    """R125 bloqué sur un contexte d'admission minimal (job write + révision avec licence)."""
    licenses.set_quota(conn, "lab-nginx", available=0, reachable=True)
    revision = {
        "resolved": {
            "licenses": [{"sku": "lab-nginx", "per_instance": 1}],
            "instances": [{"name": "vit-dev-web-01"}],
        }
    }
    ctx = admission.Context(
        conn=conn,
        job={
            "kind": "tf_apply",
            "class": "write",
            "id": demo.user("alice")["id"],
            "cancel_requested_at": None,
            "stack_id": None,
            "payload": {},
        },
        change={"id": demo.user("alice")["id"], "project_id": demo.project("vitrine")["id"]},
        environment=demo.env("vitrine", "dev"),
        execution=None,
        approval=None,
        plan=None,
        revision=revision,
    )
    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R125")
    verdict = fn(ctx)
    assert not verdict.ok
    assert verdict.code == "license_unavailable"


def test_f34_pas_de_champ_cle_dans_le_besoin(conn, demo):
    from rackloom.changes import service
    from rackloom.errors import ApiError

    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "F34")
    with pytest.raises(ApiError) as exc:
        service.add_version(
            conn,
            p,
            change["id"],
            {"need": flows.need(license_key="AAAA-BBBB"), "base_version_no": 0},
        )
    assert exc.value.code == "locked_field"


@pytest.mark.terraform
def test_f34_bloque_tf_apply(conn, demo, worker):
    """Parcours complet : exigence injectée hors trigger d'immuabilité, apply bloqué R125."""
    licenses.set_quota(conn, "lab-nginx", available=0, reachable=True)
    conn.commit()
    c = flows.create_and_submit(conn, demo)
    conn.execute("ALTER TABLE request_revisions DISABLE TRIGGER request_revisions_immutable")
    try:
        rev = conn.execute(
            "SELECT id, resolved FROM request_revisions WHERE change_id = %s "
            "ORDER BY revision_no DESC LIMIT 1",
            (c["id"],),
        ).fetchone()
        resolved = dict(rev["resolved"])
        resolved["licenses"] = [{"sku": "lab-nginx", "per_instance": 1}]
        conn.execute(
            "UPDATE request_revisions SET resolved = %s WHERE id = %s",
            (Jsonb(resolved), rev["id"]),
        )
        conn.commit()
    finally:
        conn.execute("ALTER TABLE request_revisions ENABLE TRIGGER request_revisions_immutable")
        conn.commit()
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked"
    assert job["blocked_reason"] == "license_unavailable"
