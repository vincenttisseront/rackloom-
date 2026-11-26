"""F33 : sorties de déploiement réutilisables.

Sortie hors liste jamais publiée ; lecture par un autre projet → 404 ;
l'API de sorties ne lit pas le state.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from rackloom.db import Jsonb
from rackloom.deployments import outputs as deployment_outputs
from rackloom.errors import ApiError
from rackloom.ids import uuid7


def _plant(conn, demo, *, allow: list[str], instances: list[dict]):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND version = '1.0.0'"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id = uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, %s, %s, 'f33', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f33')",
        (stack_id, f"f33-{deployment_id.hex[:8]}", project["id"], env["id"]),
    )
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, instances, service_status) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', %s, 'unknown')",
        (deployment_id, project["id"], env["id"], tpl["id"], stack_id, change_id, alice["id"], Jsonb(instances)),
    )
    n = deployment_outputs.publish(
        conn,
        deployment_id=deployment_id,
        project_id=project["id"],
        template_version_id=tpl["id"],
        instances=instances,
        allowlist=set(allow),
    )
    conn.commit()
    return deployment_id, n


def test_f33_publie_allowlist_seulement(conn, demo):
    dep_id, n = _plant(
        conn,
        demo,
        allow=["name", "vm_uuid"],
        instances=[
            {"name": "vit-dev-web-01", "vm_uuid": "lab-uuid-1"},
            {"name": "canary-skip", "vm_uuid": "canary-should-skip"},
        ],
    )
    assert n == 2
    view = deployment_outputs.latest(conn, demo.principal("alice"), dep_id)
    assert view["version"] == 1
    keys = {(o["key"], o["value"]) for o in view["outputs"]}
    assert keys == {("name", "vit-dev-web-01"), ("vm_uuid", "lab-uuid-1")}


def test_f33_hors_liste_jamais_publiee(conn, demo):
    dep_id, n = _plant(
        conn,
        demo,
        allow=["name"],
        instances=[{"name": "vit-dev-web-01", "vm_uuid": "hidden", "fqdn": "web.demo.invalid"}],
    )
    assert n == 1
    view = deployment_outputs.latest(conn, demo.principal("alice"), dep_id)
    assert [o["key"] for o in view["outputs"]] == ["name"]


def test_f33_autre_projet_404(conn, demo, api):
    dep_id, _ = _plant(
        conn, demo, allow=["name"], instances=[{"name": "vit-dev-web-01", "vm_uuid": "u1"}]
    )
    assert api("chloe").get(f"/api/v1/deployments/{dep_id}/outputs").status_code == 404
    with pytest.raises(ApiError) as exc:
        deployment_outputs.latest(conn, demo.principal("chloe"), dep_id)
    assert exc.value.status == 404


def test_f33_api_ne_lit_pas_le_state(conn, demo, api, tmp_path):
    dep_id, _ = _plant(
        conn, demo, allow=["name", "vm_uuid"], instances=[{"name": "vit-dev-web-01", "vm_uuid": "u1"}]
    )
    (tmp_path / "terraform.tfstate").write_text('{"secrets": "do-not-read"}', encoding="utf-8")
    r = api("alice").get(f"/api/v1/deployments/{dep_id}/outputs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "secrets" not in str(body)
    assert body["version"] == 1
    assert api("alice").get(f"/api/v1/deployments/{uuid4()}/outputs").status_code == 404
