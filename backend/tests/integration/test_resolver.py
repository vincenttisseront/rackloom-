"""Résolveur central et révisions : besoin seul, champs techniques refusés, jamais la production par défaut.

Couvre T21, T31 (partie service), T32 (API et client CI), T33, T34 (hors chat), T37, T38 (duplication),
T39 et les décisions D12, D13, D14, D17.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from flows import need, until
from rackloom import registry, resolver
from rackloom.changes import service
from rackloom.errors import ApiError

ROOT = Path(__file__).resolve().parents[3]
SOURCES = {"user", "suggestion", "template", "template_default", "environment_default", "environment_policy",
           "environment_policy_default", "inventory", "tie_break", "platform", "deployment_pinned",
           "template_latest_published"}


def _version(conn, demo, *, who="alice", project="vitrine", body=None, **need_kw):
    p = demo.principal(who)
    change = service.create_change(conn, p, demo.project(project), "Test")
    payload = body if body is not None else {"need": need(**need_kw), "base_version_no": 0}
    return change, service.add_version(conn, p, change["id"], payload)


def test_t31_besoin_d1_resolu_sans_choix_technique(conn, demo):
    _, res = _version(conn, demo)
    rev = res["revision"]
    assert rev["outcome"] == "resolved"
    resolved = rev["resolved"]
    assert resolved["environment"]["slug"] == "dev"
    assert resolved["environment"]["source"] == "environment_default"
    assert resolved["template"]["version"] == "1.0.0"
    assert [i["name"] for i in resolved["instances"]] == ["vit-dev-web-01"]
    assert resolved["instances"][0] | {"existing": False} == {
        "name": "vit-dev-web-01", "size_tier": "test_leger", "vcpu": 2, "memory_mb": 2048, "disk_gb": 20,
        "existing": False}
    assert [c["slug"] for c in resolved["components"]] == ["nginx"]
    assert resolved["components"][0]["config"]["listen_port"] == 80
    # Choix utilisateur : la date de fin (D14) ; tout le reste est résolu côté serveur.
    user_values = [p for p, v in rev["provenance"].items() if v["source"] == "user" and p in resolver.POINTERS]
    assert sorted(user_values) == ["/inputs/lifetime_until", "/service/template"]
    assert res["summary"]["headline"].startswith("1 serveur web Ubuntu 24.04 avec Nginx dans Vitrine / dev, "
                                                 "niveau Test léger, jusqu'au ")
    assert res["summary"]["approval"] == "un approbateur de Vitrine autre que vous"
    assert res["summary"]["verifications"] == "paquet nginx installé, service nginx actif, page HTTP 200"


def test_d14_date_de_fin_seule_question(conn, demo):
    _, res = _version(conn, demo, lifetime_until=None)
    rev = res["revision"]
    assert rev["outcome"] == "incomplete"
    assert [m["field"] for m in rev["resolved"]["missing"]] == ["/inputs/lifetime_until"]
    assert rev["resolved"]["missing"][0]["text"] == "Jusqu'à quelle date en avez-vous besoin ? (30 jours au plus)"
    assert res["change"]["state"] == "needs_info"
    with pytest.raises(ApiError) as exc:
        _version(conn, demo, lifetime_until=until(31))
    assert exc.value.code == "invalid_input" and exc.value.field == "/inputs/lifetime_until"


def test_t37_determinisme_et_provenance(conn, demo):
    p = demo.principal("alice")
    project = demo.project("vitrine")
    n = resolver.validate_need(need(quantity=2))
    prov = resolver.validate_provenance({}, n)
    a = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    b = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    assert a["resolution_digest"] == b["resolution_digest"]
    assert a["resolution_digest"].startswith("sha256:")
    network = a["values"]["/placement/network"]
    assert network["label"] == "net-dev-vitrine-a"
    assert network["source"] == "tie_break" and network["candidates"] == 2
    assert [i["name"] for i in a["instances"]] == ["vit-dev-web-01", "vit-dev-web-02"]
    for pointer, value in a["values"].items():
        assert value["source"] in SOURCES, pointer
        assert value["group"] in {"service", "placement", "reseau", "dimensionnement", "execution", "gouvernance"}
    # Rang explicite : il prime sur l'identifiant stable (D12).
    conn.execute("UPDATE inventory_items SET attributes = attributes || '{\"rank\": 1}' "
                 "WHERE stable_id = 'network-102'")
    c = resolver.resolve(conn, principal=p, project=project, need=n, provenance=prov)
    assert c["values"]["/placement/network"]["label"] == "net-dev-vitrine-b"
    assert c["resolution_digest"] != a["resolution_digest"]


LOCKED = [
    ("network", "network-101"), ("vlan", 101), ("vcpu", 2), ("cpu", 2), ("memory_mb", 2048), ("disk_gb", 20),
    ("image", "vm-500"), ("datastore", "datastore-201"), ("cluster", "domain-c10"), ("folder", "group-v21"),
    ("ip", "192.0.2.10"), ("dns_name", "web.example.invalid"), ("name", "vit-dev-web-01"),
    ("template_version", "1.0.0"), ("runner", "terraform"), ("state_backend", "local_demo"),
    ("roles", ["rackloom.nginx"]), ("ansible_become_user", "root"), ("software", ["nginx"]),
    ("packages", ["nginx"]), ("components", [{"component": "nginx"}]),
]


@pytest.mark.parametrize("field,value", LOCKED)
@pytest.mark.parametrize("where", ["need", "inputs", "body"])
def test_t32_t21_champ_technique_refuse_meme_egal_a_la_resolution(conn, demo, field, value, where):
    """API et client CI passent par la même route : 422 locked_field, aucune version créée."""
    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "Test")
    body = {"need": need(), "base_version_no": 0, "source": "ci"}
    if where == "need":
        body["need"][field] = value
    elif where == "inputs":
        body["need"]["inputs"][field] = value
    else:
        body[field] = value
    with pytest.raises(ApiError) as exc:
        service.add_version(conn, p, change["id"], body)
    assert exc.value.status == 422
    assert exc.value.code == "locked_field"
    assert exc.value.message == "Ce réglage est déterminé par le profil de service."
    assert conn.execute("SELECT count(*) AS n FROM request_versions WHERE change_id = %s",
                        (change["id"],)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_champ_inconnu_non_pris_en_charge(conn, demo):
    with pytest.raises(ApiError) as exc:
        _version(conn, demo, body={"need": {**need(), "couleur": "bleu"}, "base_version_no": 0})
    assert exc.value.code == "field_not_allowed"
    with pytest.raises(ApiError) as exc:
        _version(conn, demo, body={"need": need(couleur="bleu"), "base_version_no": 0})
    assert exc.value.code == "field_not_allowed" and exc.value.field == "/inputs/couleur"


def test_t21_template_hors_registre_refuse_au_chargement(tmp_path):
    cat = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", cat)
    tpl = cat / "templates" / "ubuntu-web" / "1.0.0" / "template.yaml"
    original = tpl.read_text(encoding="utf-8")
    data = yaml.safe_load(original)
    data["components"].append({"component": "redis", "version": "7.0.0"})
    tpl.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(registry.CatalogError, match="redis"):
        registry.scan(cat)
    data = yaml.safe_load(original)
    data["components"][0]["config"]["worker_processes"] = 8
    tpl.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(registry.CatalogError, match="non pris en charge"):
        registry.scan(cat)


def test_t33_d3_compta_sans_reseau(conn, demo):
    change, res = _version(conn, demo, who="chloe", project="compta")
    rev = res["revision"]
    assert rev["outcome"] == "blocked"
    assert [b["code"] for b in rev["block_reasons"]] == ["no_authorized_network"]
    message = rev["block_reasons"][0]["message"]
    assert message == ("Aucun réseau n'est disponible pour ce profil. "
                       "Contactez le référent du projet Compta (Chloé).")
    assert "vitrine" not in message.lower() and "network-" not in message
    assert rev["resolved"]["missing"] == []
    assert rev["resolved"]["environment"]["slug"] == "dev"
    assert res["change"]["state"] == "resolution_blocked"
    with pytest.raises(ApiError) as exc:
        service.submit(conn, demo.principal("chloe"), change["id"], res["revision_no"])
    assert exc.value.code == "not_resolved"


def test_t33_image_absente_et_inventaire_perime(conn, demo):
    env = demo.env("vitrine", "dev")
    conn.execute("UPDATE resource_grants SET revoked_at = now() WHERE environment_id = %s AND kind = 'image'",
                 (env["id"],))
    _, res = _version(conn, demo)
    assert [b["code"] for b in res["revision"]["block_reasons"]] == ["no_authorized_image"]
    conn.execute("UPDATE inventory_snapshots SET observed_at = now() - interval '2 days'")
    _, res = _version(conn, demo)
    assert [b["code"] for b in res["revision"]["block_reasons"]] == ["inventory_stale"]


def test_t34_d4_jamais_la_production_par_defaut(conn, demo):
    _, res = _version(conn, demo)
    assert res["revision"]["resolved"]["environment"]["slug"] == "dev"
    dev = demo.env("vitrine", "dev")
    conn.execute("UPDATE resource_grants SET revoked_at = now() WHERE environment_id = %s AND kind = 'network'",
                 (dev["id"],))
    _, res = _version(conn, demo)
    rev = res["revision"]
    assert rev["outcome"] == "blocked"
    assert rev["resolved"]["environment"]["slug"] == "dev"
    assert [b["code"] for b in rev["block_reasons"]] == ["no_authorized_network"]
    with pytest.raises(ApiError) as exc:
        _version(conn, demo, environment="prod")
    assert exc.value.code == "production_not_confirmed"
    _, res = _version(conn, demo, environment="prod", confirm_production=True, lifetime_until=None)
    rev = res["revision"]
    assert rev["outcome"] == "resolved"
    assert rev["resolved"]["environment"]["is_production"] is True
    assert rev["resolved"]["approval"] == {"required": True, "distinct_approver": True, "approver_role": "approver",
                                           "production": True}
    assert [i["name"] for i in rev["resolved"]["instances"]] == ["vit-prd-web-01"]
    assert res["summary"]["production"] is True
    assert res["summary"]["approval"].endswith("(circuit de production)")


def test_cli_grants_retrait_et_retablissement(conn, demo, db_url):
    from rackloom import cli

    args = ["--project", "vitrine", "--environment", "dev", "--kind", "network", "--reason", "D4"]
    assert cli.main(["grants", "revoke", *args]) == 0
    conn.commit()
    _, res = _version(conn, demo)
    assert [b["code"] for b in res["revision"]["block_reasons"]] == ["no_authorized_network"]
    assert res["revision"]["resolved"]["environment"]["slug"] == "dev"
    conn.commit()
    assert cli.main(["grants", "restore", *args]) == 0
    conn.commit()
    _, res = _version(conn, demo)
    assert res["revision"]["outcome"] == "resolved"
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE action LIKE 'grant.%' ORDER BY id").fetchall()]
    assert actions == ["grant.revoke", "grant.restore"]


def test_d13_plusieurs_environnements_hors_production(conn, demo):
    project = demo.project("vitrine")
    env_id = conn.execute(
        "INSERT INTO environments (id, project_id, slug, name, kind, is_production, plan_ttl_seconds, "
        "requires_distinct_approver) VALUES (gen_random_uuid(), %s, 'dev2', 'dev 2', 'dev', false, 3600, true) "
        "RETURNING id", (project["id"],)).fetchone()["id"]
    conn.execute("INSERT INTO resource_grants (id, project_id, environment_id, kind, stable_id) "
                 "VALUES (gen_random_uuid(), %s, %s, 'service_template', 'ubuntu-web')", (project["id"], env_id))
    _, res = _version(conn, demo)
    rev = res["revision"]
    assert rev["outcome"] == "incomplete"
    question = rev["resolved"]["missing"][0]
    assert question["field"] == "/environment"
    assert question["options"] == ["dev", "dev2"]


def test_v07_suggestion_non_confirmee(conn, demo):
    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "Test")
    body = {"need": need(environment="dev"), "base_version_no": 0,
            "provenance": {"/environment": {"source": "suggestion"}}}
    with pytest.raises(ApiError) as exc:
        service.add_version(conn, p, change["id"], body)
    assert exc.value.code == "unconfirmed_suggestion"
    body = {"need": need(size_tier="standard"), "base_version_no": 0,
            "provenance": {"/inputs/size_tier": {"source": "suggestion"}}}
    res = service.add_version(conn, p, change["id"], body)
    assert res["unconfirmed_suggestions"] == ["/inputs/size_tier"]
    with pytest.raises(ApiError) as exc:
        service.submit(conn, p, change["id"], res["revision_no"])
    assert exc.value.code == "unconfirmed_suggestion"


def test_noms_reserves_puis_liberes(conn, demo):
    p = demo.principal("alice")
    a, ra = _version(conn, demo)
    b, rb = _version(conn, demo)
    service.submit(conn, p, a["id"], ra["revision_no"])
    # La révision de b a été calculée avant la réservation de a : conflit explicite, pas de renommage silencieux.
    with pytest.raises(ApiError) as exc:
        service.submit(conn, p, b["id"], rb["revision_no"])
    assert exc.value.code == "name_unavailable"
    rb = service.add_version(conn, p, b["id"], {"need": need(), "base_version_no": 1})
    assert [i["name"] for i in rb["revision"]["resolved"]["instances"]] == ["vit-dev-web-02"]
    service.cancel(conn, p, a["id"])
    released = conn.execute("SELECT released_at FROM name_reservations WHERE change_id = %s",
                            (a["id"],)).fetchone()
    assert released["released_at"] is not None
    _, rc = _version(conn, demo)
    assert [i["name"] for i in rc["revision"]["resolved"]["instances"]] == ["vit-dev-web-01"]


def test_t38_duplication_nouvelle_resolution_versions_courantes(conn, demo):
    p = demo.principal("alice")
    original, res = _version(conn, demo)
    assert res["revision"]["resolved"]["template"]["version"] == "1.0.0"
    registry.publish_action(conn, demo.principal("pub"), "ubuntu-web", "1.1.0", "publish", "Nouvelle version")
    dup = service.duplicate(conn, p, original["id"])
    assert dup["change"]["id"] != original["id"]
    assert dup["revision"]["resolved"]["template"]["version"] == "1.1.0"
    assert dup["revision"]["resolved"]["instances"][0]["memory_mb"] == 3072
    assert dup["revision"]["resolution_digest"] != res["revision"]["resolution_digest"]
    first = conn.execute("SELECT resolved FROM request_revisions WHERE change_id = %s AND revision_no = 1",
                         (original["id"],)).fetchone()
    assert first["resolved"]["template"]["version"] == "1.0.0"


def test_t39_droits_de_publication_distincts(conn, demo):
    jobs_before = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
    for who in ("alice", "bruno", "admin"):
        with pytest.raises(ApiError) as exc:
            registry.publish_action(conn, demo.principal(who), "ubuntu-web", "1.1.0", "publish", "essai")
        assert exc.value.status == 403
    with pytest.raises(ApiError) as exc:
        registry.publish_action(conn, demo.principal("pub"), "ubuntu-web", "1.1.0", "publish", "  ")
    assert exc.value.code == "reason_required"
    out = registry.publish_action(conn, demo.principal("pub"), "ubuntu-web", "1.1.0", "publish", "Version 1.1.0")
    assert out["status"] == "published"
    assert out["impact"]["projects"] == ["compta", "vitrine"]
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == jobs_before
    events = [r["action"] for r in registry.template_history(conn, "ubuntu-web")]
    assert events.count("published") == 2
    # Le publicateur n'a aucun rôle projet : il ne voit ni ne crée de demande.
    with pytest.raises(ApiError) as exc:
        service.create_change(conn, demo.principal("pub"), demo.project("vitrine"), "Essai")
    assert exc.value.status == 404


def test_d7_admin_ne_lit_pas_les_projets(conn, demo):
    change, _ = _version(conn, demo)
    with pytest.raises(ApiError) as exc:
        service.get_change(conn, demo.principal("admin"), change["id"])
    assert exc.value.status == 404
    assert service.get_change(conn, demo.principal("david"), change["id"])["id"] == change["id"]
