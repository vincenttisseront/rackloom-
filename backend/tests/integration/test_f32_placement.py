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
from rackloom.db import Jsonb
from rackloom.errors import ApiError


def _enable_anti_affinity(conn) -> None:
    """Active anti_affinity sur la version publiée pour la durée du test (déclencheur désactivé)."""
    conn.execute("ALTER TABLE template_versions DISABLE TRIGGER template_versions_content")
    try:
        row = conn.execute(
            "SELECT id, contract FROM template_versions WHERE slug = 'ubuntu-web' AND version = '1.0.0'"
        ).fetchone()
        contract = dict(row["contract"])
        policy = dict(contract["deployment_policy"])
        policy["anti_affinity"] = {"scope": "cluster"}
        contract["deployment_policy"] = policy
        # digest inchangé volontairement : le contenu est muté uniquement pour le test local.
        conn.execute(
            "UPDATE template_versions SET contract = %s WHERE id = %s",
            (Jsonb(contract), row["id"]),
        )
        conn.commit()
    finally:
        conn.execute("ALTER TABLE template_versions ENABLE TRIGGER template_versions_content")
        conn.commit()


def test_f32_replicas_separes_deterministes(conn, demo):
    _enable_anti_affinity(conn)
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
    assert a["anti_affinity"] == {"scope": "cluster"}
    assert a["values"]["/placement/instances/0/cluster"]["label"] == "cl-dev-a"
    assert a["values"]["/placement/instances/1/cluster"]["label"] == "cl-dev-b"
    assert [i["datastore"]["stable_id"] for i in a["instances"]] == ["datastore-201", "datastore-202"]
    # Refs inventaire (stable_id) : le bundle d'exécution les lit ; ipam_preview (F03) est métadonnée.
    for key, val in a["placement"].items():
        if key == "ipam_preview":
            continue
        if isinstance(val, dict):
            assert "stable_id" in val, key


def test_f32_anti_affinity_unsatisfiable(conn, demo):
    _enable_anti_affinity(conn)
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


def test_f32_sans_anti_affinity_un_seul_cluster(conn, demo):
    """Parcours nominal : sans anti_affinity, quantité 2 reste sur le premier cluster classé."""
    p = demo.principal("alice")
    project = demo.project("vitrine")
    n = resolver.validate_need(need(quantity=2))
    prov = resolver.validate_provenance({}, n)
    res = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    assert res["outcome"] == "resolved"
    assert res["anti_affinity"] is None
    assert [i["cluster"]["stable_id"] for i in res["instances"]] == ["domain-c10", "domain-c10"]


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
