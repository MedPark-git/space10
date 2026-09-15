"""Initial schema marker.

Revision ID: 0001_initial
This deployment creates the initial schema idempotently under a PostgreSQL advisory
lock. Future schema changes must use explicit Alembic revisions after this marker.
"""
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Initial production schema downgrade is intentionally disabled")

