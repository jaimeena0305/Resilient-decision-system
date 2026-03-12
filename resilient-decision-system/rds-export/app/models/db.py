"""
app/models/db.py
─────────────────────────────────────────────────────────────────────────────
Database layer — SQLAlchemy ORM models.

Design decisions:
  • Sync SQLAlchemy (not async) — simpler error stack for a hackathon;
    trivially switchable to AsyncSession later by swapping the engine/session.
  • All primary keys are UUID strings — avoids auto-increment collisions
    when the schema is sharded later.
  • JSON columns (SQLAlchemy's JSON type) map to TEXT in SQLite and JSONB
    in PostgreSQL — zero migration cost when moving up the stack.
  • Audit logs are append-only. No UPDATE is ever issued against that table.
    This preserves a tamper-evident trail.
─────────────────────────────────────────────────────────────────────────────
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Integer,
    Float,
    JSON,
    Enum as SAEnum,
    ForeignKey,
    Index,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
import enum

# ── Shared base class ──────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """All ORM models inherit from this single declarative base."""
    pass


# ── Enumerations (mirrored in Pydantic schemas) ────────────────────────────

class ExecutionStatus(str, enum.Enum):
    """
    Lifecycle states of a single workflow execution.

    State machine (valid transitions):
        PENDING   → RUNNING
        RUNNING   → APPROVED | REJECTED | MANUAL_REVIEW | FAILED | RETRYING
        RETRYING  → RUNNING  (after backoff delay)
        FAILED    → (terminal — no further transitions)
        APPROVED  → (terminal)
        REJECTED  → (terminal)
        MANUAL_REVIEW → (terminal for the engine; human action happens outside)
    """
    PENDING       = "PENDING"
    RUNNING       = "RUNNING"
    APPROVED      = "APPROVED"
    REJECTED      = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RETRYING      = "RETRYING"
    FAILED        = "FAILED"


class AuditEventType(str, enum.Enum):
    """
    Granular event types written to the audit log.
    Every meaningful action in the engine maps to exactly one event type.
    """
    EXECUTION_CREATED   = "EXECUTION_CREATED"
    STAGE_STARTED       = "STAGE_STARTED"
    STAGE_COMPLETED     = "STAGE_COMPLETED"
    STAGE_FAILED        = "STAGE_FAILED"
    RULE_EVALUATED      = "RULE_EVALUATED"
    EXT_DEP_ATTEMPT     = "EXT_DEP_ATTEMPT"       # Each retry attempt
    EXT_DEP_SUCCESS     = "EXT_DEP_SUCCESS"
    EXT_DEP_FAILURE     = "EXT_DEP_FAILURE"        # After all retries exhausted
    STATUS_TRANSITION   = "STATUS_TRANSITION"
    IDEMPOTENCY_HIT     = "IDEMPOTENCY_HIT"        # Duplicate request detected
    EXECUTION_FINAL     = "EXECUTION_FINAL"


# ── ORM Models ─────────────────────────────────────────────────────────────

class Execution(Base):
    """
    Represents a single running instance of a workflow.

    One WorkflowConfig blueprint → many Execution instances.

    `input_payload`  : the raw JSON the client submitted.
    `context`        : mutable scratch-space built up during execution
                       (e.g., credit_score fetched from an external API is
                       written here so later stages can reference it).
    `decision_trace` : final summary of which rules fired and why.
    """
    __tablename__ = "executions"

    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id      = Column(String(255), unique=True, nullable=False, index=True,
                             comment="Client-supplied X-Request-ID for idempotency")
    workflow_id     = Column(String(100), nullable=False, index=True,
                             comment="References the workflow YAML config ID")
    status          = Column(SAEnum(ExecutionStatus), nullable=False,
                             default=ExecutionStatus.PENDING, index=True)

    # ── Payload columns ──────────────────────────────────────────────────
    input_payload   = Column(JSON, nullable=False,
                             comment="Original, unmodified client input")
    context         = Column(JSON, nullable=False, default=dict,
                             comment="Runtime context built up across stages")
    decision_trace  = Column(JSON, nullable=True,
                             comment="Human-readable summary of the final decision")

    # ── Timing ───────────────────────────────────────────────────────────
    created_at      = Column(DateTime(timezone=True), nullable=False,
                             default=lambda: datetime.now(timezone.utc))
    updated_at      = Column(DateTime(timezone=True), nullable=False,
                             default=lambda: datetime.now(timezone.utc),
                             onupdate=lambda: datetime.now(timezone.utc))
    completed_at    = Column(DateTime(timezone=True), nullable=True)

    # ── Stage tracking ────────────────────────────────────────────────────
    current_stage   = Column(String(100), nullable=True,
                             comment="The stage_id currently being executed")
    error_message   = Column(Text, nullable=True,
                             comment="Last unrecoverable error, if any")

    # ── Relationship ─────────────────────────────────────────────────────
    audit_logs = relationship(
        "AuditLog",
        back_populates="execution",
        order_by="AuditLog.sequence_number",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Execution id={self.id!r} workflow={self.workflow_id!r} status={self.status!r}>"


class AuditLog(Base):
    """
    Immutable, append-only record of every decision step.

    Design rule: NEVER UPDATE a row in this table.
    INSERT only. This table is the ground truth of "what the engine did."

    `sequence_number` : monotonically increasing per execution, allows
                        deterministic replay of the decision chain.
    `rule_id`         : populated only for RULE_EVALUATED events.
    `field_path`      : the JSON path that was evaluated (e.g. "input.age").
    `evaluated_value` : the actual runtime value (stored as string for portability).
    `expected_value`  : the threshold/expected value from the rule config.
    `operator`        : the comparison operator used (gte, lte, equals, …).
    `result`          : "PASS" | "FAIL" | "ERROR" | "SKIP"
    `metadata`        : arbitrary JSON for extra context (retry counts, HTTP
                        status codes, latency, etc.).
    """
    __tablename__ = "audit_logs"

    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    execution_id    = Column(String(36), ForeignKey("executions.id"), nullable=False, index=True)
    sequence_number = Column(Integer,   nullable=False,
                             comment="Ordering within this execution's log")
    event_type      = Column(SAEnum(AuditEventType), nullable=False)
    stage_id        = Column(String(100), nullable=True)
    rule_id         = Column(String(100), nullable=True)

    # ── Evaluation detail (rule-specific) ────────────────────────────────
    field_path      = Column(String(255), nullable=True)
    evaluated_value = Column(String(512), nullable=True)
    expected_value  = Column(String(512), nullable=True)
    operator        = Column(String(50),  nullable=True)
    result          = Column(String(20),  nullable=True)   # PASS | FAIL | ERROR

    # ── Human-readable explanation ────────────────────────────────────────
    message         = Column(Text, nullable=False, default="")
    extra_metadata  = Column(JSON, nullable=True,
                             comment="Retry counts, HTTP codes, latency, etc.")

    # ── Timing ───────────────────────────────────────────────────────────
    created_at      = Column(DateTime(timezone=True), nullable=False,
                             default=lambda: datetime.now(timezone.utc))
    duration_ms     = Column(Float, nullable=True,
                             comment="How long this step took in milliseconds")

    # ── Relationship ─────────────────────────────────────────────────────
    execution = relationship("Execution", back_populates="audit_logs")

    # ── Composite index for fast per-execution trace queries ──────────────
    __table_args__ = (
        Index("ix_audit_execution_seq", "execution_id", "sequence_number"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog exec={self.execution_id!r} seq={self.sequence_number} "
            f"event={self.event_type!r} result={self.result!r}>"
        )


# ── Database bootstrap helpers ─────────────────────────────────────────────

def get_engine(database_url: str = "sqlite:///./decisions.db"):
    """
    Create and return a SQLAlchemy engine.

    SQLite:     database_url = "sqlite:///./decisions.db"
    PostgreSQL: database_url = "postgresql://user:pass@host/dbname"

    connect_args is SQLite-specific; safe to drop for Postgres.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, echo=False)


def get_session_factory(engine):
    """Return a configured SessionLocal factory."""
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db(engine):
    """Create all tables. Call once at startup (idempotent via CREATE IF NOT EXISTS)."""
    Base.metadata.create_all(bind=engine)
