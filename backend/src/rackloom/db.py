"""Accès PostgreSQL : pool de connexions, transactions courtes et migrations Alembic.

Règle : aucune transaction ne reste ouverte pendant un appel au launcher, à un runner
ou à un connecteur. Les modules appellent `transaction()` pour des blocs courts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None

__all__ = ["Jsonb", "close_pool", "connect", "get_pool", "migrate", "transaction"]


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            settings.resolved_database_url(),
            min_size=1,
            max_size=settings.db_pool_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def connect(url: str | None = None, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(url or get_settings().resolved_database_url(), row_factory=dict_row,
                           autocommit=autocommit)


def alembic_dir() -> Path:
    env = os.environ.get("RACKLOOM_ALEMBIC_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "alembic"


def migrate(url: str | None = None) -> None:
    """Applique les migrations Alembic manquantes. Idempotent."""
    from alembic import command
    from alembic.config import Config

    target = url or get_settings().resolved_migrator_url()
    sa_url = target.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir()))
    cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
