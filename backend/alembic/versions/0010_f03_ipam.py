"""F03 : réservations IPAM/DNS.

Revision ID: 0010
Revises: 0009
"""

from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0010_f03_ipam.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
