"""JSON canonique (RFC 8785, sous-ensemble) et empreintes SHA-256.

Les documents canonisés ne contiennent que des chaînes, entiers, booléens, null,
listes et objets : les flottants sont refusés pour éviter toute ambiguïté de sérialisation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _check(value: Any, path: str = "") -> None:
    if isinstance(value, float):
        raise ValueError(f"flottant interdit dans un document canonique : {path or '/'}")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"clé non textuelle : {path}/{key!r}")
            _check(item, f"{path}/{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check(item, f"{path}/{index}")


def canonical_json(value: Any) -> bytes:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def file_tree_digest(root, patterns: tuple[str, ...] = ("**/*",)) -> str:
    """Empreinte d'un arbre de fichiers : chemins relatifs triés et SHA-256 de chaque contenu."""
    from pathlib import Path

    root = Path(root)
    entries = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and ".terraform" not in path.parts and "__pycache__" not in path.parts:
                entries.append([path.relative_to(root).as_posix(), sha256_hex(path.read_bytes())])
    entries.sort()
    return digest(entries)
