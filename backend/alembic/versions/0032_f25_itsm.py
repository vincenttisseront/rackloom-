"""F25 : ITSM et notifications.

Revision ID: 0032
Revises: 0031

Cascade après F24 : down_revision=0031. Aucun stub.
"""

from pathlib import Path

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0032_f25_itsm.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
