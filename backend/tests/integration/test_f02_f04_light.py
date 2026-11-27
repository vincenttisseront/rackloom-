"""F02/F04 sans moteur de conteneurs : résolution composition + graphe scopé.

Les recettes labo (tf_apply, ansible, reprise) restent dans
`test_f02_f04_compositions_graph.py` (marqueur terraform, CI).
"""

from __future__ import annotations

import flows
from rackloom import graph
from rackloom.changes import service
from rackloom.db import Jsonb
from rackloom.ids import uuid7


def _publish_app_base(conn, demo) -> None:
    from rackloom import registry

    row = conn.execute(
        "SELECT digest FROM template_versions WHERE slug = 'ubuntu-app-base' AND version = '1.0.0'"
    ).fetchone()
    assert row is not None
    if conn.execute(
        "SELECT status FROM template_versions WHERE slug = 'ubuntu-app-base' AND version = '1.0.0'"
    ).fetchone()["status"] != "published":
        registry.publish_action(
            conn,
            demo.principal("pub"),
            "ubuntu-app-base",
            "1.0.0",
            "publish",
            "Composition multi-VM F02",
            row["digest"],
        )
        conn.commit()


def test_f02_resolution_composition_ordonne_les_membres(conn, demo, api):
    _publish_app_base(conn, demo)
    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "Composition app+socle")
    res = service.add_version(
        conn,
        p,
        change["id"],
        {"need": flows.need(template="ubuntu-app-base", quantity=1), "base_version_no": 0},
    )
    conn.commit()
    assert res["revision"]["outcome"] == "resolved", res["revision"]
    resolved = res["revision"]["resolved"]
    assert [i["member"] for i in resolved["instances"]] == ["base", "application"]
    assert len(resolved["waves"]) == 2
    assert resolved["waves"][0] == [resolved["instances"][0]["name"]]
    assert resolved["waves"][1] == [resolved["instances"][1]["name"]]
    assert "base" in resolved["member_components"]
    assert "application" in resolved["member_components"]
    assert resolved["member_components"]["base"][0]["slug"] == "fail2ban"
    assert resolved["member_components"]["application"][0]["slug"] == "nginx"

    # Vue API du changement : plan détails / instances avec membre.
    view = api("alice").get(f"/api/v1/changes/{change['id']}")
    assert view.status_code == 200


def test_f04_impact_masque_les_vm_hors_perimetre(conn, demo, api):
    """Deux projets, une VM chacun sur le même réseau ; chaque opérateur ne voit que les siennes."""
    vitrine = demo.project("vitrine")
    compta = demo.project("compta")
    env_v = conn.execute(
        "SELECT id FROM environments WHERE project_id = %s AND slug = 'dev'", (vitrine["id"],)
    ).fetchone()
    env_c = conn.execute(
        "SELECT id FROM environments WHERE project_id = %s AND slug = 'dev'", (compta["id"],)
    ).fetchone()
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' LIMIT 1"
    ).fetchone()
    network_id = "network-101"

    def _seed_host(project_id, env_id, name: str, vm_id: str) -> None:
        stack_id = uuid7()
        dep_id = uuid7()
        change_id = uuid7()
        conn.execute(
            "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
            "VALUES (%s, %s, %s, %s, 'local_demo', %s)",
            (stack_id, f"stk-{name}", project_id, env_id, f"demo/{dep_id}"),
        )
        conn.execute(
            "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
            "VALUES (%s, 'create', %s, %s, %s, 'succeeded', %s)",
            (change_id, project_id, env_id, name, demo.principal("alice").user_id
             if project_id == vitrine["id"] else demo.principal("chloe").user_id),
        )
        owner = (
            demo.principal("alice").user_id
            if project_id == vitrine["id"]
            else demo.principal("chloe").user_id
        )
        conn.execute(
            "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
            "stack_id, created_by_change_id, owner_id, status, instances) "
            "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', %s)",
            (
                dep_id,
                project_id,
                env_id,
                tpl["id"],
                stack_id,
                change_id,
                owner,
                Jsonb([]),
            ),
        )
        conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (dep_id, change_id))
        # Révision résolue minimale pour le placement réseau.
        conn.execute(
            "INSERT INTO request_versions "
            "(id, change_id, version_no, need, provenance, need_sha256, source, created_by) "
            "VALUES (%s, %s, 1, %s, %s, %s, 'api', %s)",
            (
                uuid7(),
                change_id,
                Jsonb({"schema": "rackloom.need/v1", "service": {"template": "ubuntu-web"}, "inputs": {}}),
                Jsonb({}),
                "x" * 64,
                owner,
            ),
        )
        conn.execute(
            "INSERT INTO request_revisions (id, change_id, revision_no, version_no, outcome, template_version_id, "
            "environment_id, policy_bundle_version, resolved, provenance, resolution_digest) "
            "VALUES (%s, %s, 1, 1, 'resolved', %s, %s, 'builtin@1', %s, %s, %s)",
            (
                uuid7(),
                change_id,
                tpl["id"],
                env_id,
                Jsonb({
                    "placement": {"network": {"stable_id": network_id, "name": "net-dev-vitrine-a"}},
                    "instances": [{"name": name}],
                }),
                Jsonb({}),
                "y" * 64,
            ),
        )
        conn.execute("UPDATE changes SET current_version_no = 1, current_revision_no = 1 WHERE id = %s", (change_id,))
        conn.execute(
            "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
            "stack_id, vm_stable_id, name, deployment_state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verified')",
            (uuid7(), project_id, env_id, dep_id, tpl["id"], change_id, stack_id, vm_id, name),
        )

    _seed_host(vitrine["id"], env_v["id"], "vit-dev-web-91", "vm-lab-aaaa1111")
    _seed_host(compta["id"], env_c["id"], "cpt-dev-web-91", "vm-lab-bbbb2222")
    conn.commit()

    built = graph.build(conn, built_by="test-light")
    conn.commit()
    assert built["edge_count"] >= 2

    alice_impact = api("alice").get(f"/api/v1/dependency-graph/impact/network/{network_id}")
    assert alice_impact.status_code == 200, alice_impact.text
    alice_names = {d["name"] for d in alice_impact.json()["dependents"]}
    assert "vit-dev-web-91" in alice_names
    assert "cpt-dev-web-91" not in alice_names
    assert alice_impact.json()["hidden_count"] >= 1

    chloe_impact = api("chloe").get(f"/api/v1/dependency-graph/impact/network/{network_id}")
    assert chloe_impact.status_code == 200
    chloe_names = {d["name"] for d in chloe_impact.json()["dependents"]}
    assert "cpt-dev-web-91" in chloe_names
    assert "vit-dev-web-91" not in chloe_names

    # VM hors périmètre → 404.
    assert api("chloe").get("/api/v1/dependency-graph/impact/vm/vm-lab-aaaa1111").status_code == 404
    assert api("alice").get("/api/v1/dependency-graph/impact/vm/vm-lab-aaaa1111").status_code == 200
