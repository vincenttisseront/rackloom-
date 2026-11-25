"""Besoin de catégorie A (V01, V02) : porte d'entrée unique des brouillons, y compris ceux de l'assistant."""

from __future__ import annotations

from typing import Any

from .. import resolver


def validate(raw: Any, partial: bool = False) -> dict[str, Any]:
    return resolver.validate_need(raw, partial=partial)


def is_locked_key(key: str) -> bool:
    return resolver._is_technical(key)
