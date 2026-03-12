"""
app/core/rules_engine.py
─────────────────────────────────────────────────────────────────────────────
The Rules Engine — pure, stateless evaluation logic.

Responsibilities:
  1. Parse a single rule definition (from the YAML config dict).
  2. Extract the value to evaluate from a nested JSON context dict.
  3. Apply the configured operator.
  4. Return a typed RuleResult (PASS | FAIL | ERROR) with full audit detail.

Design decisions:
  • ZERO side effects — the engine never writes to the DB. That is the
    orchestrator's job. This makes the engine trivially unit-testable.
  • Operators are a registry (OPERATOR_REGISTRY dict), not a chain of
    if/elif statements. Adding a new operator = adding one function.
  • `field_expression` (arithmetic) is evaluated via a safe sandbox
    that only allows simple arithmetic on context values — no `eval()`
    on raw user input.
  • Soft-fail routing logic is handled HERE (not in the orchestrator),
    because it is a property of the rule, not the workflow structure.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import operator as op
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  Data classes for rule evaluation results
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class RuleResult:
    """
    The outcome of evaluating a single rule.

    Attributes:
        rule_id         : from the YAML config
        passed          : True = rule condition satisfied
        severity        : "mandatory" | "soft"
        action          : optional special action (e.g. "force_manual_review")
        field_path      : the resolved path that was evaluated
        evaluated_value : the actual runtime value (for audit logging)
        expected_value  : the threshold / expected value (for audit logging)
        operator        : the operator string (for audit logging)
        reason          : human-readable explanation
        error           : set if evaluation itself threw an exception
    """
    rule_id:         str
    passed:          bool
    severity:        str           = "mandatory"
    action:          Optional[str] = None
    field_path:      str           = ""
    evaluated_value: Any           = None
    expected_value:  Any           = None
    operator:        str           = ""
    reason:          str           = ""
    error:           Optional[str] = None

    @property
    def result_label(self) -> str:
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"

    @property
    def triggers_manual_review(self) -> bool:
        """True when this rule should route to manual review (not hard reject)."""
        if self.passed:
            return False
        if self.action == "force_manual_review":
            return True
        if self.severity == "soft":
            return True
        return False

    @property
    def triggers_hard_reject(self) -> bool:
        """True when this failure should immediately reject the execution."""
        if self.passed:
            return False
        return self.severity == "mandatory" and self.action != "force_manual_review"


@dataclass
class StageEvaluationResult:
    """
    Aggregate result of evaluating ALL rules in a single stage.

    The orchestrator uses this to decide the next state transition.
    """
    stage_id:          str
    rule_results:      List[RuleResult] = field(default_factory=list)

    @property
    def has_hard_failure(self) -> bool:
        return any(r.triggers_hard_reject for r in self.rule_results)

    @property
    def has_soft_failure(self) -> bool:
        return any(r.triggers_manual_review for r in self.rule_results)

    @property
    def has_force_manual_review(self) -> bool:
        return any(r.action == "force_manual_review" and not r.passed for r in self.rule_results)

    @property
    def mandatory_failures(self) -> List[str]:
        return [r.rule_id for r in self.rule_results if r.triggers_hard_reject]

    @property
    def soft_failures(self) -> List[str]:
        return [r.rule_id for r in self.rule_results
                if r.triggers_manual_review and r.action != "force_manual_review"]

    @property
    def forced_reviews(self) -> List[str]:
        return [r.rule_id for r in self.rule_results if r.action == "force_manual_review" and not r.passed]

    @property
    def all_passed(self) -> bool:
        return not self.has_hard_failure and not self.has_soft_failure and not self.has_force_manual_review


# ══════════════════════════════════════════════════════════════════════════
#  Operator Registry
# ══════════════════════════════════════════════════════════════════════════
# Each operator is a function: (actual_value, threshold) -> bool
# To add a new operator, just add an entry here. Zero code changes elsewhere.

def _op_in(value: Any, threshold: Any) -> bool:
    return value in threshold


def _op_not_in(value: Any, threshold: Any) -> bool:
    return value not in threshold


def _op_contains(value: Any, threshold: Any) -> bool:
    return threshold in str(value)


def _op_startswith(value: Any, threshold: Any) -> bool:
    return str(value).startswith(str(threshold))


def _op_regex(value: Any, threshold: Any) -> bool:
    return bool(re.match(str(threshold), str(value)))


OPERATOR_REGISTRY: Dict[str, Callable[[Any, Any], bool]] = {
    # Numeric comparisons
    "gt":           op.gt,
    "gte":          op.ge,
    "lt":           op.lt,
    "lte":          op.le,
    "equals":       op.eq,
    "not_equals":   op.ne,
    # Membership
    "in":           _op_in,
    "not_in":       _op_not_in,
    # String
    "contains":     _op_contains,
    "startswith":   _op_startswith,
    "regex":        _op_regex,
    # Boolean
    "is_true":      lambda v, _: v is True or v == "true" or v == 1,
    "is_false":     lambda v, _: v is False or v == "false" or v == 0,
    "is_null":      lambda v, _: v is None,
    "is_not_null":  lambda v, _: v is not None,
}


# ══════════════════════════════════════════════════════════════════════════
#  Value extraction helpers
# ══════════════════════════════════════════════════════════════════════════

def _get_nested_value(context: Dict[str, Any], path: str) -> Any:
    """
    Resolve a dot-notation path from the runtime context.

    Examples:
        path = "input.age"           → context["input"]["age"]
        path = "context.credit_score"→ context["context"]["credit_score"]
        path = "input.address.city"  → context["input"]["address"]["city"]

    Raises KeyError if any segment is missing.
    """
    parts = path.split(".")
    current = context
    for part in parts:
        if not isinstance(current, dict):
            raise KeyError(f"Cannot traverse into non-dict at segment '{part}' of path '{path}'")
        if part not in current:
            raise KeyError(f"Key '{part}' not found while resolving path '{path}'")
        current = current[part]
    return current


# Safe arithmetic operators for field_expression evaluation
_SAFE_ARITHMETIC_OPS = {
    "+": op.add,
    "-": op.sub,
    "*": op.mul,
    "/": op.truediv,
    "%": op.mod,
}

# Matches expressions like: "input.field_a / input.field_b"
_EXPR_PATTERN = re.compile(
    r"^([\w.]+)\s*([+\-*/%])\s*([\w.]+)$"
)


def _evaluate_field_expression(expression: str, context: Dict[str, Any]) -> float:
    """
    Safely evaluate a simple binary arithmetic expression on context values.

    Supports: addition, subtraction, multiplication, division, modulo.
    Does NOT use eval() — the expression is parsed with a strict regex.

    Example:
        "input.existing_debt / input.annual_income"
        → context["input"]["existing_debt"] / context["input"]["annual_income"]
    """
    match = _EXPR_PATTERN.match(expression.strip())
    if not match:
        raise ValueError(
            f"Unsupported field_expression syntax: '{expression}'. "
            f"Only simple binary arithmetic is allowed (e.g. 'a.b / c.d')."
        )
    left_path, operator_sym, right_path = match.groups()
    left_val  = float(_get_nested_value(context, left_path))
    right_val = float(_get_nested_value(context, right_path))
    arithmetic_fn = _SAFE_ARITHMETIC_OPS[operator_sym]
    return arithmetic_fn(left_val, right_val)


# ══════════════════════════════════════════════════════════════════════════
#  Core evaluation function
# ══════════════════════════════════════════════════════════════════════════

def evaluate_rule(rule_config: Dict[str, Any], context: Dict[str, Any]) -> RuleResult:
    """
    Evaluate a single rule definition against the runtime context.

    Parameters:
        rule_config : dict from the YAML (one element of a stage's `rules` list)
        context     : the execution context dict:
                      {
                        "input":   { ...original client payload... },
                        "context": { ...values populated by earlier stages... }
                      }

    Returns:
        RuleResult with full audit detail.

    Never raises — exceptions are caught and returned as ERROR results
    so the orchestrator can handle them gracefully.
    """
    rule_id          = rule_config.get("rule_id", "unknown_rule")
    severity         = rule_config.get("severity", "mandatory")
    action           = rule_config.get("action")
    operator_name    = rule_config.get("operator", "equals")
    threshold        = rule_config.get("threshold")
    description      = rule_config.get("description", "")

    # ── 1. Resolve the value to evaluate ──────────────────────────────────
    field_path    = ""
    actual_value  = None

    try:
        if "field_expression" in rule_config:
            # Arithmetic expression, e.g. "input.debt / input.income"
            field_path   = rule_config["field_expression"]
            actual_value = _evaluate_field_expression(field_path, context)
        elif "field" in rule_config:
            field_path   = rule_config["field"]
            actual_value = _get_nested_value(context, field_path)
        else:
            raise ValueError(f"Rule '{rule_id}' must have either 'field' or 'field_expression'.")
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Rule '%s' failed to resolve field: %s", rule_id, exc)
        return RuleResult(
            rule_id=rule_id, passed=False, severity=severity, action=action,
            field_path=field_path, evaluated_value=None, expected_value=threshold,
            operator=operator_name, error=str(exc),
            reason=f"Could not resolve field '{field_path}': {exc}",
        )

    # ── 2. Look up the operator function ──────────────────────────────────
    operator_fn = OPERATOR_REGISTRY.get(operator_name)
    if operator_fn is None:
        error_msg = (
            f"Unknown operator '{operator_name}'. "
            f"Supported operators: {sorted(OPERATOR_REGISTRY.keys())}"
        )
        logger.error("Rule '%s': %s", rule_id, error_msg)
        return RuleResult(
            rule_id=rule_id, passed=False, severity=severity, action=action,
            field_path=field_path, evaluated_value=actual_value,
            expected_value=threshold, operator=operator_name,
            error=error_msg, reason=error_msg,
        )

    # ── 3. Apply the operator ─────────────────────────────────────────────
    try:
        # Coerce numeric types when comparing floats/ints from JSON
        eval_value = actual_value
        if isinstance(threshold, (int, float)) and isinstance(actual_value, str):
            try:
                eval_value = float(actual_value)
            except ValueError:
                pass

        passed = operator_fn(eval_value, threshold)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rule '%s' operator evaluation raised: %s", rule_id, exc)
        return RuleResult(
            rule_id=rule_id, passed=False, severity=severity, action=action,
            field_path=field_path, evaluated_value=actual_value,
            expected_value=threshold, operator=operator_name,
            error=str(exc), reason=f"Operator error: {exc}",
        )

    # ── 4. Handle on_fail_threshold (graduated soft-fail routing) ─────────
    # If the rule fails but has an on_fail_threshold, check whether the value
    # is in the "soft fail" range vs the "hard fail" range.
    if not passed and "on_fail_threshold" in rule_config:
        on_fail_threshold = rule_config["on_fail_threshold"]
        on_fail_route     = rule_config.get("on_fail_route", "manual_review")
        try:
            if float(actual_value) >= float(on_fail_threshold):
                # Value is above the on_fail_threshold → soft route, not reject
                action   = "force_manual_review" if on_fail_route == "manual_review" else action
                severity = "soft"
                logger.debug(
                    "Rule '%s': graduated soft fail (value=%s >= on_fail_threshold=%s) → %s",
                    rule_id, actual_value, on_fail_threshold, on_fail_route,
                )
        except (TypeError, ValueError):
            pass  # If comparison fails, fall through to normal severity

    # ── 5. Build the human-readable reason ───────────────────────────────
    status_word = "PASSED" if passed else "FAILED"
    reason = (
        f"[{status_word}] {description or rule_id}: "
        f"'{field_path}' = {actual_value!r} "
        f"{operator_name} {threshold!r}"
    )
    if not passed and action == "force_manual_review":
        reason += " → routed to MANUAL REVIEW (force_manual_review)"
    elif not passed and severity == "soft":
        reason += " → routed to MANUAL REVIEW (soft failure)"
    elif not passed and severity == "mandatory":
        reason += " → HARD REJECT (mandatory failure)"

    logger.debug("Rule evaluated: %s", reason)

    return RuleResult(
        rule_id=rule_id, passed=passed, severity=severity, action=action,
        field_path=field_path, evaluated_value=actual_value,
        expected_value=threshold, operator=operator_name, reason=reason,
    )


# ══════════════════════════════════════════════════════════════════════════
#  Stage-level evaluation (evaluates all rules in a stage)
# ══════════════════════════════════════════════════════════════════════════

def evaluate_stage_rules(
    stage_config: Dict[str, Any],
    context: Dict[str, Any],
) -> StageEvaluationResult:
    """
    Evaluate all rules defined in a stage config dict.

    Continues evaluating rules even after a failure so the audit log
    captures ALL failures in one pass (not just the first one).
    This gives reviewers the full picture.

    Parameters:
        stage_config : the stage dict from the YAML
        context      : the execution context

    Returns:
        StageEvaluationResult aggregating all individual RuleResults
    """
    stage_id     = stage_config.get("stage_id", "unknown_stage")
    rules_config = stage_config.get("rules", [])
    results      = []

    for rule_config in rules_config:
        result = evaluate_rule(rule_config, context)
        results.append(result)
        logger.info(
            "Stage '%s' | Rule '%s' → %s",
            stage_id, result.rule_id, result.result_label,
        )

    return StageEvaluationResult(stage_id=stage_id, rule_results=results)
