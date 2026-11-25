"""Horloge injectable (F27). Les tests figent l'instant ; en production, UTC système.

Les horodatages PostgreSQL (`now()`) restent ceux de la base ; la programmation et
l'admission comparent via cette horloge pour que les recettes (plan expiré, DST) soient
déterministes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

_provider: Callable[[], datetime] | None = None


def now() -> datetime:
    instant = _provider() if _provider is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        return instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC)


def use(provider: Callable[[], datetime] | None) -> None:
    global _provider
    _provider = provider


@contextmanager
def frozen_at(instant: datetime) -> Iterator[None]:
    use(lambda: instant)
    try:
        yield
    finally:
        use(None)
