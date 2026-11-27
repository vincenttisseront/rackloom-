"""F41–F45 : SCIM, revue d'accès, rotation, copie d'audit, certificats TLS.

Revision ID: 0016
Revises: 0015
"""

from pathlib import Path

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0016_f41_f45.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
