"""F02 compositions multi-VM et F04 graphe de dépendances.

Revision ID: 0018
Revises: 0017
"""

from pathlib import Path

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0018_f02_f04.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
