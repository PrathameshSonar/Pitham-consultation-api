"""Feature batch — May 2026.


Schema changes accumulated across the May 2026 feature batch:


  * users.dob / tob / birth_place / city / state / country → nullable so a
    Google sign-up can land in the DB without filling the full astrology
    profile upfront.
  * events.event_days → optional integer (1-30) for multi-day events.
  * appointments.analysis_files → JSON-encoded list of additional analysis
    file paths (multi-upload). Defaults to "[]".


The `on_hold` value added to AppointmentStatus is purely string-side; the
column is VARCHAR(30), so existing rows are unaffected and no DDL is
needed for that.


Revision ID: 20260523_01
Revises:
Create Date: 2026-05-23
"""


from __future__ import annotations


import sqlalchemy as sa
from alembic import op




# revision identifiers, used by Alembic.
revision = "20260523_01"
down_revision = None
branch_labels = None
depends_on = None




def upgrade() -> None:
    # Users — relax profile NOT NULLs so Google + email-only signups don't
    # need to fill the whole astrology profile right away.
    with op.batch_alter_table("users") as batch:
        batch.alter_column("dob", existing_type=sa.String(length=20), nullable=True)
        batch.alter_column("tob", existing_type=sa.String(length=20), nullable=True)
        batch.alter_column("birth_place", existing_type=sa.String(length=150), nullable=True)
        batch.alter_column("city", existing_type=sa.String(length=100), nullable=True)
        batch.alter_column("state", existing_type=sa.String(length=100), nullable=True)
        batch.alter_column("country", existing_type=sa.String(length=100), nullable=True)


    # Events — optional length-in-days for multi-day events.
    op.add_column("events", sa.Column("event_days", sa.Integer(), nullable=True))


    # Appointments — multi-upload analysis files. JSON-encoded list stored as
    # Text so MySQL <8 stays happy.
    op.add_column(
        "appointments",
        sa.Column("analysis_files", sa.Text(), nullable=True, server_default="[]"),
    )




def downgrade() -> None:
    op.drop_column("appointments", "analysis_files")
    op.drop_column("events", "event_days")
    # Restoring the NOT NULL constraints on users would fail loudly if any
    # rows in the meantime have null values for those columns (e.g. Google
    # signups). Leave them nullable on downgrade; no data loss either way.
