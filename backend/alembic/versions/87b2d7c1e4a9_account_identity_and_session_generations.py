"""account identity and non-reusable session generations

Revision ID: 87b2d7c1e4a9
Revises: 21405765a70f
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op


revision = "87b2d7c1e4a9"
down_revision = "21405765a70f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Hex identifiers avoid a database extension while remaining opaque and
    # long enough that they cannot collide in practice.
    op.add_column("users", sa.Column("identity", sa.String(32), nullable=True))
    op.execute(
        "UPDATE users SET identity = md5(id::text || clock_timestamp()::text || random()::text)"
    )
    op.alter_column("users", "identity", nullable=False)
    op.create_unique_constraint("uq_users_identity", "users", ["identity"])

    # `version + 1` was reused after delete/recreate.  A separate sequence
    # makes every future session generation unique across all accounts.
    op.execute("CREATE SEQUENCE users_session_version_seq")
    op.execute(
        "SELECT setval('users_session_version_seq', "
        "GREATEST(COALESCE((SELECT MAX(version) FROM users), 0), 1), true)"
    )

    # Existing vaults follow their existing accounts once; later accounts with
    # the same visible name get a different identity and cannot see them.
    op.execute(
        "UPDATE vault_keys AS vault SET person = users.identity "
        "FROM users WHERE vault.person = users.name"
    )
    op.execute(
        "UPDATE vault_files AS vault SET person = users.identity "
        "FROM users WHERE vault.person = users.name"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE vault_keys AS vault SET person = users.name "
        "FROM users WHERE vault.person = users.identity"
    )
    op.execute(
        "UPDATE vault_files AS vault SET person = users.name "
        "FROM users WHERE vault.person = users.identity"
    )
    op.execute("DROP SEQUENCE users_session_version_seq")
    op.drop_constraint("uq_users_identity", "users", type_="unique")
    op.drop_column("users", "identity")
