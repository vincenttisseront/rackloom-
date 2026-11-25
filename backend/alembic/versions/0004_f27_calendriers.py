"""F27 : calendriers de changement (fenêtres et gels planifiés).

Revision ID: 0004
Revises: 0003_atelier
"""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003_atelier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0004_f27_calendriers.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
