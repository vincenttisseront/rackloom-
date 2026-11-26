"""F46 : banc d'évaluation Ollama — conversations fictives, métriques, qualification.

Niveau de preuve : testé localement (adaptateur fictif). Qualifier un vrai modèle exige
Ollama local ; non exécuté ici (D2).
"""

from __future__ import annotations

import pytest

from rackloom.chat import PROMPT_ID
from rackloom.chat import eval as chat_eval

API = "/api/v1"


def _counts(conn) -> tuple[int, int, int, int]:
    conn.commit()
    row = conn.execute(
        "SELECT (SELECT count(*) FROM approvals) AS a, (SELECT count(*) FROM jobs) AS j, "
        "(SELECT count(*) FROM request_versions) AS v, (SELECT count(*) FROM changes) AS c"
    ).fetchone()
    return row["a"], row["j"], row["v"], row["c"]


def test_f46_nominal_qualifie(api, conn):
    before = _counts(conn)
    admin = api("admin")
    r = admin.post(f"{API}/admin/assistant/eval", json={"adapter": "fake", "scenario": "nominal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "passed"
    assert body["failure_codes"] == []
    assert body["adapter"] == "fake"
    assert body["model"] == "fake:nominal"
    assert body["prompt_id"] == PROMPT_ID
    assert body["metrics"]["need_resolved"] >= 1
    assert body["metrics"]["production_defaults"] == 0
    assert body["metrics"]["vlan_questions"] == 0
    assert body["metrics"]["unnecessary_questions"] == 0
    assert _counts(conn) == before
    listed = admin.get(f"{API}/admin/assistant/eval").json()
    assert listed["prompt_id"] == PROMPT_ID
    assert listed["items"][0]["id"] == body["id"]
    detail = admin.get(f"{API}/admin/assistant/eval/{body['id']}").json()
    assert detail["verdict"] == "passed"
    assert {c["id"] for c in detail["cases"]} == {f["id"] for f in chat_eval.FIXTURES}


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("defaults_prod", "defaults_prod"),
        ("asks_vlan", "asks_vlan"),
    ],
)
def test_f46_regression_echoue_qualification(api, conn, scenario, code):
    before = _counts(conn)
    r = api("admin").post(f"{API}/admin/assistant/eval", json={"adapter": "fake", "scenario": scenario})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "failed"
    assert code in body["failure_codes"]
    assert body["model"] == f"fake:{scenario}"
    assert body["prompt_id"] == PROMPT_ID
    assert _counts(conn) == before


def test_f46_override_locked_echoue(api):
    r = api("admin").post(f"{API}/admin/assistant/eval", json={"adapter": "fake", "scenario": "override_attempt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "failed"
    assert "locked_field" in body["failure_codes"]


def test_f46_reserve_admin(api):
    assert api("alice").post(f"{API}/admin/assistant/eval", json={}).status_code == 403
    assert api("alice").get(f"{API}/admin/assistant/eval").status_code == 403


def test_f46_desactive_refuse(api):
    api("admin").put(f"{API}/admin/assistant", json={"adapter": "disabled", "scenario": "nominal"})
    r = api("admin").post(f"{API}/admin/assistant/eval", json={})
    assert r.status_code == 422 and r.json()["code"] == "eval_disabled"


def test_f46_config_expose_prompt(api):
    cfg = api("admin").get(f"{API}/admin/assistant").json()
    assert cfg["prompt_id"] == PROMPT_ID
