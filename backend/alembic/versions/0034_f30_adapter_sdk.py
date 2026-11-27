"""F30 : SDK d'adaptateurs et registre de paquets.

Revision ID: 0034
Revises: 0033
"""

from pathlib import Path

from alembic import op

revision = "0034"
# Cascade après F23 (#42) : enchaîne sur 0033.
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0034_f30_adapter_sdk.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
