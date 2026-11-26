"""F01 : catalogue Git avec promotion.

Dépôt Git local de labo ; commit non épinglé ou non revu refusé ; promotion de 1.1.0 sans
modifier un plan approuvé ni une stack en 1.0.0.
Niveau de preuve : testé localement (PostgreSQL éphémère, Terraform CLI, pas de forge réelle).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import flows
from rackloom import registry
from rackloom.errors import ApiError
from rackloom.registry import git_source

ROOT = Path(__file__).resolve().parents[3]
API = "/api/v1"


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "catalog-git"
    repo.mkdir()
    # Catalogue minimal : copie du dépôt appliance (templates + composants + terraform + ansible).
    for name in ("templates", "components", "terraform", "ansible"):
        shutil.copytree(ROOT / "catalog" / name, repo / name)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "lab@demo.invalid"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Lab"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "catalog lab"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_f01_commit_non_epingle_refuse(tmp_path):
    repo, sha = _git_repo(tmp_path)
    with pytest.raises(ApiError) as exc:
        git_source.resolve_commit(repo, None)
    assert exc.value.code == "unpinned"
    with pytest.raises(ApiError) as exc2:
        git_source.resolve_commit(repo, sha[:12])
    assert exc2.value.code == "unpinned"
    assert git_source.resolve_commit(repo, sha) == sha
    with pytest.raises(ApiError) as exc3:
        git_source.resolve_local_repo("https://example.invalid/repo.git")
    assert exc3.value.code == "remote_git_forbidden"


@pytest.mark.terraform
def test_f01_import_revue_promotion_plan_inchange(conn, demo, worker, worker_env, tmp_path, api):
    # Plan A en 1.0.0 déjà promue (catalogue appliance) ; approbation avant promotion de 1.1.0.
    c, plan = _plan(conn, demo, worker)
    assert plan["bundle"]["template"]["version"] == "1.0.0"
    res = flows.approve(conn, demo, c["id"], plan)
    digest_a = plan["bundle_digest"]

    # Prépare une 1.2.0 uniquement présente dans le dépôt Git (évite le conflit avec 1.1.0 filesystem).
    repo, _ = _git_repo(tmp_path)
    src = repo / "templates" / "ubuntu-web" / "1.1.0"
    dst = repo / "templates" / "ubuntu-web" / "1.2.0"
    shutil.copytree(src, dst)
    tpl = (dst / "template.yaml").read_text(encoding="utf-8")
    tpl = tpl.replace("version: 1.1.0", "version: 1.2.0", 1)
    (dst / "template.yaml").write_text(tpl, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ubuntu-web 1.2.0"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    imported = registry.load_from_git(conn, repo, sha)
    conn.commit()
    pin_id = imported["pin"]["id"]
    assert imported["pin"]["reviewed"] is False
    assert imported["counts"]["templates"] >= 1

    row = registry.template(conn, "ubuntu-web", "1.2.0")
    assert row is not None and row["status"] == "validated"
    assert str(row["source_pin_id"]) == pin_id

    # Publication sans revue → refus.
    with pytest.raises(ApiError) as exc:
        registry.publish_action(conn, demo.principal("pub"), "ubuntu-web", "1.2.0", "publish", "trop tôt")
    assert exc.value.code == "unreviewed_commit"
    conn.rollback()

    # Revue puis publication : pas de promotion auto (source Git).
    reviewed = api("pub").post(f"{API}/admin/catalog/pins/{pin_id}/review", json={"reason": "revue labo F01"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviewed"] is True

    pub = api("pub").post(
        f"{API}/admin/templates/ubuntu-web/versions/1.2.0/publish",
        json={"reason": "publication après revue Git"},
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published"
    assert registry.list_promotions(conn, row["id"]) == []
    assert registry.latest_promoted(conn, "ubuntu-web", "dev")["version"] == "1.0.0"

    # Nouvelle demande : toujours 1.0.0 tant que 1.2.0 n'est pas promue.
    c2, plan2 = _plan(conn, demo, worker)
    assert plan2["bundle"]["template"]["version"] == "1.0.0"

    # Promotion → nouvelles demandes en 1.2.0 ; plan A et stack inchangés.
    promo = api("pub").post(
        f"{API}/admin/templates/ubuntu-web/versions/1.2.0/promote",
        json={"environment_kind": "dev", "reason": "promotion labo F01"},
    )
    assert promo.status_code == 200, promo.text
    assert promo.json()["environment_kind"] == "dev"
    assert "ne modifie ni plan" in promo.json()["note"]

    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "succeeded", job
    assert flows.latest_plan(conn, c["id"])["bundle_digest"] == digest_a
    dep = flows.deployment(conn, c["deployment_id"])
    assert registry.template_by_id(conn, dep["template_version_id"])["version"] == "1.0.0"
    assert list((worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))

    c3, plan3 = _plan(conn, demo, worker)
    assert plan3["bundle"]["template"]["version"] == "1.2.0"


def _plan(conn, demo, worker):
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    return c, flows.latest_plan(conn, c["id"])
