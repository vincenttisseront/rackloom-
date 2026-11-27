"""F07 : fabrique d'images — provenance, versions, tests, promotion, dépréciation.

Une image vSphere (point de départ inventorié) n'est pas un template Rackloom (ADR-0009).
Ce module gère le pipeline de fabrication : création d'une version, build, tests, promotion
vers un kind d'environnement du catalogue, dépréciation sans retrait tant que nécessaire.

Recette : une image dont les tests ont échoué ne peut pas être promue vers `prod`.
L'outil réel de fabrication est ouvert (D11) ; le connecteur `fake` suffit aux recettes locales.
Publier / promouvoir une image ne crée aucun job et ne modifie ni plan ni déploiement.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

import psycopg.errors

from .. import audit
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_publisher
from ..ids import uuid7
from . import fake as factory_fake

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
ENV_KINDS = ("dev", "test", "preprod", "prod")
SOURCE_KINDS = ("iso_digest", "git_commit", "artifact_digest")


def _digest64(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise ApiError(422, "reason_required", "un motif est obligatoire", field="/reason")
    return reason.strip()


def _view(conn: Conn, build_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM image_builds WHERE id = %s", (build_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version d'image introuvable")
    tests = list(
        conn.execute(
            "SELECT name, outcome, detail, simulated, ran_at FROM image_build_tests "
            "WHERE image_build_id = %s ORDER BY name",
            (build_id,),
        ).fetchall()
    )
    promotions = list(
        conn.execute(
            "SELECT environment_kind, promoted_at, reason, promoted_by FROM image_build_promotions "
            "WHERE image_build_id = %s ORDER BY environment_kind",
            (build_id,),
        ).fetchall()
    )
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "status": row["status"],
        "source_kind": row["source_kind"],
        "source_digest": row["source_digest"],
        "recipe_digest": row["recipe_digest"],
        "builder": row["builder"],
        "os": row["os"],
        "layout_id": row["layout_id"],
        "attributes": row["attributes"] or {},
        "image_digest": row["image_digest"],
        "stable_id": row["stable_id"],
        "simulated": row["simulated"],
        "created_at": row["created_at"],
        "built_at": row["built_at"],
        "tested_at": row["tested_at"],
        "deprecated_at": row["deprecated_at"],
        "deprecation_reason": row["deprecation_reason"],
        "tests": [
            {
                "name": t["name"],
                "outcome": t["outcome"],
                "detail": t["detail"],
                "simulated": t["simulated"],
                "ran_at": t["ran_at"],
            }
            for t in tests
        ],
        "promotions": [
            {
                "environment_kind": p["environment_kind"],
                "promoted_at": p["promoted_at"],
                "reason": p["reason"],
                "promoted_by": str(p["promoted_by"]) if p["promoted_by"] else None,
            }
            for p in promotions
        ],
        "badge": "Simulé" if row["simulated"] else None,
        "notice": (
            "Fabrique fictive : aucun outil Packer/vSphere réel (D11). "
            "Déprécier ne retire pas l'image tant qu'elle reste nécessaire."
        ),
    }


def get(conn: Conn, slug: str, version: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id FROM image_builds WHERE slug = %s AND version = %s", (slug, version)).fetchone()
    return _view(conn, row["id"]) if row else None


def list_builds(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT slug, title FROM image_builds GROUP BY slug, title ORDER BY slug").fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        versions = conn.execute(
            "SELECT id, version, status, image_digest, simulated, deprecated_at "
            "FROM image_builds WHERE slug = %s ORDER BY version",
            (r["slug"],),
        ).fetchall()
        out.append(
            {
                "slug": r["slug"],
                "title": r["title"],
                "versions": [
                    {
                        "version": v["version"],
                        "status": v["status"],
                        "image_digest": v["image_digest"],
                        "simulated": v["simulated"],
                        "deprecated": v["deprecated_at"] is not None or v["status"] == "deprecated",
                    }
                    for v in versions
                ],
            }
        )
    return out


def create(
    conn: Conn,
    principal: Principal,
    *,
    slug: str,
    version: str,
    title: str,
    source_kind: str,
    source_digest: str,
    os: str,
    layout_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enregistre une version d'image en `draft`. Ne crée aucun job."""
    require_publisher(principal)
    if not SLUG_RE.match(slug):
        raise ApiError(422, "invalid_slug", "slug d'image invalide", field="/slug")
    if not VERSION_RE.match(version):
        raise ApiError(422, "invalid_version", "version semver attendue (x.y.z)", field="/version")
    if source_kind not in SOURCE_KINDS:
        raise ApiError(422, "invalid_source_kind", f"source_kind attendu : {', '.join(SOURCE_KINDS)}")
    if source_kind == "git_commit":
        if not re.fullmatch(r"[0-9a-f]{40}", source_digest):
            raise ApiError(422, "invalid_source_digest", "commit SHA-1 (40 hex) attendu", field="/source_digest")
    elif not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise ApiError(422, "invalid_source_digest", "empreinte SHA-256 (64 hex) attendue", field="/source_digest")
    if not title or not title.strip():
        raise ApiError(422, "title_required", "un titre est obligatoire", field="/title")
    if not os or not layout_id:
        raise ApiError(422, "attributes_required", "os et layout_id sont obligatoires")

    attrs = dict(attributes or {})
    attrs.setdefault("os", os)
    attrs.setdefault("layout_id", layout_id)
    recipe = _digest64(f"{slug}|{version}|{source_kind}|{source_digest}|{os}|{layout_id}|{sorted(attrs.items())}")
    bid = uuid7()
    try:
        conn.execute(
            "INSERT INTO image_builds (id, slug, version, title, status, source_kind, source_digest, "
            "recipe_digest, builder, os, layout_id, attributes, simulated, created_by) "
            "VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, 'fake', %s, %s, %s, true, %s)",
            (
                bid,
                slug,
                version,
                title.strip(),
                source_kind,
                source_digest,
                recipe,
                os,
                layout_id,
                Jsonb(attrs),
                principal.user_id,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ApiError(409, "already_exists", f"image {slug}@{version} déjà enregistrée") from exc
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="factory.image.create",
        outcome="ok",
        targets={"image": slug},
        versions={"image": version, "recipe_digest": recipe},
    )
    return _view(conn, bid)


def build(conn: Conn, principal: Principal, slug: str, version: str) -> dict[str, Any]:
    """Exécute le constructeur fictif. Passe `draft` → `built`."""
    require_publisher(principal)
    row = conn.execute(
        "SELECT * FROM image_builds WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version d'image introuvable")
    if row["status"] != "draft":
        raise ApiError(409, "invalid_build_state", f"build impossible depuis « {row['status']} »")
    image_digest = factory_fake.build_digest(
        slug=row["slug"],
        version=row["version"],
        source_digest=row["source_digest"],
        recipe_digest=row["recipe_digest"],
        os=row["os"],
        layout_id=row["layout_id"],
    )
    stable = factory_fake.stable_id_for(slug=row["slug"], version=row["version"])
    conn.execute(
        "UPDATE image_builds SET status = 'built', image_digest = %s, stable_id = %s, built_at = now() WHERE id = %s",
        (image_digest, stable, row["id"]),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="factory.image.build",
        outcome="ok",
        targets={"image": slug, "stable_id": stable},
        versions={"image": version, "image_digest": image_digest},
        policy_decision={"builder": "fake", "simulated": True},
    )
    return _view(conn, row["id"])


def run_tests(
    conn: Conn,
    principal: Principal,
    slug: str,
    version: str,
    *,
    fail: bool = False,
) -> dict[str, Any]:
    """Enregistre les résultats de tests. `built` → `ready` ou `failed`."""
    require_publisher(principal)
    row = conn.execute(
        "SELECT * FROM image_builds WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version d'image introuvable")
    if row["status"] not in ("built", "ready", "failed"):
        raise ApiError(409, "invalid_build_state", f"tests impossibles depuis « {row['status']} »")

    results = factory_fake.run_tests(fail=fail)
    for t in results:
        # Upsert : pas de DELETE (rôle API sans ce droit) ; rejeu remplace le résultat.
        conn.execute(
            "INSERT INTO image_build_tests (id, image_build_id, name, outcome, detail, simulated) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (image_build_id, name) DO UPDATE SET "
            "outcome = EXCLUDED.outcome, detail = EXCLUDED.detail, "
            "simulated = EXCLUDED.simulated, ran_at = now()",
            (uuid7(), row["id"], t["name"], t["outcome"], t["detail"], t["simulated"]),
        )
    passed = all(t["outcome"] == "passed" for t in results)
    new_status = "ready" if passed else "failed"
    conn.execute(
        "UPDATE image_builds SET status = %s, tested_at = now() WHERE id = %s",
        (new_status, row["id"]),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="factory.image.test",
        outcome="ok" if passed else "failed",
        targets={"image": slug},
        versions={"image": version, "status": new_status},
        policy_decision={"fail_injected": fail, "simulated": True},
    )
    return _view(conn, row["id"])


def promote(
    conn: Conn,
    principal: Principal,
    slug: str,
    version: str,
    *,
    environment_kind: str,
    reason: str,
) -> dict[str, Any]:
    """Promeut une image prête vers un kind du catalogue. Aucun job."""
    require_publisher(principal)
    reason = _require_reason(reason)
    if environment_kind not in ENV_KINDS:
        raise ApiError(422, "invalid_environment_kind", f"kind attendu : {', '.join(ENV_KINDS)}")
    row = conn.execute(
        "SELECT * FROM image_builds WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version d'image introuvable")
    if row["status"] == "deprecated":
        raise ApiError(409, "deprecated", "une image dépréciée ne peut plus être promue")
    if row["status"] == "failed":
        raise ApiError(
            409,
            "image_tests_failed",
            "cette image a échoué à ses tests : promotion vers le catalogue refusée"
            + (" (y compris production)" if environment_kind == "prod" else ""),
        )
    if row["status"] != "ready":
        raise ApiError(
            409,
            "not_ready",
            "seule une image dont les tests ont réussi peut être promue",
        )
    # Garde explicite prod (recette F07) — déjà couverte par status, mais motif dédié.
    if environment_kind == "prod":
        failed = conn.execute(
            "SELECT 1 FROM image_build_tests WHERE image_build_id = %s AND outcome = 'failed'",
            (row["id"],),
        ).fetchone()
        if failed:
            raise ApiError(
                409,
                "image_tests_failed",
                "un template/image qui échoue à ses tests ne peut pas être promu dans le catalogue de production",
            )

    existing = conn.execute(
        "SELECT 1 FROM image_build_promotions WHERE image_build_id = %s AND environment_kind = %s",
        (row["id"], environment_kind),
    ).fetchone()
    if existing:
        raise ApiError(409, "already_promoted", f"déjà promue vers {environment_kind}")

    conn.execute(
        "INSERT INTO image_build_promotions (id, image_build_id, environment_kind, promoted_by, reason) "
        "VALUES (%s, %s, %s, %s, %s)",
        (uuid7(), row["id"], environment_kind, principal.user_id, reason),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="factory.image.promote",
        outcome="ok",
        targets={"image": slug, "environment_kind": environment_kind},
        versions={"image": version, "image_digest": row["image_digest"]},
        policy_decision={"reason": reason},
    )
    view = _view(conn, row["id"])
    view["note"] = "La promotion ne modifie ni plan approuvé ni déploiement existant."
    return view


def deprecate(
    conn: Conn,
    principal: Principal,
    slug: str,
    version: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Déprécie sans retirer : l'image reste listée et ses promotions conservées."""
    require_publisher(principal)
    reason = _require_reason(reason)
    row = conn.execute(
        "SELECT * FROM image_builds WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version d'image introuvable")
    if row["status"] == "deprecated":
        return _view(conn, row["id"])
    if row["status"] not in ("ready", "failed", "built"):
        raise ApiError(409, "invalid_build_state", f"dépréciation impossible depuis « {row['status']} »")
    conn.execute(
        "UPDATE image_builds SET status = 'deprecated', deprecated_at = now(), "
        "deprecated_by = %s, deprecation_reason = %s WHERE id = %s",
        (principal.user_id, reason, row["id"]),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="factory.image.deprecate",
        outcome="ok",
        targets={"image": slug},
        versions={"image": version},
        policy_decision={"reason": reason, "retained": True},
    )
    view = _view(conn, row["id"])
    view["note"] = "Image dépréciée mais conservée : aucun retrait tant qu'elle est nécessaire."
    return view


def ensure_seeded(conn: Conn) -> None:
    """Image labo prête et promue (dev) pour la démo — alignée sur le filtre ubuntu-web."""
    existing = conn.execute(
        "SELECT id FROM image_builds WHERE slug = 'ubuntu-2404-std' AND version = '1.0.0'"
    ).fetchone()
    if existing:
        return
    source = _digest64("lab-iso-ubuntu-24.04-demo.invalid")
    recipe = _digest64("ubuntu-2404-std|1.0.0|iso_digest|seed")
    image_digest = factory_fake.build_digest(
        slug="ubuntu-2404-std",
        version="1.0.0",
        source_digest=source,
        recipe_digest=recipe,
        os="ubuntu-24.04",
        layout_id="std-v1",
    )
    stable = "vm-500"  # même stable_id que l'inventaire de labo
    bid = uuid7()
    conn.execute(
        "INSERT INTO image_builds (id, slug, version, title, status, source_kind, source_digest, "
        "recipe_digest, builder, os, layout_id, attributes, image_digest, stable_id, simulated, "
        "built_at, tested_at) VALUES (%s, 'ubuntu-2404-std', '1.0.0', "
        "'Ubuntu 24.04 standard (labo)', 'ready', 'iso_digest', %s, %s, 'fake', "
        "'ubuntu-24.04', 'std-v1', %s, %s, %s, true, now(), now())",
        (
            bid,
            source,
            recipe,
            Jsonb(
                {
                    "os": "ubuntu-24.04",
                    "layout_id": "std-v1",
                    "vmware_tools": True,
                    "cloud_init": True,
                    "guest_os": "Ubuntu 24.04 LTS",
                }
            ),
            image_digest,
            stable,
        ),
    )
    for name in factory_fake.DEFAULT_TESTS:
        conn.execute(
            "INSERT INTO image_build_tests (id, image_build_id, name, outcome, detail, simulated) "
            "VALUES (%s, %s, %s, 'passed', 'seed labo', true)",
            (uuid7(), bid, name),
        )
    for kind in ("dev", "test", "preprod", "prod"):
        conn.execute(
            "INSERT INTO image_build_promotions (id, image_build_id, environment_kind, reason) VALUES (%s, %s, %s, %s)",
            (uuid7(), bid, kind, "promotion initiale (seed fabrique F07)"),
        )
