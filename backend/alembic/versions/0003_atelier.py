"""Atelier de templates (AT01) : brouillons, revue, propositions d'extension, preuves de vérification.

Revision ID: 0003_atelier
Revises: 0002
"""

from pathlib import Path

from alembic import op

revision = "0003_atelier"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0003_atelier.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
