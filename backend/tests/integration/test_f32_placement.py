"""F32 : placement multi-cluster et anti-affinité.

Inventaire fictif à deux clusters → réplicas séparés, même `resolution_digest` ;
un seul cluster autorisé → blocage `anti_affinity_unsatisfiable` ; aucun champ
cluster dans le besoin (422 `locked_field`).
Niveau de preuve : testé localement (inventaire fictif, PostgreSQL éphémère).
"""

from __future__ import annotations

import pytest

from flows import need
from rackloom import resolver
from rackloom.changes import service
from rackloom.errors import ApiError


def test_f32_replicas_separes_deterministes(conn, demo):
    p = demo.principal("alice")
    project = demo.project("vitrine")
    n = resolver.validate_need(need(quantity=2))
    prov = resolver.validate_provenance({}, n)
    a = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    b = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    assert a["outcome"] == "resolved"
    assert a["resolution_digest"] == b["resolution_digest"]
    clusters = [i["cluster"]["stable_id"] for i in a["instances"]]
    assert clusters == ["domain-c10", "domain-c11"]
    assert clusters[0] != clusters[1]
    assert a["placement"]["anti_affinity"] == {"scope": "cluster"}
    assert a["values"]["/placement/instances/0/cluster"]["label"] == "cl-dev-a"
    assert a["values"]["/placement/instances/1/cluster"]["label"] == "cl-dev-b"
    # Datastores alignés sur le site du cluster.
    assert [i["datastore"]["stable_id"] for i in a["instances"]] == ["datastore-201", "datastore-202"]


def test_f32_anti_affinity_unsatisfiable(conn, demo):
    # Un seul cluster octroyé à vitrine/dev → quantité 2 impossible.
    env = demo.env("vitrine", "dev")
    conn.execute(
        "UPDATE resource_grants SET revoked_at = now() WHERE environment_id = %s AND kind = 'cluster' "
        "AND stable_id = 'domain-c11'",
        (env["id"],),
    )
    conn.commit()
    p = demo.principal("alice")
    project = demo.project("vitrine")
    n = resolver.validate_need(need(quantity=2))
    prov = resolver.validate_provenance({}, n)
    res = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    assert res["outcome"] == "blocked"
    assert res["blocks"][0]["code"] == "anti_affinity_unsatisfiable"
    assert "sépare les réplicas" in res["blocks"][0]["message"]


def test_f32_cluster_refuse_dans_le_besoin(conn, demo):
    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "F32")
    with pytest.raises(ApiError) as exc:
        service.add_version(
            conn,
            p,
            change["id"],
            {"need": need(cluster="domain-c10"), "base_version_no": 0},
        )
    assert exc.value.code == "locked_field"
    assert exc.value.field == "/inputs/cluster"
