"""Filtrage des secrets avant stockage, affichage, journalisation ou envoi au modèle.

Deux mécanismes complémentaires : valeurs secrètes connues (celles remises au job)
et motifs génériques. Le filtrage est appliqué aux sorties d'outils, aux événements,
aux observations d'hôtes et au contexte du chat.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[secret masqué]"

PATTERNS = [
    re.compile(r"RLCANARY-[0-9a-f]{8,64}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)authorization:\s*\S+(\s+\S+)?"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"),
]


class Redactor:
    def __init__(self, known_values: Iterable[str] = ()) -> None:
        self.known = sorted({v for v in known_values if v and len(v) >= 6}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.known:
            if secret in value:
                value = value.replace(secret, REDACTED)
        for pattern in PATTERNS:
            if pattern.groups >= 3:
                value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
            else:
                value = pattern.sub(REDACTED, value)
        return value

    def bytes(self, value: bytes) -> bytes:
        return self.text(value.decode("utf-8", errors="replace")).encode("utf-8")

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.data(v) for v in value]
        return value


DEFAULT = Redactor()


def redact(value: Any) -> Any:
    return DEFAULT.data(value)
