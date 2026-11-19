"""initial schema: sanctions lists, screenings, cases, append-only audit log

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Trigram similarity + GIN operator class for fuzzy candidate retrieval.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "list_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("entity_count", sa.Integer, nullable=False),
        sa.Column("name_count", sa.Integer, nullable=False),
        sa.Column("ingested_at", TS, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "sanctioned_entities",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("primary_name", sa.Text, nullable=False),
        sa.Column("programs", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("countries", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("dobs", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("remarks", sa.Text),
        sa.Column("list_version", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source", "source_id", name="uq_entity_source_id"),
    )

    op.create_table(
        "entity_names",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("entity_id", sa.Integer, sa.ForeignKey("sanctioned_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("name_type", sa.String(16), nullable=False),
    )
    op.create_index("ix_entity_names_entity_id", "entity_names", ["entity_id"])
    op.create_index(
        "ix_entity_names_norm_trgm", "entity_names", ["name_norm"],
        postgresql_using="gin", postgresql_ops={"name_norm": "gin_trgm_ops"},
    )

    op.create_table(
        "screenings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("query_name", sa.Text, nullable=False),
        sa.Column("query_type", sa.String(16)),
        sa.Column("query_dob", sa.String(10)),
        sa.Column("query_country", sa.Text),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("top_score", sa.Float, nullable=False),
        sa.Column("hit_count", sa.Integer, nullable=False),
        sa.Column("latency_ms", sa.Float, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "screening_hits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("screening_id", sa.Integer, sa.ForeignKey("screenings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.Integer, sa.ForeignKey("sanctioned_entities.id"), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("matched_name", sa.Text, nullable=False),
        sa.Column("explanation", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_screening_hits_screening_id", "screening_hits", ["screening_id"])

    op.create_table(
        "cases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("screening_id", sa.Integer, sa.ForeignKey("screenings.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("top_score", sa.Float, nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("summary_source", sa.String(16)),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'escalated', 'cleared', 'confirmed_match')", name="ck_case_status"),
    )
    op.create_index("ix_cases_status", "cases", ["status"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("case_id", sa.Integer, sa.ForeignKey("cases.id")),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20)),
        sa.Column("note", sa.Text),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_log_case_id", "audit_log", ["case_id"])

    # Append-only audit log, enforced by the database itself (not just app code).
    op.execute(
        """
        CREATE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_change()")
    for table in ("audit_log", "cases", "screening_hits", "screenings", "entity_names",
                  "sanctioned_entities", "list_versions"):
        op.drop_table(table)
