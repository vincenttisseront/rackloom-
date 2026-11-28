"""Lot 4 : SSO OIDC (Dex local), mapping groupes, PKCE.

Revision ID: 0036
Revises: 0035
"""

from pathlib import Path

from alembic import op

revision = "0036"
# Cascade après Rudder RAC-36 (#50) : enchaîne sur 0035.
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / "0036_lot4_oidc.sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("pas de retour arrière du schéma : restaurer une sauvegarde")
