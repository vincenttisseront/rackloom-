"""Résolveur central : besoin seul, champs techniques refusés, jamais la production par défaut."""

from __future__ import annotations

import pytest

from rackloom import resolver
from rackloom.errors import ApiError


def _need(**kw):
    base = {"schema": "rackloom.need/v1", "template": "ubuntu-web", "usage": "test"}
    base.update(kw)
    return base


def _resolve(conn, demo, project: str, raw: dict):
    need = resolver.parse_need(raw)
    prov = resolver.parse_provenance({}, need)
    return resolver.resolve(conn, project=demo.project(project), need=need, provenance=prov)


def test_besoin_minimal_resolu_sans_choix_technique(conn, demo):
    res = _resolve(conn, demo, "vitrine", _need())
    assert res["status"] == "resolved"
    assert res["environment"]["slug"] == "lab"
    assert res["provenance"]["/environment"]["source"] == "single_option"
    assert res["network"]["name"] == "net-lab-vitrine"
    # Départage explicite : espace libre décroissant puis nom.
    assert res["placement"]["datastore"]["name"] == "ds-lab-02"
    assert res["image"]["name"] == "tpl-ubuntu-2404"
    assert [vm["name"] for vm in res["vms"]] == ["vitrine-lab-web-01"]
    assert res["size"]["key"] == "standard"
    assert res["provenance"]["/size"]["source"] == "template_default"
    assert res["template"] == {"slug": "ubuntu-web", "version": "1.0.0", "digest": res["template"]["digest"],
                               "title": "Serveur web Ubuntu (Nginx)"}
    assert res["policy"]["version"] == "builtin@1"
    assert res["inventory"]["stale"] is False
    assert [c["name"] for c in res["components"]] == ["nginx"]
    assert res["approval"]["distinct_approver"] is True


def test_resolution_deterministe(conn, demo):
    a = _resolve(conn, demo, "vitrine", _need(quantity=2, size="light"))
    b = _resolve(conn, demo, "vitrine", _need(quantity=2, size="light"))
    assert resolver.resolution_digest(a) == resolver.resolution_digest(b)
    assert [vm["name"] for vm in a["vms"]] == ["vitrine-lab-web-01", "vitrine-lab-web-02"]
    assert a["vms"][0]["cpu"] == 1


@pytest.mark.parametrize("field,value", [
    ("datastore", "datastore-202"),      # même valeur que la résolution : refusé quand même
    ("network", "network-101"),
    ("vlan", 101),
    ("cpu", 2),
    ("memory_mb", 2048),
    ("name", "vitrine-lab-web-01"),
    ("template_version", "1.0.0"),
    ("runner", "terraform"),
    ("state_backend", "local"),
    ("roles", ["rackloom.nginx"]),
    ("ansible_become_user", "root"),
    ("ip", "192.0.2.10"),
])
def test_champ_technique_refuse_meme_egal_au_defaut(field, value):
    with pytest.raises(ApiError) as exc:
        resolver.parse_need(_need(**{field: value}))
    assert exc.value.status == 422
    assert exc.value.code == "locked_field"
    assert exc.value.field == f"/{field}"


def test_champ_inconnu_refuse():
    with pytest.raises(ApiError) as exc:
        resolver.parse_need(_need(couleur="bleu"))
    assert exc.value.code == "unknown_field"


def test_production_jamais_par_defaut(conn, demo):
    res = _resolve(conn, demo, "vitrine", _need(usage="production"))
    assert res["status"] == "needs_input"
    assert "environment" not in res
    question = res["questions"][0]
    assert question["field"] == "/environment_id"
    assert [o["label"] for o in question["options"]] == ["Production"]
    # L'usage « test » n'atteint jamais la production, même si elle existe dans le projet.
    test = _resolve(conn, demo, "vitrine", _need())
    assert test["environment"]["kind"] != "prod"


def test_production_explicite_bloquee_avec_explication(conn, demo):
    prod = demo.env("vitrine", "prod")
    res = _resolve(conn, demo, "vitrine", _need(usage="production", environment_id=str(prod["id"])))
    assert res["status"] == "blocked"
    codes = {b["code"] for b in res["blocked"]}
    assert "no_network" in codes
    assert "network" not in res


def test_usage_test_refuse_en_production_explicite(conn, demo):
    prod = demo.env("vitrine", "prod")
    res = _resolve(conn, demo, "vitrine", _need(environment_id=str(prod["id"])))
    assert res["status"] == "blocked"
    assert res["blocked"][0]["code"] == "environment_not_allowed"


def test_aucun_reseau_explique_sans_parametre_technique(conn, demo):
    res = _resolve(conn, demo, "recherche", _need())
    assert res["status"] == "blocked"
    block = next(b for b in res["blocked"] if b["code"] == "no_network")
    assert block["message"].startswith("Aucun réseau n'est disponible pour ce profil")
    assert "field" not in block
    assert res["questions"] == []


def test_inventaire_perime_suspend_la_resolution(conn, demo):
    conn.execute("UPDATE inventory_snapshots SET observed_at = now() - interval '2 days'")
    res = _resolve(conn, demo, "vitrine", _need())
    assert res["status"] == "blocked"
    assert res["blocked"][0]["code"] == "inventory_stale"


def test_suggestion_non_confirmee_signalee():
    need = resolver.parse_need(_need(size="high"))
    prov = resolver.parse_provenance({"/size": {"source": "suggestion"}}, need)
    assert resolver.unconfirmed_suggestions(prov) == ["/size"]
