"""F38 : matrice de compatibilité active (R135).

Provider marqué incompatible → diagnostic avant déploiement.
Combinaison absente → non vérifié (politique continue par défaut).
Niveau de preuve : testé localement.
"""

from __future__ import annotations

from rackloom import admission, compatibility
from rackloom.policy import BUILTIN_VERSION

PROVIDER = "registry.terraform.io/hashicorp/local"
VERSION = "2.5.3"


def test_f38_incompatible_bloque(conn, demo):
    compatibility.ensure_seeded(conn)
    try:
        compatibility.set_status(
            conn,
            kind="terraform_provider",
            name=PROVIDER,
            version=VERSION,
            status="incompatible",
            note="version de test marquée incompatible",
        )
        resolution = {
            "terraform": {"module": "lab-vm", "version": "1.0.0"},
            "components": [],
        }
        # used_versions lit le manifest du module → providers locaux.
        result = compatibility.check(conn, resolution)
        assert result["status"] == "incompatible"
        code, message = compatibility.refusal(result)
        assert code == "compatibility_incompatible"
        assert "incompatible" in message.lower() or "incompatible" in code

        ctx = admission.Context(
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
            environment=demo.env("vitrine", "dev"),
            execution=None,
            approval=None,
            plan=None,
            revision={"resolved": resolution},
        )
        fn = next(r[4] for r in admission.REGISTRY if r[1] == "R135")
        verdict = fn(ctx)
        assert not verdict.ok
        assert verdict.code == "compatibility_incompatible"
        assert BUILTIN_VERSION == "builtin@10"
    finally:
        compatibility.set_status(conn, kind="terraform_provider", name=PROVIDER, version=VERSION, status="verified")
        conn.commit()


def test_f38_absent_non_verifie(conn):
    compatibility.ensure_seeded(conn)
    result = compatibility.check(
        conn,
        {
            "terraform": {"module": "lab-vm", "version": "9.9.9"},
            "components": [{"slug": "inconnu", "version": "0.0.1"}],
        },
    )
    assert result["status"] == "unverified"
    assert any(i["status"] == "unverified" for i in result["items"])
    assert compatibility.unverified_policy() == "continue"


def test_f38_ok_apres_seed(conn):
    compatibility.ensure_seeded(conn)
    # Remet le provider en verified (autres tests peuvent l'avoir marqué).
    compatibility.set_status(conn, kind="terraform_provider", name=PROVIDER, version=VERSION, status="verified")
    result = compatibility.check(
        conn,
        {"terraform": {"module": "lab-vm", "version": "1.0.0"}, "components": []},
    )
    assert result["status"] in ("ok", "unverified")  # composants absents OK selon politique
    if result["status"] == "ok":
        assert all(i["status"] != "incompatible" for i in result["items"])
