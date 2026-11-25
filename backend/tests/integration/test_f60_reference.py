"""F60 : template depuis un serveur de référence (observations fictives explicites).

Niveau de preuve : testé localement. Collecteur réel indisponible (D21) : fixture
`ubuntu-nginx-dedicated-mount`. Aucun job d'écriture. Canaris d'hôte absents du brouillon.
"""

from __future__ import annotations

from rackloom.studio import reference as refmod

API = "/api/v1"
BASE = {"slug": "ubuntu-web", "version": "1.0.0"}
FIXTURE = {"fixture": refmod.FIXTURE_ID}


def _create(ines, slug: str = "ubuntu-web-ref"):
    created = ines.post(
        f"{API}/studio/drafts",
        json={"slug": slug, "base": BASE, "reference": FIXTURE},
    )
    assert created.status_code == 200, created.text
    return created.json()["draft"]


def _decide(ines, draft: dict, item_id: str, decision: str) -> dict:
    r = ines.post(
        f"{API}/studio/drafts/{draft['id']}/reference/{item_id}",
        json={"row_version": draft["row_version"], "decision": decision},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _pending(draft: dict, category: str | None = None) -> list[dict]:
    ref = draft["reference"]
    assert ref is not None
    items = []
    for group in ref["categories"]:
        if category and group["id"] != category:
            continue
        items.extend([i for i in group["items"] if i["status"] == "pending"])
    return items


def test_sources_listent_fixture_et_collecteurs(api):
    ines = api("ines")
    r = ines.get(f"{API}/studio/reference-sources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(f["id"] == refmod.FIXTURE_ID and f["simulated"] for f in body["fixtures"])
    assert {c["slug"] for c in body["collectors"]} >= {"os_facts", "mounts", "packages", "services"}
    # Inès (auteur global) n'a pas de projet : aucun hôte réel listé.
    assert body["hosts"] == []


def test_brouillon_depuis_fixture_categories_et_layout(api, conn):
    ines = api("ines")
    draft = _create(ines)
    assert draft["origin"] == "reference_host"
    ref = draft["reference"]
    assert ref["source"]["simulated"] is True
    assert ref["host_unchanged"] is True
    assert ref["layout_warning"] and "/var/www" in ref["layout_warning"]
    cats = {c["id"]: c for c in ref["categories"]}
    assert cats["standardize"]["items"]
    assert cats["host_specific"]["items"]
    assert cats["confirm"]["items"]
    # Identifiants / canaris absents de la vue et du contenu.
    blob = draft["design"]["title"] + draft["design"]["description"] + str(draft["reference"])
    for token in refmod.leak_tokens():
        assert token not in blob
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_standardiser_puis_soumettre_sans_job_ni_fuite(api, conn):
    ines = api("ines")
    draft = _create(ines, slug="ubuntu-web-from-ref")
    jobs_before = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]

    # Composants d'abord, puis réglages, puis « à confirmer » (extensions).
    for _ in range(24):
        pending_std = _pending(draft, "standardize")
        comps = [i for i in pending_std if i.get("path") and "/config/" not in i["path"]]
        configs = [i for i in pending_std if i.get("path") and "/config/" in i["path"]]
        nxt = comps or configs or _pending(draft, "confirm")
        if not nxt:
            break
        draft = _decide(ines, draft, nxt[0]["id"], "accepted")

    assert draft["allowed_actions"]["submit"] is None, draft["problems"]
    # Nginx présent ; listen_port observé (8080) appliqué.
    nginx = next(c for c in draft["design"]["components"] if c["component"] == "nginx")
    port = next(p for p in nginx["parameters"] if p["name"] == "listen_port")
    assert port["value"] == 8080
    assert any(p["source"] == "reference_host" for p in draft["provenance"])
    assert draft["extensions"], "réglages non supportés → extensions"
    for token in refmod.leak_tokens():
        assert token not in str(draft["design"])
        assert token not in str(draft["provenance"])
        assert token not in str(draft.get("extensions"))

    submitted = ines.post(
        f"{API}/studio/drafts/{draft['id']}/submit",
        json={"row_version": draft["row_version"], "digest": draft["digest"]},
    )
    assert submitted.status_code == 200, submitted.text
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == jobs_before
    # Toujours non publié automatiquement.
    assert submitted.json()["status"] == "in_review"


def test_propre_a_hote_non_decidable(api):
    ines = api("ines")
    draft = _create(ines, slug="ubuntu-web-host-only")
    host_items = _pending(draft, "host_specific") or [
        i for g in draft["reference"]["categories"] if g["id"] == "host_specific" for i in g["items"]
    ]
    assert host_items
    r = ines.post(
        f"{API}/studio/drafts/{draft['id']}/reference/{host_items[0]['id']}",
        json={"row_version": draft["row_version"], "decision": "accepted"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "not_decidable"


def test_hote_hors_perimetre_404(api):
    """Un host_id hors périmètre ne révèle rien (404), même pour l'auteur de templates."""
    from rackloom.ids import uuid7

    ines = api("ines")
    r = ines.post(
        f"{API}/studio/drafts",
        json={"slug": "ghost-ref", "base": BASE, "reference": {"host_id": str(uuid7())}},
    )
    assert r.status_code == 404


def test_hote_reel_refuse_collector_unavailable(api, conn, demo):
    """Hôte enrôlé dans le périmètre : collecteur d'atelier indisponible → 422, aucun job."""
    from datetime import UTC, datetime

    from rackloom.ids import uuid7

    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute("SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND version = '1.0.0'").fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id, host_id = uuid7(), uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, %s, %s, 'f60-host', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f60')",
        (stack_id, f"f60-{host_id.hex[:8]}", project["id"], env["id"]),
    )
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active')",
        (deployment_id, project["id"], env["id"], tpl["id"], stack_id, change_id, alice["id"]),
    )
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, address, host_key_fingerprint, enrolled_at, deployment_state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'vm-f60', 'ref-lab-f60', '127.0.0.1', 'SHA256:f60', %s, 'ready')",
        (
            host_id,
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            datetime.now(UTC),
        ),
    )
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (%s, %s, 'operator', %s)",
        (uuid7(), demo.user("ines")["id"], project["id"]),
    )
    conn.commit()
    jobs = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
    ines = api("ines")
    r = ines.post(
        f"{API}/studio/drafts",
        json={"slug": "from-real-host", "base": BASE, "reference": {"host_id": str(host_id)}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "collector_unavailable"
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == jobs


def test_assistant_contexte_sans_canari(api, conn):
    from rackloom import studio

    ines = api("ines")
    draft_view = _create(ines, slug="ubuntu-web-assist-ref")
    row = conn.execute("SELECT * FROM template_drafts WHERE id = %s", (draft_view["id"],)).fetchone()
    base = studio._base(conn, row)
    ctx = studio._assistant_context(conn, row, base)
    refmod.assert_no_host_leaks(ctx)
