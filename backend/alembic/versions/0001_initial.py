"""Schéma initial du lot 1.

Revision ID: 0001
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0001_initial.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
