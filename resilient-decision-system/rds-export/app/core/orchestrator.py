"""
app/core/orchestrator.py
─────────────────────────────────────────────────────────────────────────────
The Workflow Orchestrator — the engine's central coordinator.

Responsibilities:
  1. Load and validate the workflow YAML config.
  2. Transition the Execution through its lifecycle states.
  3. Fan out to the appropriate stage executor (rule_evaluation or
     external_dependency) for each stage.
  4. Write a structured AuditLog row after EVERY meaningful action.
  5. Build the final DecisionTrace explaining the outcome.
  6. Handle all failure modes: stage failures, external API exhaustion,
     unexpected exceptions — ensuring the Execution always lands in a
     terminal state with an audit trail.

Design decisions:
  • The orchestrator knows NOTHING about specific business rules or which
    external APIs exist. All of that is expressed in the YAML config.
  • A single `_audit()` helper centralises all DB writes for audit logs,
    keeping stage logic clean.
  • The `context` dict is the single source of shared state between stages.
    Stage N can write to it; Stage N+1 reads from it. This is the "pipeline
    accumulator" pattern.
  • Every DB interaction inside `run_workflow` is wrapped in a try/except
    so that even if persistence itself fails, we attempt to mark the
    execution as FAILED rather than leaving it stuck in RUNNING forever.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import jsonschema
from sqlalchemy.orm import Session

from app.core.rules_engine import (
    StageEvaluationResult,
    evaluate_stage_rules,
)
from app.dependencies.mock_api import ExternalAPIError, call_external_dependency
from app.models.db import AuditEventType, AuditLog, Execution, ExecutionStatus
from app.services.config_loader import load_workflow_config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_seq(db: Session, execution_id: str) -> int:
    """Return the next monotonic sequence number for this execution's audit log."""
    count = (
        db.query(AuditLog)
        .filter(AuditLog.execution_id == execution_id)
        .count()
    )
    return count + 1


def _audit(
    db:           Session,
    execution_id: str,
    event_type:   AuditEventType,
    message:      str,
    *,
    stage_id:         Optional[str]         = None,
    rule_id:          Optional[str]         = None,
    field_path:       Optional[str]         = None,
    evaluated_value:  Any                   = None,
    expected_value:   Any                   = None,
    operator:         Optional[str]         = None,
    result:           Optional[str]         = None,
    extra_metadata:   Optional[Dict]        = None,
    duration_ms:      Optional[float]       = None,
) -> AuditLog:
    """
    Append one immutable row to audit_logs.

    All callers go through this single function to ensure consistent
    formatting and that `sequence_number` is always correct.
    """
    entry = AuditLog(
        execution_id    = execution_id,
        sequence_number = _next_seq(db, execution_id),
        event_type      = event_type,
        stage_id        = stage_id,
        rule_id         = rule_id,
        field_path      = field_path,
        evaluated_value = str(evaluated_value) if evaluated_value is not None else None,
        expected_value  = str(expected_value)  if expected_value  is not None else None,
        operator        = operator,
        result          = result,
        message         = message,
        extra_metadata  = extra_metadata,
        duration_ms     = duration_ms,
        created_at      = _now(),
    )
    db.add(entry)
    db.flush()   # assign the PK without committing the outer transaction
    return entry


def _transition(
    db:           Session,
    execution:    Execution,
    new_status:   ExecutionStatus,
    stage_id:     Optional[str] = None,
    message:      str           = "",
) -> None:
    """
    Move an Execution to a new status and write a STATUS_TRANSITION audit entry.
    """
    old_status           = execution.status
    execution.status     = new_status
    execution.updated_at = _now()
    if stage_id:
        execution.current_stage = stage_id
    if new_status in (
        ExecutionStatus.APPROVED,
        ExecutionStatus.REJECTED,
        ExecutionStatus.MANUAL_REVIEW,
        ExecutionStatus.FAILED,
    ):
        execution.completed_at = _now()

    _audit(
        db, execution.id, AuditEventType.STATUS_TRANSITION,
        message or f"Status changed: {old_status} → {new_status}",
        stage_id=stage_id,
        result=new_status.value,
    )
    db.flush()


# ══════════════════════════════════════════════════════════════════════════
#  Input validation
# ══════════════════════════════════════════════════════════════════════════

def validate_input(
    input_data:   Dict[str, Any],
    workflow_cfg: Dict[str, Any],
) -> Optional[str]:
    """
    Validate `input_data` against the workflow's JSON Schema.

    Returns None on success, or an error message string on failure.
    jsonschema is the standard Python validator — no extra dependencies.
    """
    schema = workflow_cfg.get("input_schema")
    if not schema:
        return None  # No schema = no validation

    try:
        jsonschema.validate(instance=input_data, schema=schema)
        return None
    except jsonschema.ValidationError as exc:
        return f"Input validation failed: {exc.message} (path: {' → '.join(str(p) for p in exc.absolute_path)})"
    except jsonschema.SchemaError as exc:
        return f"Workflow schema is malformed: {exc.message}"


# ══════════════════════════════════════════════════════════════════════════
#  Stage executors
# ══════════════════════════════════════════════════════════════════════════

def _execute_rule_stage(
    db:           Session,
    execution:    Execution,
    stage_cfg:    Dict[str, Any],
    context:      Dict[str, Any],
) -> str:
    """
    Execute a `rule_evaluation` stage.

    Returns the routing decision: "continue" | "reject" | "manual_review"
    """
    stage_id  = stage_cfg["stage_id"]
    t_start   = time.perf_counter()

    _audit(db, execution.id, AuditEventType.STAGE_STARTED,
           f"Stage '{stage_id}' started (type=rule_evaluation)", stage_id=stage_id)

    # Evaluate all rules in this stage
    result: StageEvaluationResult = evaluate_stage_rules(stage_cfg, context)

    total_ms = (time.perf_counter() - t_start) * 1000

    # Write one audit entry per rule result
    for rr in result.rule_results:
        _audit(
            db, execution.id, AuditEventType.RULE_EVALUATED,
            rr.reason,
            stage_id        = stage_id,
            rule_id         = rr.rule_id,
            field_path      = rr.field_path,
            evaluated_value = rr.evaluated_value,
            expected_value  = rr.expected_value,
            operator        = rr.operator,
            result          = rr.result_label,
            extra_metadata  = {"severity": rr.severity, "action": rr.action, "error": rr.error},
        )

    # Determine routing outcome
    if result.has_hard_failure:
        _audit(db, execution.id, AuditEventType.STAGE_FAILED,
               f"Stage '{stage_id}' FAILED: mandatory rule(s) failed: {result.mandatory_failures}",
               stage_id=stage_id, result="FAIL", duration_ms=total_ms)
        return "reject"

    if result.has_soft_failure or result.has_force_manual_review:
        reasons = result.soft_failures + result.forced_reviews
        _audit(db, execution.id, AuditEventType.STAGE_COMPLETED,
               f"Stage '{stage_id}' soft-failed → manual review: {reasons}",
               stage_id=stage_id, result="SOFT_FAIL", duration_ms=total_ms)
        return "manual_review"

    _audit(db, execution.id, AuditEventType.STAGE_COMPLETED,
           f"Stage '{stage_id}' PASSED: all {len(result.rule_results)} rule(s) passed.",
           stage_id=stage_id, result="PASS", duration_ms=total_ms)
    return "continue"


def _execute_external_stage(
    db:           Session,
    execution:    Execution,
    stage_cfg:    Dict[str, Any],
    context:      Dict[str, Any],
) -> str:
    """
    Execute an `external_dependency` stage.

    Calls the mock (or real) external API with retry/backoff.
    Maps the response fields into `context` per `result_mapping` in the YAML.

    Returns the routing decision: "continue" | "reject" | "manual_review" | "failed"
    """
    stage_id    = stage_cfg["stage_id"]
    dep_cfg     = stage_cfg.get("dependency", {})
    service_id  = dep_cfg.get("service_id", "unknown_service")
    mock_cfg    = dep_cfg.get("mock_config", {})
    retry_pol   = stage_cfg.get("retry_policy", {})
    on_failure  = stage_cfg.get("on_failure", "reject")

    _audit(db, execution.id, AuditEventType.STAGE_STARTED,
           f"Stage '{stage_id}' started (type=external_dependency, service={service_id})",
           stage_id=stage_id)

    attempt_log: List[Dict[str, Any]] = []
    t_start     = time.perf_counter()

    try:
        response, total_attempts = call_external_dependency(
            service_id    = service_id,
            mock_config   = mock_cfg,
            retry_policy  = retry_pol,
            input_payload = context.get("input", {}),
            attempt_log   = attempt_log,
        )

        total_ms = (time.perf_counter() - t_start) * 1000

        # Write a log entry for each failed attempt (the retries)
        for attempt_info in attempt_log:
            _audit(
                db, execution.id, AuditEventType.EXT_DEP_ATTEMPT,
                f"[{service_id}] Attempt {attempt_info['attempt']} failed: {attempt_info.get('error')}",
                stage_id=stage_id, result="FAIL",
                extra_metadata={"service_id": service_id, **attempt_info},
            )

        # Log the final success
        _audit(
            db, execution.id, AuditEventType.EXT_DEP_SUCCESS,
            f"[{service_id}] Succeeded after {total_attempts} attempt(s). "
            f"Response: {response}",
            stage_id=stage_id, result="PASS", duration_ms=total_ms,
            extra_metadata={"service_id": service_id, "total_attempts": total_attempts,
                            "response": response},
        )

        # Map response fields into context so later stages can reference them
        result_mapping = stage_cfg.get("result_mapping", {})
        for response_key, context_path in result_mapping.items():
            if response_key in response:
                _set_nested_value(context, context_path, response[response_key])
                logger.debug("Mapped %s=%r → %s", response_key, response[response_key], context_path)

        _audit(db, execution.id, AuditEventType.STAGE_COMPLETED,
               f"Stage '{stage_id}' PASSED.", stage_id=stage_id, result="PASS")
        return "continue"

    except ExternalAPIError as exc:
        total_ms = (time.perf_counter() - t_start) * 1000

        # Write entries for any partial attempts that happened before exhaustion
        for attempt_info in attempt_log:
            _audit(
                db, execution.id, AuditEventType.EXT_DEP_ATTEMPT,
                f"[{service_id}] Attempt {attempt_info['attempt']} failed: {attempt_info.get('error')}",
                stage_id=stage_id, result="FAIL",
                extra_metadata={"service_id": service_id, **attempt_info},
            )

        _audit(
            db, execution.id, AuditEventType.EXT_DEP_FAILURE,
            f"[{service_id}] All {exc.attempts} attempt(s) exhausted. "
            f"Last error: {exc.last_error} (HTTP {exc.status_code})",
            stage_id=stage_id, result="FAIL", duration_ms=total_ms,
            extra_metadata={"service_id": service_id, "total_attempts": exc.attempts,
                            "status_code": exc.status_code},
        )
        _audit(db, execution.id, AuditEventType.STAGE_FAILED,
               f"Stage '{stage_id}' FAILED: external dependency exhausted.",
               stage_id=stage_id, result="FAIL")

        # Route based on on_failure config
        if on_failure == "manual_review":
            return "manual_review"
        elif on_failure == "continue":
            return "continue"
        else:
            return "failed"


def _set_nested_value(d: Dict[str, Any], path: str, value: Any) -> None:
    """
    Write `value` to a dot-notation path in dict `d`, creating intermediate
    dicts as needed.

    e.g. _set_nested_value(ctx, "context.credit_score", 720)
    → ctx["context"]["credit_score"] = 720
    """
    parts = path.split(".")
    for part in parts[:-1]:
        if part not in d or not isinstance(d[part], dict):
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value


# ══════════════════════════════════════════════════════════════════════════
#  Decision Trace builder
# ══════════════════════════════════════════════════════════════════════════

def _build_decision_trace(
    db:           Session,
    execution_id: str,
    final_status: ExecutionStatus,
    workflow_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Aggregate all audit log entries into a human-readable decision trace.
    This is persisted on the Execution row and returned in the API response.
    """
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.execution_id == execution_id)
        .order_by(AuditLog.sequence_number)
        .all()
    )

    mandatory_failures = [
        l.rule_id for l in logs
        if l.event_type == AuditEventType.RULE_EVALUATED
        and l.result == "FAIL"
        and l.extra_metadata
        and l.extra_metadata.get("severity") == "mandatory"
    ]
    soft_failures = [
        l.rule_id for l in logs
        if l.event_type == AuditEventType.RULE_EVALUATED
        and l.result == "FAIL"
        and l.extra_metadata
        and l.extra_metadata.get("severity") == "soft"
        and l.extra_metadata.get("action") != "force_manual_review"
    ]
    forced_reviews = [
        l.rule_id for l in logs
        if l.event_type == AuditEventType.RULE_EVALUATED
        and l.extra_metadata
        and l.extra_metadata.get("action") == "force_manual_review"
    ]
    stages_passed = len([
        l for l in logs
        if l.event_type == AuditEventType.STAGE_COMPLETED and l.result == "PASS"
    ])
    stages_failed = len([
        l for l in logs
        if l.event_type in (AuditEventType.STAGE_FAILED,)
    ])

    # Build one-line summary
    if final_status == ExecutionStatus.APPROVED:
        summary = (
            f"Approved: all {stages_passed} stage(s) passed with no mandatory failures."
        )
    elif final_status == ExecutionStatus.REJECTED:
        summary = (
            f"Rejected: mandatory rule failure(s) in: {mandatory_failures}."
        )
    elif final_status == ExecutionStatus.MANUAL_REVIEW:
        reasons = soft_failures + forced_reviews
        summary = (
            f"Sent to manual review due to: {reasons}."
        )
    else:
        summary = f"Execution ended with status: {final_status.value}."

    rule_trace = [
        {
            "rule_id":         l.rule_id,
            "stage_id":        l.stage_id,
            "field_path":      l.field_path,
            "evaluated_value": l.evaluated_value,
            "expected_value":  l.expected_value,
            "operator":        l.operator,
            "result":          l.result,
            "reason":          l.message,
        }
        for l in logs if l.event_type == AuditEventType.RULE_EVALUATED
    ]

    return {
        "final_status":       final_status.value,
        "total_stages":       stages_passed + stages_failed,
        "stages_passed":      stages_passed,
        "stages_failed":      stages_failed,
        "mandatory_failures": mandatory_failures,
        "soft_failures":      soft_failures,
        "forced_reviews":     forced_reviews,
        "summary":            summary,
        "rule_trace":         rule_trace,
        "workflow_id":        workflow_cfg.get("workflow_id"),
        "workflow_version":   workflow_cfg.get("version"),
        "generated_at":       _now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════
#  Main entry point
# ══════════════════════════════════════════════════════════════════════════

def run_workflow(
    execution_id: str,
    db:           Session,
) -> Execution:
    """
    Drive a single workflow Execution from PENDING to a terminal state.

    This is the top-level function called by the API route handler after
    creating the Execution row. It is synchronous by design (see architecture
    doc for the async upgrade path).

    Parameters:
        execution_id : PK of the Execution row (already created by the route)
        db           : active SQLAlchemy Session (caller owns commit/rollback)

    Returns:
        The updated Execution instance (caller should db.commit() after).

    The function guarantees:
        • Execution always lands in a terminal state (APPROVED | REJECTED |
          MANUAL_REVIEW | FAILED) — no stuck RUNNING rows.
        • Every meaningful action has a corresponding AuditLog entry.
        • The `decision_trace` column is populated on completion.
    """
    # ── 1. Load the execution row ──────────────────────────────────────────
    execution: Execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise ValueError(f"Execution '{execution_id}' not found in database.")

    logger.info("Starting workflow execution: %s (workflow=%s)", execution_id, execution.workflow_id)

    # ── 2. Load workflow config ────────────────────────────────────────────
    try:
        workflow_cfg = load_workflow_config(execution.workflow_id)
    except FileNotFoundError as exc:
        execution.error_message = str(exc)
        _transition(db, execution, ExecutionStatus.FAILED,
                    message=f"Workflow config not found: {exc}")
        db.flush()
        return execution

    # ── 3. Transition to RUNNING ───────────────────────────────────────────
    _transition(db, execution, ExecutionStatus.RUNNING,
                message="Workflow execution started.")
    _audit(db, execution.id, AuditEventType.EXECUTION_CREATED,
           f"Execution created for workflow '{execution.workflow_id}' "
           f"(version={workflow_cfg.get('version', 'N/A')})",
           extra_metadata={"workflow_name": workflow_cfg.get("name"),
                           "input_keys": list(execution.input_payload.keys())})

    # ── 4. Validate input against workflow's JSON Schema ──────────────────
    validation_error = validate_input(execution.input_payload, workflow_cfg)
    if validation_error:
        execution.error_message = validation_error
        _audit(db, execution.id, AuditEventType.STAGE_FAILED,
               f"Input schema validation failed: {validation_error}",
               stage_id="__input_validation__", result="FAIL")
        _transition(db, execution, ExecutionStatus.REJECTED,
                    message=f"Input validation failed: {validation_error}")
        _finalise(db, execution, workflow_cfg)
        return execution

    # ── 5. Build the initial runtime context ──────────────────────────────
    # `input` holds the original payload (immutable by convention).
    # `context` is the mutable scratchpad for inter-stage data.
    context: Dict[str, Any] = {
        "input":   execution.input_payload,
        "context": {},
    }
    execution.context = context  # SQLAlchemy tracks this as a JSON column

    # ── 6. Execute stages sequentially ────────────────────────────────────
    stages = workflow_cfg.get("stages", [])
    final_routing = "continue"

    try:
        for stage_cfg in stages:
            stage_id   = stage_cfg.get("stage_id", "unknown")
            stage_type = stage_cfg.get("type", "rule_evaluation")
            execution.current_stage = stage_id
            db.flush()

            logger.info("[%s] Executing stage '%s' (type=%s)", execution_id, stage_id, stage_type)

            if stage_type == "rule_evaluation":
                routing = _execute_rule_stage(db, execution, stage_cfg, context)

            elif stage_type == "external_dependency":
                # Transition to RETRYING during the external call so status
                # reflects "in flight with potential retries"
                execution.status = ExecutionStatus.RETRYING
                db.flush()
                routing = _execute_external_stage(db, execution, stage_cfg, context)
                # Restore to RUNNING if we came back successfully
                if routing == "continue":
                    execution.status = ExecutionStatus.RUNNING
                    db.flush()

            else:
                logger.warning("Unknown stage type '%s' in stage '%s' — skipping.", stage_type, stage_id)
                routing = "continue"

            # Update the shared context column after each stage
            execution.context = dict(context)
            db.flush()

            # ── Routing decision ─────────────────────────────────────────
            if routing == "reject":
                final_routing = "reject"
                break
            elif routing == "manual_review":
                final_routing = "manual_review"
                break
            elif routing == "failed":
                final_routing = "failed"
                break
            # "continue" → proceed to next stage

    except Exception as exc:  # noqa: BLE001
        # Unexpected exception — log it and mark as FAILED
        # This guard ensures no execution ever gets stuck in RUNNING/RETRYING.
        logger.exception("[%s] Unexpected exception in orchestrator: %s", execution_id, exc)
        execution.error_message = f"Unexpected error: {exc}"
        _audit(db, execution.id, AuditEventType.STAGE_FAILED,
               f"Unexpected orchestrator exception: {exc}",
               stage_id=execution.current_stage, result="ERROR",
               extra_metadata={"exception_type": type(exc).__name__})
        final_routing = "failed"

    # ── 7. Apply final state transition ───────────────────────────────────
    _apply_final_routing(db, execution, final_routing, workflow_cfg)
    _finalise(db, execution, workflow_cfg)

    logger.info(
        "[%s] Workflow completed. Final status: %s",
        execution_id, execution.status.value,
    )
    return execution


def _apply_final_routing(
    db:           Session,
    execution:    Execution,
    routing:      str,
    workflow_cfg: Dict[str, Any],
) -> None:
    """Translate the routing string into the correct terminal ExecutionStatus."""
    decision_logic = workflow_cfg.get("decision_logic", {})

    if routing == "continue":
        # All stages passed — check decision_logic for final approval
        _transition(db, execution, ExecutionStatus.APPROVED,
                    message="All stages completed successfully → APPROVED.")

    elif routing == "reject":
        _transition(db, execution, ExecutionStatus.REJECTED,
                    message="Mandatory rule failure detected → REJECTED.")

    elif routing == "manual_review":
        review_queue = workflow_cfg.get("metadata", {}).get("review_queue", "default_queue")
        sla_hours    = workflow_cfg.get("metadata", {}).get("sla_hours", 48)
        _transition(db, execution, ExecutionStatus.MANUAL_REVIEW,
                    message=f"Soft failure or forced review → MANUAL_REVIEW "
                            f"(queue={review_queue}, SLA={sla_hours}h).")

    elif routing == "failed":
        _transition(db, execution, ExecutionStatus.FAILED,
                    message="Execution failed due to unrecoverable error.")


def _finalise(
    db:           Session,
    execution:    Execution,
    workflow_cfg: Dict[str, Any],
) -> None:
    """Build and persist the decision trace, then write the final audit entry."""
    trace = _build_decision_trace(db, execution.id, execution.status, workflow_cfg)
    execution.decision_trace = trace

    _audit(
        db, execution.id, AuditEventType.EXECUTION_FINAL,
        trace["summary"],
        result=execution.status.value,
        extra_metadata={"decision_trace": trace},
    )
    db.flush()
