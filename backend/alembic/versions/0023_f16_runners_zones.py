"""F16 : runners par site ou zone (mTLS, capacités, heartbeat).

Revision ID: 0023
Revises: 0022
"""

from pathlib import Path

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0023_f16_runners_zones.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
