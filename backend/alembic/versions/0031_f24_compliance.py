"""F24 : conformité des machines (profils, scans lecture seule, Rudder fictif).

Revision ID: 0031
Revises: 0030
"""

from pathlib import Path

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0031_f24_compliance.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
