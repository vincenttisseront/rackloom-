"""F07 : fabrique d'images (pipeline, tests, promotion, dépréciation).

Revision ID: 0020
Revises: 0019
"""

from pathlib import Path

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0020_f07_fabrique.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
