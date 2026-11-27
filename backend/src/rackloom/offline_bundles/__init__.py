"""F17 : bundles signés pour sites déconnectés.

Lots redistribuables (images applicatives, dépendances, providers Terraform,
collections Ansible, modèles) signés Ed25519. Le contenu exact est affiché ;
intégrité et compatibilité sont vérifiées avant tout import. Un octet altéré
refuse l'import. Aucun job n'est créé ; un import n'altère ni plan ni déploiement.
La clé privée de signature vit hors base (`keys_dir`), jamais exposée aux runners.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .. import audit
from ..canonical import canonical_json, sha256_hex
from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_admin
from ..ids import uuid7

MAGIC = b"RLB1"
FORMAT = "rackloom.offline_bundle.v1"
ITEM_KINDS = ("image", "dependency", "terraform_provider", "ansible_collection", "model")
# Version d'appliance locale (package Python) pour le contrôle de compatibilité.
APPLIANCE_VERSION = "0.1.0"
APPLIANCE_ARCH = "amd64"
APPLIANCE_OS = "ubuntu-24.04"
SIGN_KEY_NAME = "offline-bundle-sign"
SIGN_KEY_FILE = "offline-bundle-sign.key"
TRUST_LABEL_DEMO = "clé de démo F17 (sites déconnectés)"


def _bundles_dir() -> Path:
    """Stockage des lots inspectés. Volume dédié en Compose (API read_only ailleurs)."""
    path = get_settings().data_dir / "offline-bundles"
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError as exc:
        raise ApiError(
            500,
            "bundle_storage_unavailable",
            "stockage des lots hors ligne indisponible (volume offline-bundles)",
        ) from exc


def _dir_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".rackloom-write-probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _signing_keys_dir(*, create: bool = False) -> Path | None:
    """Répertoire des clés F17. Sur l'API (keys :ro) : lecture seule ; création via `keys init`."""
    preferred = get_settings().keys_dir
    if (preferred / SIGN_KEY_FILE).exists() or (preferred / f"{SIGN_KEY_NAME}.pub").exists():
        return preferred
    if create and _dir_writable(preferred):
        return preferred
    # Tests locaux : data_dir est souvent le seul volume writable.
    fallback = get_settings().data_dir / "offline-bundle-keys"
    if (fallback / SIGN_KEY_FILE).exists() or (fallback / f"{SIGN_KEY_NAME}.pub").exists():
        return fallback
    if create and _dir_writable(fallback):
        return fallback
    return preferred if preferred.exists() else None


def _parse_semver(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ApiError(422, "invalid_version", f"version semver invalide : {value}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _semver_le(a: str, b: str) -> bool:
    return _parse_semver(a) <= _parse_semver(b)


def _semver_ge(a: str, b: str) -> bool:
    return _parse_semver(a) >= _parse_semver(b)


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def load_signing_public(keys_dir: Path | None = None) -> bytes | None:
    """Lit la clé publique F17 depuis le volume de clés (sans écrire)."""
    directory = keys_dir or _signing_keys_dir(create=False)
    if directory is None:
        return None
    pub_path = directory / f"{SIGN_KEY_NAME}.pub"
    if pub_path.exists():
        data = pub_path.read_bytes()
        return data if len(data) == 32 else None
    priv_path = directory / SIGN_KEY_FILE
    if priv_path.exists():
        private = Ed25519PrivateKey.from_private_bytes(priv_path.read_bytes())
        return private.public_key().public_bytes_raw()
    return None


def ensure_signing_keypair(keys_dir: Path | None = None) -> tuple[bytes, bytes]:
    """Charge ou crée la paire Ed25519. Ne remplace jamais une clé existante.

    Sur l'API (volume keys en lecture seule) : la paire doit déjà exister (`keys init`).
    """
    directory = keys_dir or _signing_keys_dir(create=True)
    if directory is None:
        raise ApiError(
            500,
            "signing_key_missing",
            "clé de signature offline-bundle absente ; exécuter rackloom keys init",
        )
    priv_path = directory / SIGN_KEY_FILE
    pub_path = directory / f"{SIGN_KEY_NAME}.pub"
    if priv_path.exists():
        private_raw = priv_path.read_bytes()
        if len(private_raw) != 32:
            raise ApiError(500, "signing_key_invalid", "clé de signature offline-bundle invalide")
        private = Ed25519PrivateKey.from_private_bytes(private_raw)
        public_raw = private.public_key().public_bytes_raw()
        return private_raw, public_raw
    if not _dir_writable(directory):
        raise ApiError(
            500,
            "signing_key_missing",
            "clé de signature offline-bundle absente sur volume en lecture seule",
        )
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes_raw()
    public_raw = private.public_key().public_bytes_raw()
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(private_raw)
    pub_path.write_bytes(public_raw)
    os.chmod(pub_path, 0o644)
    return private_raw, public_raw


def ensure_trust_key(conn: Conn, *, public_raw: bytes | None = None, label: str = TRUST_LABEL_DEMO) -> dict[str, Any]:
    """Enregistre la clé publique de confiance (démo) si absente. N'écrit pas sur un FS en lecture seule."""
    if public_raw is None:
        public_raw = load_signing_public()
        if public_raw is None:
            # Tests / labo natif : crée la paire si le volume est writable.
            try:
                _, public_raw = ensure_signing_keypair()
            except ApiError:
                # Aucune clé disque : amorce une confiance éphémère (inspect/import de lots pré-signés).
                public_raw = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    kid = key_id_for(public_raw)
    row = conn.execute("SELECT * FROM offline_bundle_trust_keys WHERE key_id = %s", (kid,)).fetchone()
    if row:
        return dict(row)
    inserted = conn.execute(
        "INSERT INTO offline_bundle_trust_keys (id, key_id, public_key, label, active) "
        "VALUES (%s, %s, %s, %s, true) RETURNING *",
        (uuid7(), kid, public_raw, label),
    ).fetchone()
    if inserted is None:
        raise ApiError(500, "trust_key_insert_failed", "insertion de la clé de confiance impossible")
    return dict(inserted)


def ensure_seeded(conn: Conn) -> None:
    """Amorce la clé de confiance depuis keys_dir (sans écrire de clé privée)."""
    if not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'offline_bundle_trust_keys'"
    ).fetchone():
        return
    public_raw = load_signing_public()
    if public_raw is None:
        # Volume keys non monté (tests unitaires sans init) : pas bloquant pour le seed.
        return
    ensure_trust_key(conn, public_raw=public_raw)


def _signing_payload(manifest_body: dict[str, Any]) -> bytes:
    """Document signé : slug, version, titre, compatibilité et items (digests inclus)."""
    return canonical_json(
        {
            "format": manifest_body["format"],
            "slug": manifest_body["slug"],
            "version": manifest_body["version"],
            "title": manifest_body["title"],
            "appliance_compat": manifest_body["appliance_compat"],
            "items": manifest_body["items"],
        }
    )


def content_digest_of(manifest_body: dict[str, Any]) -> str:
    return sha256_hex(_signing_payload(manifest_body))


def sign_manifest(manifest_body: dict[str, Any], private_raw: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(private_raw)
    return private.sign(_signing_payload(manifest_body)).hex()


def verify_signature(manifest_body: dict[str, Any], signature_hex: str, public_raw: bytes) -> None:
    public = Ed25519PublicKey.from_public_bytes(public_raw)
    try:
        public.verify(bytes.fromhex(signature_hex), _signing_payload(manifest_body))
    except (InvalidSignature, ValueError) as exc:
        raise ApiError(
            422,
            "bundle_tampered",
            "signature du bundle invalide ou contenu altéré",
        ) from exc


def _pack_item(kind: str, name: str, version: str, payload: bytes, media_type: str) -> dict[str, Any]:
    if kind not in ITEM_KINDS:
        raise ApiError(422, "invalid_item_kind", f"kind d'item invalide : {kind}")
    if not name or not version:
        raise ApiError(422, "item_required", "name et version d'item obligatoires")
    return {
        "kind": kind,
        "name": name,
        "version": version,
        "digest": sha256_hex(payload),
        "size_bytes": len(payload),
        "media_type": media_type or "application/octet-stream",
        "payload": payload,
    }


def build_package(
    slug: str,
    version: str,
    title: str,
    items: list[dict[str, Any]],
    *,
    appliance_min_version: str = "0.1.0",
    appliance_max_version: str = "0.1.99",
    appliance_arch: str = APPLIANCE_ARCH,
    appliance_os: str = APPLIANCE_OS,
    private_raw: bytes | None = None,
) -> bytes:
    """Construit un lot binaire signé (RLB1). Aucune écriture base."""
    if private_raw is None:
        private_raw, _ = ensure_signing_keypair()
    packed: list[dict[str, Any]] = []
    for raw in items:
        payload = raw["payload"] if isinstance(raw.get("payload"), (bytes, bytearray)) else bytes(raw["payload"])
        packed.append(
            _pack_item(
                str(raw["kind"]),
                str(raw["name"]),
                str(raw["version"]),
                payload,
                str(raw.get("media_type") or "application/octet-stream"),
            )
        )
    if not packed:
        raise ApiError(422, "empty_bundle", "un bundle doit contenir au moins un item")
    # Tri stable pour empreinte déterministe.
    packed.sort(key=lambda i: (i["kind"], i["name"], i["version"]))
    manifest_items = [
        {
            "kind": i["kind"],
            "name": i["name"],
            "version": i["version"],
            "digest": i["digest"],
            "size_bytes": i["size_bytes"],
            "media_type": i["media_type"],
        }
        for i in packed
    ]
    body = {
        "format": FORMAT,
        "slug": slug,
        "version": version,
        "title": title,
        "appliance_compat": {
            "min_version": appliance_min_version,
            "max_version": appliance_max_version,
            "arch": appliance_arch,
            "os": appliance_os,
        },
        "items": manifest_items,
    }
    body["content_digest"] = content_digest_of(body)
    signature = sign_manifest(body, private_raw)
    kid = key_id_for(Ed25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes_raw())
    envelope = {
        "manifest": body,
        "signature": signature,
        "signer_key_id": kid,
    }
    envelope_json = canonical_json(envelope)
    # RLB1 | u32be json_len | json | blobs concaténés dans l'ordre des items.
    blob = b"".join(i["payload"] for i in packed)
    return MAGIC + struct.pack(">I", len(envelope_json)) + envelope_json + blob


def parse_package(data: bytes) -> tuple[dict[str, Any], str, str, list[bytes]]:
    """Décode un lot RLB1 → (manifest, signature_hex, signer_key_id, payloads)."""
    if len(data) < 8 or not data.startswith(MAGIC):
        raise ApiError(422, "bundle_format_invalid", "lot non reconnu (magic RLB1 attendu)")
    (json_len,) = struct.unpack(">I", data[4:8])
    if json_len < 2 or 8 + json_len > len(data):
        raise ApiError(422, "bundle_format_invalid", "longueur de manifeste invalide")
    try:
        import json

        envelope = json.loads(data[8 : 8 + json_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(422, "bundle_format_invalid", "manifeste JSON illisible") from exc
    if not isinstance(envelope, dict):
        raise ApiError(422, "bundle_format_invalid", "enveloppe invalide")
    manifest = envelope.get("manifest")
    signature = envelope.get("signature")
    signer_key_id = envelope.get("signer_key_id")
    if not isinstance(manifest, dict) or not isinstance(signature, str) or not isinstance(signer_key_id, str):
        raise ApiError(422, "bundle_format_invalid", "champs manifeste / signature manquants")
    if manifest.get("format") != FORMAT:
        raise ApiError(422, "bundle_format_invalid", f"format attendu {FORMAT}")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(422, "bundle_format_invalid", "items manquants")
    blob = data[8 + json_len :]
    payloads: list[bytes] = []
    offset = 0
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(422, "bundle_format_invalid", "item invalide")
        size = int(item.get("size_bytes") or -1)
        if size < 0 or offset + size > len(blob):
            raise ApiError(422, "bundle_tampered", "taille d'item incohérente avec le lot")
        payloads.append(blob[offset : offset + size])
        offset += size
    if offset != len(blob):
        raise ApiError(422, "bundle_tampered", "octets surnuméraires dans le lot")
    return manifest, signature, signer_key_id, payloads


def verify_integrity(
    manifest: dict[str, Any],
    signature_hex: str,
    signer_key_id: str,
    payloads: list[bytes],
    public_raw: bytes,
) -> str:
    """Vérifie digests d'items, empreinte de contenu et signature. Retourne content_digest."""
    items = manifest["items"]
    if len(items) != len(payloads):
        raise ApiError(422, "bundle_tampered", "nombre d'items ≠ charges utiles")
    for item, payload in zip(items, payloads, strict=True):
        expected = str(item["digest"])
        actual = sha256_hex(payload)
        if actual != expected or int(item["size_bytes"]) != len(payload):
            raise ApiError(
                422,
                "bundle_tampered",
                f"intégrité rompue pour {item.get('kind')}/{item.get('name')}@{item.get('version')}",
                details={"expected_digest": expected, "actual_digest": actual},
            )
        if item.get("kind") not in ITEM_KINDS:
            raise ApiError(422, "bundle_format_invalid", f"kind inconnu : {item.get('kind')}")
    expected_digest = content_digest_of(manifest)
    declared = str(manifest.get("content_digest") or "")
    if declared and declared != expected_digest:
        raise ApiError(422, "bundle_tampered", "empreinte de contenu déclarée incorrecte")
    # La clé publique fournie doit correspondre à l'identifiant déclaré.
    if key_id_for(public_raw) != signer_key_id:
        raise ApiError(422, "bundle_untrusted", "clé de signature non reconnue pour ce lot")
    verify_signature(manifest, signature_hex, public_raw)
    return expected_digest


def check_compatibility(
    manifest: dict[str, Any],
    *,
    appliance_version: str = APPLIANCE_VERSION,
    appliance_arch: str = APPLIANCE_ARCH,
    appliance_os: str = APPLIANCE_OS,
) -> None:
    """Refuse un lot incompatible avec l'appliance locale (avant import)."""
    compat = manifest.get("appliance_compat") or {}
    min_v = str(compat.get("min_version") or "")
    max_v = str(compat.get("max_version") or "")
    arch = str(compat.get("arch") or "")
    os_name = str(compat.get("os") or "")
    if not min_v or not max_v:
        raise ApiError(422, "bundle_incompatible", "bornes de compatibilité absentes")
    if arch and arch != appliance_arch:
        raise ApiError(
            422,
            "bundle_incompatible",
            f"architecture incompatible : lot={arch}, appliance={appliance_arch}",
            details={"field": "appliance_compat.arch"},
        )
    if os_name and os_name != appliance_os:
        raise ApiError(
            422,
            "bundle_incompatible",
            f"OS incompatible : lot={os_name}, appliance={appliance_os}",
            details={"field": "appliance_compat.os"},
        )
    if not (_semver_ge(appliance_version, min_v) and _semver_le(appliance_version, max_v)):
        raise ApiError(
            422,
            "bundle_incompatible",
            f"version appliance {appliance_version} hors plage [{min_v}, {max_v}]",
            details={
                "appliance_version": appliance_version,
                "min_version": min_v,
                "max_version": max_v,
            },
        )


def _public_for(conn: Conn, signer_key_id: str) -> bytes:
    row = conn.execute(
        "SELECT * FROM offline_bundle_trust_keys WHERE key_id = %s AND active",
        (signer_key_id,),
    ).fetchone()
    if row is None:
        raise ApiError(422, "bundle_untrusted", "aucune clé de confiance active pour ce signataire")
    return bytes(row["public_key"])


def _item_views(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "kind": i["kind"],
            "name": i["name"],
            "version": i["version"],
            "digest": i["digest"],
            "size_bytes": i["size_bytes"],
            "media_type": i["media_type"],
        }
        for i in manifest["items"]
    ]


def _view(conn: Conn, bundle_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM offline_bundles WHERE id = %s", (bundle_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "bundle introuvable")
    items = list(
        conn.execute(
            "SELECT kind, name, version, digest, size_bytes, media_type FROM offline_bundle_items "
            "WHERE bundle_id = %s ORDER BY kind, name, version",
            (bundle_id,),
        ).fetchall()
    )
    imports = list(
        conn.execute(
            "SELECT outcome, detail, created_at FROM offline_bundle_imports "
            "WHERE bundle_id = %s ORDER BY created_at DESC",
            (bundle_id,),
        ).fetchall()
    )
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "version": row["version"],
        "title": row["title"],
        "status": row["status"],
        "format": row["format"],
        "content_digest": row["content_digest"],
        "signature": row["signature"],
        "signer_key_id": row["signer_key_id"],
        "appliance_compat": {
            "min_version": row["appliance_min_version"],
            "max_version": row["appliance_max_version"],
            "arch": row["appliance_arch"],
            "os": row["appliance_os"],
        },
        "size_bytes": row["size_bytes"],
        "reject_code": row["reject_code"],
        "reject_reason": row["reject_reason"],
        "inspected_at": row["inspected_at"],
        "imported_at": row["imported_at"],
        "items": [
            {
                "kind": i["kind"],
                "name": i["name"],
                "version": i["version"],
                "digest": i["digest"],
                "size_bytes": i["size_bytes"],
                "media_type": i["media_type"],
            }
            for i in items
        ],
        "imports": [{"outcome": r["outcome"], "detail": r["detail"], "created_at": r["created_at"]} for r in imports],
        "notice": (
            "Import hors ligne : intégrité et compatibilité vérifiées avant application. "
            "Aucun job créé ; aucun plan ni déploiement modifié."
        ),
    }


def list_bundles(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, slug, version, title, status, content_digest, size_bytes, inspected_at, imported_at "
        "FROM offline_bundles ORDER BY inspected_at DESC"
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "slug": r["slug"],
            "version": r["version"],
            "title": r["title"],
            "status": r["status"],
            "content_digest": r["content_digest"],
            "size_bytes": r["size_bytes"],
            "inspected_at": r["inspected_at"],
            "imported_at": r["imported_at"],
        }
        for r in rows
    ]


def get(conn: Conn, bundle_id: UUID) -> dict[str, Any]:
    return _view(conn, bundle_id)


def inspect_package(
    conn: Conn,
    principal: Principal,
    data: bytes,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Vérifie intégrité + compatibilité et affiche le contenu exact. N'importe pas."""
    require_admin(principal)
    ensure_trust_key(conn)
    try:
        manifest, signature, signer_key_id, payloads = parse_package(data)
        public_raw = _public_for(conn, signer_key_id)
        content_digest = verify_integrity(manifest, signature, signer_key_id, payloads, public_raw)
        check_compatibility(manifest)
    except ApiError as exc:
        if persist and exc.code in (
            "bundle_tampered",
            "bundle_untrusted",
            "bundle_format_invalid",
            "bundle_incompatible",
        ):
            # Enregistrement minimal du refus pour audit UI (sans stocker le blob altéré).
            audit.record(
                conn,
                actor_type="user",
                actor_id=str(principal.user_id),
                action="offline_bundle.inspect_refused",
                outcome="denied",
                targets={"code": exc.code},
                policy_decision={"message": exc.message},
            )
        raise

    existing = conn.execute("SELECT id FROM offline_bundles WHERE content_digest = %s", (content_digest,)).fetchone()
    if existing:
        return _view(conn, existing["id"])

    if not persist:
        return {
            "slug": manifest["slug"],
            "version": manifest["version"],
            "title": manifest["title"],
            "status": "inspected",
            "format": manifest["format"],
            "content_digest": content_digest,
            "signature": signature,
            "signer_key_id": signer_key_id,
            "appliance_compat": manifest["appliance_compat"],
            "size_bytes": len(data),
            "items": _item_views(manifest),
            "imports": [],
            "notice": "Inspection seule : aucun enregistrement.",
        }

    bid = uuid7()
    compat = manifest["appliance_compat"]
    conn.execute(
        "INSERT INTO offline_bundles (id, slug, version, title, status, format, manifest, content_digest, "
        "signature, signer_key_id, appliance_min_version, appliance_max_version, appliance_arch, "
        "appliance_os, size_bytes, inspected_by) VALUES ("
        "%s, %s, %s, %s, 'inspected', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            bid,
            manifest["slug"],
            manifest["version"],
            manifest["title"],
            FORMAT,
            Jsonb({**manifest, "content_digest": content_digest}),
            content_digest,
            signature,
            signer_key_id,
            compat["min_version"],
            compat["max_version"],
            compat["arch"],
            compat["os"],
            len(data),
            principal.user_id,
        ),
    )
    for item in manifest["items"]:
        conn.execute(
            "INSERT INTO offline_bundle_items (id, bundle_id, kind, name, version, digest, size_bytes, media_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid7(),
                bid,
                item["kind"],
                item["name"],
                item["version"],
                item["digest"],
                item["size_bytes"],
                item["media_type"],
            ),
        )
    # Stocke le lot intact pour import ultérieur (sites déconnectés).
    rel = Path(str(bid)) / "bundle.rlb1"
    path = _bundles_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(path)
    conn.execute("UPDATE offline_bundles SET storage_path = %s WHERE id = %s", (str(rel), bid))
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="offline_bundle.inspected",
        outcome="ok",
        targets={"bundle_id": str(bid), "slug": manifest["slug"]},
        versions={"bundle": manifest["version"], "content_digest": content_digest},
    )
    return _view(conn, bid)


def import_bundle(conn: Conn, principal: Principal, bundle_id: UUID) -> dict[str, Any]:
    """Ré-vérifie intégrité + compatibilité, puis marque importé. Aucun job."""
    require_admin(principal)
    row = conn.execute("SELECT * FROM offline_bundles WHERE id = %s FOR UPDATE", (bundle_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "bundle introuvable")
    if row["status"] == "imported":
        return _view(conn, bundle_id)
    if row["status"] == "rejected":
        raise ApiError(409, "bundle_rejected", "ce lot a déjà été refusé")
    if not row["storage_path"]:
        raise ApiError(409, "bundle_missing", "fichier du lot absent")

    path = _bundles_dir() / row["storage_path"]
    if not path.is_file():
        raise ApiError(409, "bundle_missing", "fichier du lot introuvable sur disque")
    data = path.read_bytes()

    try:
        manifest, signature, signer_key_id, payloads = parse_package(data)
        public_raw = _public_for(conn, signer_key_id)
        content_digest = verify_integrity(manifest, signature, signer_key_id, payloads, public_raw)
        if content_digest != row["content_digest"]:
            raise ApiError(422, "bundle_tampered", "empreinte divergente depuis l'inspection")
        check_compatibility(manifest)
    except ApiError as exc:
        conn.execute(
            "UPDATE offline_bundles SET status = 'rejected', reject_code = %s, reject_reason = %s WHERE id = %s",
            (exc.code, exc.message[:500], bundle_id),
        )
        conn.execute(
            "INSERT INTO offline_bundle_imports (id, bundle_id, outcome, detail, actor_id) "
            "VALUES (%s, %s, 'refused', %s, %s)",
            (uuid7(), bundle_id, exc.message[:500], principal.user_id),
        )
        audit.record(
            conn,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="offline_bundle.import_refused",
            outcome="denied",
            targets={"bundle_id": str(bundle_id), "code": exc.code},
        )
        raise

    # « Application » locale : enregistrement des items comme disponibles hors ligne.
    # Pas de pull réseau, pas de job, pas de mutation de plan / déploiement.
    conn.execute(
        "UPDATE offline_bundles SET status = 'imported', imported_by = %s, imported_at = now(), "
        "reject_code = NULL, reject_reason = NULL WHERE id = %s",
        (principal.user_id, bundle_id),
    )
    conn.execute(
        "INSERT INTO offline_bundle_imports (id, bundle_id, outcome, detail, actor_id) VALUES (%s, %s, 'ok', %s, %s)",
        (
            uuid7(),
            bundle_id,
            f"{len(manifest['items'])} items importés hors ligne",
            principal.user_id,
        ),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="offline_bundle.imported",
        outcome="ok",
        targets={"bundle_id": str(bundle_id), "slug": row["slug"]},
        versions={"bundle": row["version"], "content_digest": row["content_digest"]},
        policy_decision={"items": len(manifest["items"]), "jobs_created": 0},
    )
    view = _view(conn, bundle_id)
    view["applied"] = {
        "items": _item_views(manifest),
        "jobs_created": 0,
        "network": "none",
    }
    return view


def import_package(conn: Conn, principal: Principal, data: bytes) -> dict[str, Any]:
    """Inspecte puis importe en une étape (après vérifications)."""
    view = inspect_package(conn, principal, data, persist=True)
    return import_bundle(conn, principal, UUID(view["id"]))


def list_trust_keys(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key_id, label, active, created_at FROM offline_bundle_trust_keys ORDER BY created_at"
    ).fetchall()
    return [
        {
            "key_id": r["key_id"],
            "label": r["label"],
            "active": r["active"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def demo_items() -> list[dict[str, Any]]:
    """Charges utiles fictives pour recettes locales (aucun binaire réel)."""
    return [
        {
            "kind": "image",
            "name": "rackloom-api",
            "version": "0.1.0",
            "payload": b"demo-image-rackloom-api-v0.1.0\n",
            "media_type": "application/vnd.oci.image.layer.v1.tar",
        },
        {
            "kind": "dependency",
            "name": "uv-lock",
            "version": "0.1.0",
            "payload": b"demo-uv.lock\n",
            "media_type": "text/plain",
        },
        {
            "kind": "terraform_provider",
            "name": "registry.terraform.io/hashicorp/local",
            "version": "2.5.3",
            "payload": b"demo-provider-local-2.5.3\n",
            "media_type": "application/octet-stream",
        },
        {
            "kind": "ansible_collection",
            "name": "rackloom.nginx",
            "version": "1.0.0",
            "payload": b"demo-collection-nginx-1.0.0\n",
            "media_type": "application/gzip",
        },
        {
            "kind": "model",
            "name": "llama3.1:8b-instruct-q4_0",
            "version": "1",
            "payload": b"demo-ollama-model-stub\n",
            "media_type": "application/octet-stream",
        },
    ]


# Réexport utile aux tests / CLI.
__all__ = [
    "APPLIANCE_VERSION",
    "FORMAT",
    "ITEM_KINDS",
    "MAGIC",
    "build_package",
    "check_compatibility",
    "content_digest_of",
    "demo_items",
    "ensure_seeded",
    "ensure_signing_keypair",
    "ensure_trust_key",
    "get",
    "import_bundle",
    "import_package",
    "inspect_package",
    "list_bundles",
    "list_trust_keys",
    "parse_package",
    "verify_integrity",
]
