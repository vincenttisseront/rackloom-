"""F07 : fabrique d'images — provenance, tests, promotion, dépréciation.

Recette : une image qui échoue à ses tests ne peut pas être promue vers le catalogue
de production ; déprécier conserve l'image. Niveau : testé localement (fabrique fictive, D11).
"""

from __future__ import annotations

import hashlib

import pytest

from rackloom import factory
from rackloom.errors import ApiError

API = "/api/v1"


def _sha256(payload: bytes = b"lab-iso-f07") -> str:
    return hashlib.sha256(payload).hexdigest()


def test_f07_echec_tests_bloque_promotion_prod(conn, demo, api):
    """Recette F07 : échec de tests → promotion prod refusée ; succès → OK ; dépréciation sans retrait."""
    pub = api("pub")
    source = _sha256()

    created = pub.post(
        f"{API}/admin/images",
        json={
            "slug": "ubuntu-2404-lab",
            "version": "2.0.0",
            "title": "Ubuntu 24.04 lab F07",
            "source_kind": "iso_digest",
            "source_digest": source,
            "os": "ubuntu-24.04",
            "layout_id": "std-v1",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "draft"
    assert body["simulated"] is True
    assert body["badge"] == "Simulé"
    assert body["source_digest"] == source

    built = pub.post(f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0/build")
    assert built.status_code == 200, built.text
    assert built.json()["status"] == "built"
    assert built.json()["image_digest"]
    assert built.json()["stable_id"]

    failed = pub.post(
        f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0/test",
        json={"fail": True},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert any(t["outcome"] == "failed" for t in failed.json()["tests"])

    refuse = pub.post(
        f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0/promote",
        json={"environment_kind": "prod", "reason": "tentative prod après échec"},
    )
    assert refuse.status_code == 409, refuse.text
    assert refuse.json()["code"] == "image_tests_failed"

    refuse_dev = pub.post(
        f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0/promote",
        json={"environment_kind": "dev", "reason": "tentative dev après échec"},
    )
    assert refuse_dev.status_code == 409
    assert refuse_dev.json()["code"] == "image_tests_failed"

    # Nouvelle version : tests OK → promotion prod.
    ok = pub.post(
        f"{API}/admin/images",
        json={
            "slug": "ubuntu-2404-lab",
            "version": "2.1.0",
            "title": "Ubuntu 24.04 lab F07",
            "source_kind": "iso_digest",
            "source_digest": hashlib.sha256(b"lab-iso-f07-ok").hexdigest(),
            "os": "ubuntu-24.04",
            "layout_id": "std-v1",
        },
    )
    assert ok.status_code == 200, ok.text
    assert pub.post(f"{API}/admin/images/ubuntu-2404-lab/versions/2.1.0/build").status_code == 200
    tested = pub.post(f"{API}/admin/images/ubuntu-2404-lab/versions/2.1.0/test", json={})
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "ready"
    assert all(t["outcome"] == "passed" for t in tested.json()["tests"])

    promo = pub.post(
        f"{API}/admin/images/ubuntu-2404-lab/versions/2.1.0/promote",
        json={"environment_kind": "prod", "reason": "promotion labo F07"},
    )
    assert promo.status_code == 200, promo.text
    assert promo.json()["promotions"][0]["environment_kind"] == "prod"
    assert "ne modifie ni plan" in promo.json()["note"]

    # Dépréciation sans retrait : toujours listée, promotions conservées.
    dep = pub.post(
        f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0/deprecate",
        json={"reason": "remplacée par 2.1.0"},
    )
    assert dep.status_code == 200, dep.text
    assert dep.json()["status"] == "deprecated"
    assert "conservée" in dep.json()["note"]

    listing = pub.get(f"{API}/admin/images")
    assert listing.status_code == 200
    lab = next(b for b in listing.json() if b["slug"] == "ubuntu-2404-lab")
    versions = {v["version"]: v for v in lab["versions"]}
    assert versions["2.0.0"]["deprecated"] is True
    assert versions["2.0.0"]["status"] == "deprecated"
    assert versions["2.1.0"]["status"] == "ready"

    detail = pub.get(f"{API}/admin/images/ubuntu-2404-lab/versions/2.0.0")
    assert detail.status_code == 200
    assert detail.json()["status"] == "deprecated"
    assert detail.json()["tests"]  # résultats conservés

    # Opérateur : lecture refusée (pas éditeur).
    op = api("alice")
    denied = op.get(f"{API}/admin/images")
    assert denied.status_code == 403


def test_f07_module_promote_refuse_failed(conn, demo):
    """Garde directe du module (sans HTTP) : failed → image_tests_failed."""
    pub = demo.principal("pub")
    view = factory.create(
        conn,
        pub,
        slug="img-unit",
        version="1.0.0",
        title="Unit",
        source_kind="iso_digest",
        source_digest=_sha256(),
        os="ubuntu-24.04",
        layout_id="std-v1",
    )
    factory.build(conn, pub, "img-unit", "1.0.0")
    factory.run_tests(conn, pub, "img-unit", "1.0.0", fail=True)
    with pytest.raises(ApiError) as exc:
        factory.promote(
            conn,
            pub,
            "img-unit",
            "1.0.0",
            environment_kind="prod",
            reason="ne doit pas passer",
        )
    assert exc.value.code == "image_tests_failed"
    got = factory.get(conn, "img-unit", "1.0.0")
    assert got is not None and got["status"] == "failed"
    assert view["slug"] == "img-unit"
