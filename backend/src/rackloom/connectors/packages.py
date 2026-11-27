"""F30 : registre des paquets d'adaptateurs (provenance + approbation avant installation).

Aucune installation depuis l'interface sans statut `approved` et digest de confiance.
Un paquet soumis n'est pas installé ; approuver n'installe pas ; installer exige
l'approbation préalable. Publier / installer ne crée aucun job.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from .. import audit
from ..canonical import canonical_json
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_admin
from ..ids import uuid7
from . import capabilities_for
from .reference import KIND as REFERENCE_KIND
from .reference import capabilities_dict as reference_capabilities
from .sdk import CAPABILITIES

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
STATUSES = ("submitted", "approved", "installed", "rejected")
KINDS = ("reference", "proxmox", "opentofu", "awx", "other")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "kind": row["kind"],
        "status": row["status"],
        "content_digest": row["content_digest"],
        "manifest": row["manifest"],
        "provenance": row["provenance"],
        "submitted_by": str(row["submitted_by"]) if row["submitted_by"] else None,
        "submitted_at": row["submitted_at"],
        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        "approved_at": row["approved_at"],
        "installed_connector_id": str(row["installed_connector_id"]) if row["installed_connector_id"] else None,
        "installed_at": row["installed_at"],
        "rejected_by": str(row["rejected_by"]) if row["rejected_by"] else None,
        "rejected_at": row["rejected_at"],
        "reject_reason": row["reject_reason"],
        "actions": _actions(row),
    }


def _actions(row: dict[str, Any]) -> list[str]:
    status = row["status"]
    if status == "submitted":
        return ["approve", "reject"]
    if status == "approved":
        return ["install", "reject"]
    return []


def list_packages(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM adapter_packages ORDER BY submitted_at DESC, slug, version"
    ).fetchall()
    return [_view(r) for r in rows]


def get_package(conn: Conn, package_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM adapter_packages WHERE id = %s", (package_id,)).fetchone()
    if row is None:
        raise not_found("paquet d'adaptateur")
    return _view(row)


def list_trust_digests(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, content_digest, label, active, created_at FROM adapter_trust_digests "
        "WHERE active ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "content_digest": r["content_digest"],
            "label": r["label"],
            "active": r["active"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def ensure_reference_trust(conn: Conn) -> dict[str, Any]:
    """Enregistre le digest de confiance du paquet de référence de démo (idempotent)."""
    manifest = reference_manifest()
    digest = content_digest_for(manifest)
    existing = conn.execute(
        "SELECT * FROM adapter_trust_digests WHERE content_digest = %s", (digest,)
    ).fetchone()
    if existing:
        return existing
    return conn.execute(
        "INSERT INTO adapter_trust_digests (id, content_digest, label, active) "
        "VALUES (%s, %s, %s, true) RETURNING *",
        (uuid7(), digest, "adaptateur de référence F30 (laboratoire)"),
    ).fetchone()


def reference_manifest() -> dict[str, Any]:
    return {
        "format": "rackloom.adapter_package.v1",
        "slug": "reference-lab",
        "version": "1.0.0",
        "title": "Adaptateur de référence",
        "kind": REFERENCE_KIND,
        "capabilities": {name: True for name in ("configure", "test_connection")},
        "network": {
            "schemes": ["https"],
            "require_hostname_verify": True,
            "allow_user_supplied_url": False,
        },
        "entry": "rackloom.connectors.reference:ReferenceAdapter",
        "notes": "Paquet de référence F30 — aucun fournisseur externe.",
    }


def content_digest_for(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ApiError(422, "invalid_manifest", "manifeste objet attendu", field="/manifest")
    fmt = manifest.get("format")
    if fmt != "rackloom.adapter_package.v1":
        raise ApiError(422, "invalid_manifest", "format rackloom.adapter_package.v1 attendu", field="/manifest/format")
    slug = str(manifest.get("slug") or "")
    version = str(manifest.get("version") or "")
    title = str(manifest.get("title") or "")
    kind = str(manifest.get("kind") or "")
    if not SLUG_RE.match(slug):
        raise ApiError(422, "invalid_manifest", "slug invalide", field="/manifest/slug")
    if not VERSION_RE.match(version):
        raise ApiError(422, "invalid_manifest", "version semver attendue", field="/manifest/version")
    if not title or len(title) > 200:
        raise ApiError(422, "invalid_manifest", "titre entre 1 et 200 caractères", field="/manifest/title")
    if kind not in KINDS:
        raise ApiError(
            422,
            "invalid_manifest",
            f"kind attendu parmi {', '.join(KINDS)}",
            field="/manifest/kind",
        )
    caps = manifest.get("capabilities")
    if not isinstance(caps, dict) or not caps:
        raise ApiError(422, "invalid_manifest", "capabilities objet non vide attendu", field="/manifest/capabilities")
    for name in caps:
        if name not in CAPABILITIES:
            raise ApiError(
                422,
                "invalid_manifest",
                f"capacité inconnue : {name}",
                field=f"/manifest/capabilities/{name}",
            )
    network = manifest.get("network") or {}
    if network.get("allow_user_supplied_url") is True:
        raise ApiError(
            422,
            "invalid_manifest",
            "URL libre interdite dans le manifeste",
            field="/manifest/network/allow_user_supplied_url",
        )
    if network.get("require_hostname_verify") is False:
        raise ApiError(
            422,
            "invalid_manifest",
            "vérification du nom d'hôte obligatoire",
            field="/manifest/network/require_hostname_verify",
        )
    return {
        "format": fmt,
        "slug": slug,
        "version": version,
        "title": title,
        "kind": kind,
        "capabilities": caps,
        "network": {
            "schemes": list(network.get("schemes") or ["https"]),
            "require_hostname_verify": True,
            "allow_user_supplied_url": False,
        },
        "entry": str(manifest.get("entry") or ""),
        "notes": str(manifest.get("notes") or "")[:500],
    }


def submit(
    conn: Conn,
    principal: Principal,
    *,
    manifest: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Soumet un paquet pour revue. N'installe rien."""
    require_admin(principal)
    cleaned = _validate_manifest(manifest)
    digest = content_digest_for(cleaned)
    trusted = conn.execute(
        "SELECT id FROM adapter_trust_digests WHERE content_digest = %s AND active",
        (digest,),
    ).fetchone()
    if trusted is None:
        raise ApiError(
            422,
            "adapter_untrusted",
            "digest inconnu : enregistrez la provenance (adapter_trust_digests) avant soumission",
            field="/manifest",
        )
    prov = {
        "source": str((provenance or {}).get("source") or "lab"),
        "reviewer_note": str((provenance or {}).get("reviewer_note") or "")[:500],
        "trust_digest_id": str(trusted["id"]),
        "content_digest": digest,
    }
    existing = conn.execute(
        "SELECT id, status FROM adapter_packages WHERE slug = %s AND version = %s",
        (cleaned["slug"], cleaned["version"]),
    ).fetchone()
    if existing and existing["status"] != "rejected":
        raise ApiError(409, "adapter_exists", "ce slug/version est déjà soumis ou installé")
    row = conn.execute(
        "INSERT INTO adapter_packages (id, slug, version, title, kind, status, content_digest, "
        "manifest, provenance, submitted_by) "
        "VALUES (%s, %s, %s, %s, %s, 'submitted', %s, %s, %s, %s) "
        "ON CONFLICT (slug, version) DO UPDATE SET "
        "title = EXCLUDED.title, kind = EXCLUDED.kind, status = 'submitted', "
        "content_digest = EXCLUDED.content_digest, manifest = EXCLUDED.manifest, "
        "provenance = EXCLUDED.provenance, submitted_by = EXCLUDED.submitted_by, "
        "submitted_at = now(), approved_by = NULL, approved_at = NULL, "
        "installed_connector_id = NULL, installed_at = NULL, "
        "rejected_by = NULL, rejected_at = NULL, reject_reason = NULL "
        "RETURNING *",
        (
            uuid7(),
            cleaned["slug"],
            cleaned["version"],
            cleaned["title"],
            cleaned["kind"],
            digest,
            Jsonb(cleaned),
            Jsonb(prov),
            principal.user_id,
        ),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adapter.submit",
        outcome="ok",
        targets={"adapter": cleaned["slug"], "version": cleaned["version"], "digest": digest},
    )
    return _view(row)


def approve(conn: Conn, principal: Principal, package_id: UUID, *, reason: str = "") -> dict[str, Any]:
    """Approuve un paquet soumis. N'installe pas."""
    require_admin(principal)
    row = conn.execute("SELECT * FROM adapter_packages WHERE id = %s", (package_id,)).fetchone()
    if row is None:
        raise not_found("paquet d'adaptateur")
    if row["status"] != "submitted":
        raise ApiError(409, "adapter_not_submitted", "seuls les paquets soumis peuvent être approuvés")
    # Séparation des rôles : l'approbateur doit être distinct du soumetteur (F13 / invariant 11).
    if row["submitted_by"] is not None and row["submitted_by"] == principal.user_id:
        raise ApiError(
            403,
            "adapter_self_approval",
            "l'approbateur d'un paquet d'adaptateur doit être distinct du soumetteur",
        )
    updated = conn.execute(
        "UPDATE adapter_packages SET status = 'approved', approved_by = %s, approved_at = now(), "
        "reject_reason = NULL, rejected_by = NULL, rejected_at = NULL WHERE id = %s RETURNING *",
        (principal.user_id, package_id),
    ).fetchone()
    targets: dict[str, Any] = {"adapter": row["slug"], "version": row["version"]}
    if reason:
        targets["reason"] = reason[:500]
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adapter.approve",
        outcome="ok",
        targets=targets,
    )
    return _view(updated)


def reject(conn: Conn, principal: Principal, package_id: UUID, *, reason: str) -> dict[str, Any]:
    require_admin(principal)
    text = (reason or "").strip()
    if not text or len(text) > 500:
        raise ApiError(422, "invalid_reason", "motif de rejet entre 1 et 500 caractères", field="/reason")
    row = conn.execute("SELECT * FROM adapter_packages WHERE id = %s", (package_id,)).fetchone()
    if row is None:
        raise not_found("paquet d'adaptateur")
    if row["status"] in ("installed", "rejected"):
        raise ApiError(409, "adapter_not_rejectable", "paquet déjà installé ou rejeté")
    updated = conn.execute(
        "UPDATE adapter_packages SET status = 'rejected', rejected_by = %s, rejected_at = now(), "
        "reject_reason = %s WHERE id = %s RETURNING *",
        (principal.user_id, text, package_id),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adapter.reject",
        outcome="ok",
        targets={"adapter": row["slug"], "version": row["version"], "reason": text},
    )
    return _view(updated)


def install(conn: Conn, principal: Principal, package_id: UUID) -> dict[str, Any]:
    """Installe un paquet **approuvé** : crée le connecteur. Refuse sinon (`adapter_not_approved`)."""
    require_admin(principal)
    row = conn.execute("SELECT * FROM adapter_packages WHERE id = %s", (package_id,)).fetchone()
    if row is None:
        raise not_found("paquet d'adaptateur")
    if row["status"] != "approved":
        raise ApiError(
            409,
            "adapter_not_approved",
            "installation refusée : le paquet doit être approuvé et sa provenance contrôlée",
        )
    trusted = conn.execute(
        "SELECT id FROM adapter_trust_digests WHERE content_digest = %s AND active",
        (row["content_digest"],),
    ).fetchone()
    if trusted is None:
        raise ApiError(422, "adapter_untrusted", "digest de confiance révoqué ou absent")
    kind = row["kind"]
    if kind == REFERENCE_KIND:
        caps = reference_capabilities()
    elif kind in ("vsphere", "terraform", "ansible", "model"):
        caps = capabilities_for(kind, "real")
    else:
        # Proxmox / OpenTofu / AWX : capacités déclarées « non disponible » jusqu'à implémentation.
        from .sdk import capability

        caps = {name: capability(None, "non implémenté — guide docs/adapter-sdk.md").as_dict() for name in CAPABILITIES}
        caps["configure"] = {"available": True, "mode": "real", "note": "stub F30"}
    existing = conn.execute("SELECT id FROM connectors WHERE slug = %s", (row["slug"],)).fetchone()
    if existing:
        connector_id = existing["id"]
        conn.execute(
            "UPDATE connectors SET kind = %s, mode = 'real', capabilities = %s, "
            "config_version = config_version + 1 WHERE id = %s",
            (kind, Jsonb(caps), connector_id),
        )
    else:
        connector_id = uuid7()
        conn.execute(
            "INSERT INTO connectors (id, slug, kind, mode, capabilities) VALUES (%s, %s, %s, 'real', %s)",
            (connector_id, row["slug"], kind, Jsonb(caps)),
        )
    updated = conn.execute(
        "UPDATE adapter_packages SET status = 'installed', installed_connector_id = %s, "
        "installed_at = now() WHERE id = %s RETURNING *",
        (connector_id, package_id),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="adapter.install",
        outcome="ok",
        targets={"adapter": row["slug"], "version": row["version"], "connector": str(connector_id)},
    )
    return _view(updated)


def submit_reference_demo(conn: Conn, principal: Principal) -> dict[str, Any]:
    """Soumet le paquet de référence de laboratoire (digest pré-enregistré)."""
    ensure_reference_trust(conn)
    return submit(conn, principal, manifest=reference_manifest(), provenance={"source": "demo"})
