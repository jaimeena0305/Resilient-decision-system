"""
app/models/schemas.py
─────────────────────────────────────────────────────────────────────────────
Pydantic v2 schemas for all API request/response payloads.

Design decisions:
  • ExecutionRequest accepts `input_data: dict[str, Any]` — the dynamic
    payload is validated against the workflow's JSON Schema at the
    orchestrator level, not here. This keeps the Pydantic layer thin
    and the workflow config the single source of truth for payload rules.
  • All responses include a full `audit_trail` list so callers can see
    exactly why a decision was made, in one API call.
  • `model_config = ConfigDict(from_attributes=True)` enables
    `.model_validate(orm_obj)` (previously `from_orm`).
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.db import AuditEventType, ExecutionStatus


# ── Shared config mixin ────────────────────────────────────────────────────

class ORMBase(BaseModel):
    """Enables ORM-mode for all response schemas."""
    model_config = ConfigDict(from_attributes=True)


# ══════════════════════════════════════════════════════════════════════════
#  REQUEST SCHEMAS
# ══════════════════════════════════════════════════════════════════════════

class ExecutionRequest(BaseModel):
    """
    POST /executions

    `workflow_id`  — must match a YAML file in app/workflows/.
    `input_data`   — arbitrary JSON; validated against the workflow's
                     input_schema after the request is accepted.

    The client MUST supply `X-Request-ID` as an HTTP header (enforced
    in middleware). It is echoed back in every response.
    """
    workflow_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="ID of the workflow to run (must match a loaded config)",
        examples=["loan_approval_v1"],
    )
    input_data: Dict[str, Any] = Field(
        ...,
        description="Business payload — validated against the workflow's JSON Schema",
        examples=[{
            "applicant_id": "APP001",
            "full_name": "Jane Doe",
            "age": 32,
            "annual_income": 75000,
            "requested_amount": 15000,
            "employment_status": "employed",
            "existing_debt": 5000,
        }],
    )

    @field_validator("workflow_id")
    @classmethod
    def no_path_traversal(cls, v: str) -> str:
        """Prevent directory traversal in workflow_id (used to build file paths)."""
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError("workflow_id must not contain path separators or '..'")
        return v.strip()


# ══════════════════════════════════════════════════════════════════════════
#  AUDIT LOG SCHEMAS
# ══════════════════════════════════════════════════════════════════════════

class AuditLogEntry(ORMBase):
    """
    A single step in the decision trail.
    Returned nested inside ExecutionResponse so the caller gets
    the full story in one HTTP round trip.
    """
    id:               str
    sequence_number:  int
    event_type:       AuditEventType
    stage_id:         Optional[str]    = None
    rule_id:          Optional[str]    = None

    # Rule evaluation detail
    field_path:       Optional[str]    = None
    evaluated_value:  Optional[str]    = None
    expected_value:   Optional[str]    = None
    operator:         Optional[str]    = None
    result:           Optional[str]    = None   # "PASS" | "FAIL" | "ERROR"

    # Human-readable explanation
    message:          str
    extra_metadata:   Optional[Dict[str, Any]] = None

    # Timing
    created_at:       datetime
    duration_ms:      Optional[float]  = None


# ══════════════════════════════════════════════════════════════════════════
#  RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════

class DecisionTrace(BaseModel):
    """
    Summary block surfaced at the top of ExecutionResponse.
    Provides a human-readable "why" without forcing the caller
    to parse the full audit trail.
    """
    final_status:       ExecutionStatus
    total_stages:       int
    stages_passed:      int
    stages_failed:      int
    mandatory_failures: List[str] = Field(default_factory=list,
                                          description="rule_ids that failed as mandatory")
    soft_failures:      List[str] = Field(default_factory=list,
                                          description="rule_ids that failed as soft")
    forced_reviews:     List[str] = Field(default_factory=list,
                                          description="rule_ids that triggered force_manual_review")
    summary:            str       = Field(..., description="One-line plain-English summary")


class ExecutionResponse(ORMBase):
    """
    Full response returned after processing (or immediately for idempotency hits).
    Includes the decision trace + complete audit trail.
    """
    id:               str
    request_id:       str
    workflow_id:      str
    status:           ExecutionStatus
    current_stage:    Optional[str]              = None
    input_payload:    Dict[str, Any]
    context:          Dict[str, Any]             = Field(default_factory=dict)
    decision_trace:   Optional[Dict[str, Any]]   = None
    error_message:    Optional[str]              = None
    created_at:       datetime
    updated_at:       datetime
    completed_at:     Optional[datetime]         = None
    audit_trail:      List[AuditLogEntry]        = Field(default_factory=list)


class ExecutionSummary(ORMBase):
    """
    Lightweight response for list endpoints (no audit trail).
    Used by GET /executions (paginated list).
    """
    id:             str
    request_id:     str
    workflow_id:    str
    status:         ExecutionStatus
    current_stage:  Optional[str]   = None
    created_at:     datetime
    updated_at:     datetime
    completed_at:   Optional[datetime] = None


class IdempotencyHitResponse(BaseModel):
    """
    Returned when a duplicate X-Request-ID is detected.
    The client receives its original result without re-processing.
    """
    detail:       str = "Duplicate request detected — returning cached result."
    execution_id: str
    cached:       bool = True
    response:     ExecutionResponse


# ══════════════════════════════════════════════════════════════════════════
#  ERROR SCHEMAS
# ══════════════════════════════════════════════════════════════════════════

class ValidationErrorDetail(BaseModel):
    field:   str
    message: str


class ErrorResponse(BaseModel):
    """Standard error envelope for all 4xx/5xx responses."""
    error:   str
    detail:  Optional[str]                      = None
    fields:  Optional[List[ValidationErrorDetail]] = None
    request_id: Optional[str]                   = None
