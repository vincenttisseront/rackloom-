"""Primitives : mots de passe argon2id, jetons opaques, chiffrement des artefacts et secrets."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as pysecrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_hasher = PasswordHasher()

ARTIFACT_MAGIC = b"RLA1"
SEALED_MAGIC = b"RLS1"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Coût équivalent pour les comptes inexistants, contre l'énumération par chronométrage.
DUMMY_PASSWORD_HASH = _hasher.hash(pysecrets.token_urlsafe(16))


def new_token(nbytes: int = 32) -> str:
    return pysecrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for(session_token: str) -> str:
    """Jeton CSRF dérivé du jeton de session : recalculable sans être stocké en clair."""
    return hmac.new(session_token.encode(), b"rackloom-csrf-v1", hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- Clés -----------------------------------------------------------------------------

KEY_FILES = {
    "artifact-a": "artifact-a.key",  # plans binaires, sorties brutes : worker seul
    "artifact-b": "artifact-b.key",  # rapports téléchargeables : worker écrit, api lit
    "secrets-box-private": "secrets-box.key",  # worker seul
    "secrets-box-public": "secrets-box.pub",  # api (scelle sans pouvoir lire)
    # F17 : signature des lots hors ligne (Ed25519). Copiée vers keys-api pour build/inspect.
    "offline-bundle-sign": "offline-bundle-sign.key",
    "offline-bundle-sign-public": "offline-bundle-sign.pub",
    # Lot 4 : chiffrement du client_secret OIDC côté API (AES-GCM), jamais journalisé.
    "oidc-client": "oidc-client.key",
}


def generate_keys(directory: Path) -> list[Path]:
    """Crée les clés manquantes (0600). Ne remplace jamais une clé existante."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for name in ("artifact-a", "artifact-b", "oidc-client"):
        path = directory / KEY_FILES[name]
        if not path.exists():
            _write_secret_file(path, os.urandom(32))
            created.append(path)
    priv_path = directory / KEY_FILES["secrets-box-private"]
    pub_path = directory / KEY_FILES["secrets-box-public"]
    if not priv_path.exists():
        private = X25519PrivateKey.generate()
        _write_secret_file(
            priv_path,
            private.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
            ),
        )
        pub_path.write_bytes(
            private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        os.chmod(pub_path, 0o644)
        created += [priv_path, pub_path]
    # F17 : paire Ed25519 pour lots hors ligne (ne remplace jamais).
    ob_priv = directory / KEY_FILES["offline-bundle-sign"]
    ob_pub = directory / KEY_FILES["offline-bundle-sign-public"]
    if not ob_priv.exists():
        ed = Ed25519PrivateKey.generate()
        _write_secret_file(ob_priv, ed.private_bytes_raw())
        ob_pub.write_bytes(ed.public_key().public_bytes_raw())
        os.chmod(ob_pub, 0o644)
        created += [ob_priv, ob_pub]
    elif not ob_pub.exists():
        ed = Ed25519PrivateKey.from_private_bytes(ob_priv.read_bytes())
        ob_pub.write_bytes(ed.public_key().public_bytes_raw())
        os.chmod(ob_pub, 0o644)
        created.append(ob_pub)
    return created


def _write_secret_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def load_key(directory: Path, name: str) -> bytes:
    path = directory / KEY_FILES[name]
    data = path.read_bytes()
    if len(data) != 32:
        raise ValueError(f"clé {name} invalide")
    return data


# --- Artefacts (AES-256-GCM) ------------------------------------------------------------


def encrypt_artifact(key: bytes, key_class: str, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return ARTIFACT_MAGIC + key_class.encode() + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_artifact(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if not blob.startswith(ARTIFACT_MAGIC):
        raise ValueError("artefact non reconnu")
    nonce = blob[5:17]
    return AESGCM(key).decrypt(nonce, blob[17:], aad)


# --- Boîte scellée (X25519 + HKDF + ChaCha20-Poly1305) -----------------------------------


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _derive(shared: bytes, epk: bytes, rpk: bytes) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=epk + rpk, info=b"rackloom-sealed-v1").derive(shared)


def seal(public_raw: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Chiffre pour le détenteur de la clé privée. L'émetteur ne peut pas relire."""
    recipient = X25519PublicKey.from_public_bytes(public_raw)
    ephemeral = X25519PrivateKey.generate()
    epk = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _derive(ephemeral.exchange(recipient), epk, public_raw)
    nonce = os.urandom(12)
    return SEALED_MAGIC + epk + nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def unseal(private_raw: bytes, blob: bytes, aad: bytes) -> bytes:
    if not blob.startswith(SEALED_MAGIC):
        raise ValueError("secret scellé non reconnu")
    private = X25519PrivateKey.from_private_bytes(private_raw)
    rpk = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    epk = blob[4:36]
    nonce = blob[36:48]
    key = _derive(private.exchange(X25519PublicKey.from_public_bytes(epk)), epk, rpk)
    return ChaCha20Poly1305(key).decrypt(nonce, blob[48:], aad)
