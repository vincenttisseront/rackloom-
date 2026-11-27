"""F26 : exercices de restauration et suspension des envois.

Revision ID: 0025
Revises: 0024
"""

from pathlib import Path

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0025_f26_restore_exercises.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
