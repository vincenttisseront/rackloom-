"""F19 : patch par vagues.

Sélection d'hôtes enrôlés, pilote, contrôles avant/après, blocage sans retour arrière.
Niveau de preuve : testé localement (patch simulé, sondes via observations probe).
Labo réel (3 nœuds) : non (D2 / L2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from rackloom.db import Jsonb
from rackloom.ids import uuid7

API = "/api/v1"


def _plant_enrolled(conn, demo, *, names: list[str], dep_name_prefix: str = "f19"):
    """Déploiement actif + N hôtes enrôlés (même environnement)."""
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id = uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, %s, 'succeeded', %s)",
        (change_id, project["id"], env["id"], f"{dep_name_prefix}-seed", alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', %s)",
        (stack_id, f"{dep_name_prefix}-{deployment_id.hex[:8]}", project["id"], env["id"], dep_name_prefix),
    )
    inst = [{"name": n, "size_tier": "test_leger", "vcpu": 2, "memory_mb": 2048} for n in names]
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, service_status, service_reason, "
        "service_checked_at, service_checks, instances) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', 'ready', %s, %s, %s, %s)",
        (
            deployment_id,
            project["id"],
            env["id"],
            tpl["id"],
            stack_id,
            change_id,
            alice["id"],
            "tous les contrôles de santé ont réussi",
            datetime.now(UTC),
            Jsonb(
                [
                    {
                        "host": n,
                        "state": "checked",
                        "checks": [{"type": "http_probe", "ok": True, "evidence": "HTTP 200"}],
                    }
                    for n in names
                ]
            ),
            Jsonb(inst),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    host_ids: list[UUID] = []
    for i, name in enumerate(names):
        hid = uuid7()
        host_ids.append(hid)
        conn.execute(
            "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, "
            "change_id, stack_id, vm_stable_id, name, address, enrolled_at, deployment_state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), 'verified')",
            (
                hid,
                project["id"],
                env["id"],
                deployment_id,
                tpl["id"],
                change_id,
                stack_id,
                f"vm-{dep_name_prefix}-{i}",
                name,
                f"10.0.0.{10 + i}",
            ),
        )
        # Sonde saine par défaut.
        conn.execute(
            "INSERT INTO host_observations (host_id, project_id, kind, data, status, source, collected_at) "
            "VALUES (%s, %s, 'probe', %s, 'ok', 'test', now())",
            (hid, project["id"], Jsonb({"evidence": "HTTP 200", "ok": True})),
        )
    conn.commit()
    return deployment_id, host_ids


def _approve_campaign(api, worker, campaign: dict) -> dict:
    alice = api("alice")
    change_id = campaign["change_id"]
    change = alice.get(f"{API}/changes/{change_id}").json()
    sub = alice.post(
        f"{API}/changes/{change_id}/submit",
        json={"revision_no": change["revision"]["revision_no"]},
    )
    assert sub.status_code == 200, sub.text
    view = alice.get(f"{API}/changes/{change_id}").json()
    assert view["state"] == "plan_ready"
    assert view["plan"] is not None
    assert view["plan"].get("interruption") is not None
    bruno = api("bruno")
    plan = view["plan"]
    decision = bruno.post(
        f"{API}/changes/{change_id}/approvals",
        json={"decision": "approved", "plan_id": plan["id"], "bundle_digest": plan["bundle_digest"]},
    )
    assert decision.status_code == 200, decision.text
    worker.drain(kinds=["host_patch"])
    return alice.get(f"{API}/patch-campaigns/{campaign['id']}").json()


def test_f19_pilote_degrade_bloque_vague2_sans_ecriture(api, conn, demo, worker, monkeypatch):
    """Trois nœuds : dégradation sur le pilote → vague 2 bloquée, aucune écriture sur elle."""
    from rackloom import patching

    names = ["vit-dev-web-p1", "vit-dev-web-p2", "vit-dev-web-p3"]
    _dep_id, host_ids = _plant_enrolled(conn, demo, names=names, dep_name_prefix="f19a")
    alice = api("alice")

    candidates = alice.get(f"{API}/projects/vitrine/patch-hosts?environment=dev")
    assert candidates.status_code == 200
    cand_names = {c["name"] for c in candidates.json()}
    assert set(names) <= cand_names

    created = alice.post(
        f"{API}/projects/vitrine/patch-campaigns",
        json={
            "title": "Patch sécurité labo",
            "host_ids": [str(h) for h in host_ids],
            "pilot_size": 1,
            "wave_size": 2,
            "reboot_policy": "if_required_in_window",
        },
    )
    assert created.status_code == 200, created.text
    campaign = created.json()
    assert campaign["simulated"] is True
    assert len(campaign["waves"]) == 2
    assert campaign["waves"][0] == [names[0]]
    assert set(campaign["waves"][1]) == set(names[1:])

    # Pré-contrôle OK, post-contrôle dégradé sur le pilote (après écriture simulée).
    calls: dict[str, int] = {}
    real = patching.probe_status

    def flaky(conn, host_id):  # noqa: ANN001
        key = str(host_id)
        calls[key] = calls.get(key, 0) + 1
        if host_id == host_ids[0] and calls[key] >= 2:
            return {
                "ok": False,
                "status": "error",
                "evidence": "dégradation provoquée (recette F19)",
                "degraded": True,
            }
        return real(conn, host_id)

    monkeypatch.setattr(patching, "probe_status", flaky)

    final = _approve_campaign(api, worker, campaign)
    assert final["state"] == "wave_blocked"
    by_name = {h["host_name"]: h for h in final["hosts"]}
    assert by_name[names[0]]["state"] == "degraded"
    assert by_name[names[0]]["write_attempted"] is True
    assert by_name[names[1]]["state"] == "wave_blocked"
    assert by_name[names[1]]["write_attempted"] is False
    assert by_name[names[2]]["state"] == "wave_blocked"
    assert by_name[names[2]]["write_attempted"] is False
    assert (
        "retour arrière" in (final.get("block_reason") or "").lower()
        or "bloqu" in (final.get("block_reason") or "").lower()
    )

    change = alice.get(f"{API}/changes/{campaign['change_id']}").json()
    assert change["state"] == "partially_succeeded"


def test_f19_campagne_complete_simulee(api, conn, demo, worker):
    names = ["vit-dev-web-ok1", "vit-dev-web-ok2"]
    _dep_id, host_ids = _plant_enrolled(conn, demo, names=names, dep_name_prefix="f19b")
    alice = api("alice")
    created = alice.post(
        f"{API}/projects/vitrine/patch-campaigns",
        json={"host_ids": [str(h) for h in host_ids], "pilot_size": 1, "wave_size": 1},
    )
    assert created.status_code == 200, created.text
    campaign = created.json()
    final = _approve_campaign(api, worker, campaign)
    assert final["state"] == "succeeded"
    assert all(h["state"] == "post_ok" for h in final["hosts"])
    assert all(h["write_attempted"] for h in final["hosts"])
    change = alice.get(f"{API}/changes/{campaign['change_id']}").json()
    assert change["state"] == "succeeded"


def test_f19_reboot_never_et_hors_perimetre(api, conn, demo, worker):
    names = ["vit-dev-web-rb1"]
    _dep_id, host_ids = _plant_enrolled(conn, demo, names=names, dep_name_prefix="f19c")
    alice = api("alice")
    created = alice.post(
        f"{API}/projects/vitrine/patch-campaigns",
        json={
            "host_ids": [str(h) for h in host_ids],
            "pilot_size": 1,
            "wave_size": 1,
            "reboot_policy": "never",
        },
    )
    assert created.status_code == 200
    final = _approve_campaign(api, worker, created.json())
    assert final["state"] == "succeeded"
    assert final["hosts"][0]["reboot_required"] is True
    assert final["hosts"][0]["reboot_done"] is False

    # Hors périmètre.
    chloe = api("chloe")
    r = chloe.get(f"{API}/projects/vitrine/patch-campaigns")
    assert r.status_code == 404
