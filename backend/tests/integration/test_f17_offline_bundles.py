"""F17 : bundles pour sites déconnectés — signature, contenu exact, intégrité, compatibilité.

Recette : import nominal hors ligne ; bundle modifié d'un octet refusé.
Niveau : testé localement (charges utiles fictives ; VM QEMU isolée = lot 3).
"""

from __future__ import annotations

import base64

import pytest

from rackloom import offline_bundles
from rackloom.errors import ApiError

API = "/api/v1"


def _build_demo(**kwargs):
    private_raw, public_raw = offline_bundles.ensure_signing_keypair()
    package = offline_bundles.build_package(
        slug=kwargs.get("slug", "lab-offline"),
        version=kwargs.get("version", "1.0.0"),
        title=kwargs.get("title", "Lot de démo hors ligne"),
        items=offline_bundles.demo_items(),
        appliance_min_version=kwargs.get("min_version", "0.1.0"),
        appliance_max_version=kwargs.get("max_version", "0.1.99"),
        private_raw=private_raw,
    )
    return package, public_raw


def test_f17_build_verify_integrity_and_content(conn, demo, tmp_path, monkeypatch):
    """Lot signé : contenu exact listé ; digests cohérents ; signature valide."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    offline_bundles.ensure_trust_key(conn)
    package, _ = _build_demo()
    assert package.startswith(offline_bundles.MAGIC)

    manifest, signature, kid, payloads = offline_bundles.parse_package(package)
    kinds = {i["kind"] for i in manifest["items"]}
    assert kinds == set(offline_bundles.ITEM_KINDS)
    assert len(manifest["items"]) == 5
    digest = offline_bundles.verify_integrity(
        manifest,
        signature,
        kid,
        payloads,
        bytes(
            conn.execute("SELECT public_key FROM offline_bundle_trust_keys WHERE key_id = %s", (kid,)).fetchone()[
                "public_key"
            ]
        ),
    )
    assert digest == manifest["content_digest"]
    offline_bundles.check_compatibility(manifest)


def test_f17_octet_altere_refuse(conn, demo, tmp_path, monkeypatch):
    """Recette : un octet modifié dans le lot → refus bundle_tampered."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    offline_bundles.ensure_trust_key(conn)
    package, public_raw = _build_demo()

    # Altère un octet dans la zone des blobs (après l'enveloppe JSON).
    mutated = bytearray(package)
    # Cherche après le JSON : magic(4) + len(4) + json_len
    import struct

    json_len = struct.unpack(">I", package[4:8])[0]
    blob_start = 8 + json_len
    assert blob_start < len(mutated)
    mutated[blob_start] ^= 0x01

    with pytest.raises(ApiError) as ei:
        manifest, signature, kid, payloads = offline_bundles.parse_package(bytes(mutated))
        offline_bundles.verify_integrity(manifest, signature, kid, payloads, public_raw)
    assert ei.value.code == "bundle_tampered"


def test_f17_signature_altere_refuse(conn, demo, tmp_path, monkeypatch):
    """Signature altérée → refus même si digests d'items semblent cohérents."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    offline_bundles.ensure_trust_key(conn)
    package, public_raw = _build_demo()
    # Flip un octet dans l'enveloppe JSON (hors magic/len) pour casser la signature.
    mutated = bytearray(package)
    mutated[12] ^= 0x01
    with pytest.raises(ApiError) as ei:
        offline_bundles.parse_package(bytes(mutated))
        # parse peut échouer (JSON) ou verify ensuite
    # Si le JSON reste parseable, verify échoue ; sinon format_invalid.
    assert ei.value.code in ("bundle_tampered", "bundle_format_invalid")

    # Cas explicite : parse OK puis signature bidon.
    manifest, _sig, kid, payloads = offline_bundles.parse_package(package)
    with pytest.raises(ApiError) as ei2:
        offline_bundles.verify_integrity(manifest, "00" * 64, kid, payloads, public_raw)
    assert ei2.value.code == "bundle_tampered"


def test_f17_incompatible_refuse(conn, demo, tmp_path, monkeypatch):
    """Compatibilité hors plage → bundle_incompatible avant import."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()
    offline_bundles.ensure_trust_key(conn)
    package, public_raw = _build_demo(min_version="9.0.0", max_version="9.9.9")
    manifest, signature, kid, payloads = offline_bundles.parse_package(package)
    offline_bundles.verify_integrity(manifest, signature, kid, payloads, public_raw)
    with pytest.raises(ApiError) as ei:
        offline_bundles.check_compatibility(manifest)
    assert ei.value.code == "bundle_incompatible"


def test_f17_api_inspect_import_and_tamper(conn, demo, api, tmp_path, monkeypatch):
    """API admin : inspect affiche le contenu ; import OK ; lot altéré refusé."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    admin = api("admin")
    # Clé de confiance amorcée.
    keys = admin.get(f"{API}/admin/offline-bundles/trust-keys")
    assert keys.status_code == 200, keys.text
    assert keys.json()

    built = admin.post(
        f"{API}/admin/offline-bundles/build-demo",
        json={"slug": "lab-offline", "version": "1.0.0", "title": "Lot F17"},
    )
    assert built.status_code == 200, built.text
    package_b64 = built.json()["package_b64"]

    inspected = admin.post(
        f"{API}/admin/offline-bundles/inspect",
        json={"package_b64": package_b64},
    )
    assert inspected.status_code == 200, inspected.text
    body = inspected.json()
    assert body["status"] == "inspected"
    assert len(body["items"]) == 5
    assert {i["kind"] for i in body["items"]} == set(offline_bundles.ITEM_KINDS)
    assert body["content_digest"]
    # Contenu exact : digests et tailles exposés.
    for item in body["items"]:
        assert item["digest"]
        assert item["size_bytes"] > 0

    imported = admin.post(
        f"{API}/admin/offline-bundles/import",
        json={"bundle_id": body["id"]},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "imported"
    assert imported.json()["applied"]["jobs_created"] == 0
    assert imported.json()["applied"]["network"] == "none"

    listing = admin.get(f"{API}/admin/offline-bundles")
    assert listing.status_code == 200
    assert any(b["id"] == body["id"] for b in listing.json())

    # Lot altéré d'un octet → refus.
    raw = bytearray(base64.b64decode(package_b64))
    raw[-1] ^= 0x01
    tampered = admin.post(
        f"{API}/admin/offline-bundles/inspect",
        json={"package_b64": base64.b64encode(bytes(raw)).decode("ascii")},
    )
    assert tampered.status_code == 422, tampered.text
    assert tampered.json()["code"] == "bundle_tampered"

    # Incompatible.
    bad = admin.post(
        f"{API}/admin/offline-bundles/build-demo",
        json={"slug": "too-new", "version": "2.0.0", "incompatible": True},
    )
    assert bad.status_code == 200
    refuse = admin.post(
        f"{API}/admin/offline-bundles/inspect",
        json={"package_b64": bad.json()["package_b64"]},
    )
    assert refuse.status_code == 422
    assert refuse.json()["code"] == "bundle_incompatible"

    # Hors admin → 403.
    alice = api("alice")
    denied = alice.get(f"{API}/admin/offline-bundles")
    assert denied.status_code == 403
