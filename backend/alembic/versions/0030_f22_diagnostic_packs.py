"""F22 : dossiers de diagnostic partageables (payload JSONB, pas d'écriture artefacts API).

Revision ID: 0030
Revises: 0029
"""

from pathlib import Path

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0030_f22_diagnostic_packs.sql").read_text(
        encoding="utf-8"
    )
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
