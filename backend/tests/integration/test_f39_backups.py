"""F39 : vérification des sauvegardes VM (R115).

Source fictive ; snapshot vSphere ≠ preuve ; périmée / absente / injoignable.
Niveau de preuve : testé localement (pas de produit de sauvegarde réel).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rackloom import admission, backups
from rackloom.policy import BUILTIN_VERSION


def _ctx(demo, conn, resolution):
    return admission.Context(
        conn=conn,
        job={
            "kind": "tf_apply",
            "class": "write",
            "id": demo.user("alice")["id"],
            "cancel_requested_at": None,
            "stack_id": None,
            "payload": {},
        },
        change={"id": demo.user("alice")["id"], "project_id": demo.project("vitrine")["id"]},
        environment=demo.env("vitrine", "prod"),
        execution=None,
        approval=None,
        plan=None,
        revision={"resolved": resolution},
    )


def _sensitive(name: str = "vit-prd-web-01") -> dict:
    return {
        "backup": {"policy": "daily-14d", "max_age_hours": 24, "required": True},
        "instances": [{"name": name}],
    }


def test_f39_snapshot_refuse(conn, demo):
    backups.set_proof(
        conn,
        "vit-prd-web-01",
        status="ok",
        proof_kind=backups.PROOF_SNAPSHOT,
        last_backup_at=datetime.now(UTC),
    )
    result = backups.check(conn, backups.requirements_from(_sensitive()))
    assert result["status"] == "snapshot_rejected"
    code, message = backups.refusal(result)
    assert code == "backup_snapshot_not_proof"
    assert "snapshot" in message.lower()

    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R115")
    verdict = fn(_ctx(demo, conn, _sensitive()))
    assert not verdict.ok
    assert verdict.code == "backup_snapshot_not_proof"
    assert BUILTIN_VERSION == "builtin@9"


def test_f39_perimee_et_absente(conn, demo):
    backups.clear_proof(conn, "vit-prd-web-01")
    result = backups.check(conn, backups.requirements_from(_sensitive()))
    assert result["status"] == "missing"

    backups.set_proof(
        conn,
        "vit-prd-web-01",
        status="ok",
        proof_kind=backups.PROOF_APPROVED,
        last_backup_at=datetime.now(UTC) - timedelta(hours=48),
    )
    result = backups.check(conn, backups.requirements_from(_sensitive()))
    assert result["status"] == "stale"
    code, _ = backups.refusal(result)
    assert code == "backup_stale"


def test_f39_injoignable(conn):
    backups.set_proof(
        conn,
        "vit-prd-web-01",
        status="unreachable",
        proof_kind=backups.PROOF_APPROVED,
        reachable=False,
    )
    result = backups.check(conn, backups.requirements_from(_sensitive()))
    assert result["status"] == "unverified"
    assert backups.unverified_policy() == "block"


def test_f39_ok_preuve_approuvee(conn, demo):
    backups.set_proof(
        conn,
        "vit-prd-web-01",
        status="ok",
        proof_kind=backups.PROOF_APPROVED,
        last_backup_at=datetime.now(UTC),
        note="observation fictive",
    )
    result = backups.check(conn, backups.requirements_from(_sensitive()))
    assert result["status"] == "ok"
    assert result["simulated"] is True
    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R115")
    assert fn(_ctx(demo, conn, _sensitive())).ok


def test_f39_sans_politique_passe(conn, demo):
    """dev backup: none → R115 sans effet."""
    resolution = {"backup": {"policy": "none", "required": False}, "instances": [{"name": "x"}]}
    assert backups.requirements_from(resolution) is None
    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R115")
    assert fn(_ctx(demo, conn, resolution)).ok
