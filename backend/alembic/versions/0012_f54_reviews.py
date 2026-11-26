"""F54 : sollicitations de revue (passer la main).

Revision ID: 0012
Revises: 0011
"""

from pathlib import Path

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0012_f54_reviews.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
