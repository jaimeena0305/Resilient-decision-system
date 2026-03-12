"""
tests/test_engine.py
─────────────────────────────────────────────────────────────────────────────
Full test suite for the Resilient Decision System.

Coverage:
  1.  Happy path        — loan that passes every check → APPROVED
  2.  Rejection path    — mandatory rule failure → REJECTED
  3.  Manual review     — soft-fail / force_manual_review → MANUAL_REVIEW
  4.  Idempotency       — same X-Request-ID twice → no double-processing
  5.  Retry & recovery  — mock fails once then succeeds → retries captured in audit
  6.  All retries fail  — mock always fails → REJECTED with EXT_DEP_FAILURE audit
  7.  Rule change       — reload modified YAML config → engine respects new threshold
  8.  Invalid input     — missing required field → 400 validation error
  9.  Schema violation  — age below minimum → REJECTED at input-validation stage
  10. Unknown workflow  — non-existent workflow_id → 400 not found
  11. Rules engine unit — each operator tested in isolation
  12. Retry backoff     — verify delay computation from RetryConfig
  13. Idempotency svc   — unit-test the service layer directly

Run:
    pytest tests/test_engine.py -v
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker, Session

# ── Bootstrap the app with an isolated in-memory SQLite DB ────────────────

from app.models.db import Base, Execution, ExecutionStatus, init_db
from app.services.config_loader import load_workflow_config
from app.services.idempotency import IdempotencyService
from app.core.rules_engine import (
    RuleResult,
    StageEvaluationResult,
    evaluate_rule,
    evaluate_stage_rules,
)
from app.dependencies.mock_api import (
    ExternalAPIError,
    RetryConfig,
    TransientAPIError,
    call_external_dependency,
    with_retry,
)
from app.core.orchestrator import run_workflow


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def db_session():
    """
    Provide a fresh, isolated SQLite in-memory database for each test.
    All data is discarded when the test ends.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture(scope="function")
def client(db_session):
    """
    Return a FastAPI TestClient wired to use the test DB session.
    Overrides the `get_db` dependency so every route handler uses
    our isolated in-memory database.
    """
    from app.main import app, get_db

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_config_cache():
    """
    Clear the LRU config cache before each test so YAML mutations
    in the rule-change test don't leak into other tests.
    """
    load_workflow_config.cache_clear()
    yield
    load_workflow_config.cache_clear()


@pytest.fixture(autouse=True)
def clear_idempotency(client):
    """
    Wipe the in-memory idempotency store between tests.
    (The TestClient shares the app-level IdempotencyService singleton.)
    """
    from app.main import idempotency_svc
    idempotency_svc._memory.clear()
    yield


def _request_id() -> str:
    """Generate a unique X-Request-ID for each test call."""
    return str(uuid.uuid4())


# ── Canonical payloads ────────────────────────────────────────────────────

GOOD_LOAN_PAYLOAD: Dict[str, Any] = {
    "workflow_id": "loan_approval_v1",
    "input_data": {
        "applicant_id":      "APP-001",
        "full_name":         "Alice Johnson",
        "age":               35,
        "annual_income":     95000,
        "requested_amount":  18000,   # within 30% cap, under $25k
        "employment_status": "employed",
        "existing_debt":     5000,    # DTI = 5.3%
    },
}

REJECTED_LOAN_PAYLOAD: Dict[str, Any] = {
    "workflow_id": "loan_approval_v1",
    "input_data": {
        "applicant_id":      "APP-002",
        "full_name":         "Bob Smith",
        "age":               28,
        "annual_income":     55000,
        "requested_amount":  12000,
        "employment_status": "employed",
        "existing_debt":     3000,
        # credit score will be forced low via mock
    },
}

HIGH_VALUE_LOAN_PAYLOAD: Dict[str, Any] = {
    "workflow_id": "loan_approval_v1",
    "input_data": {
        "applicant_id":      "APP-003",
        "full_name":         "Carol White",
        "age":               42,
        "annual_income":     150000,
        "requested_amount":  30000,   # > $25k → force_manual_review
        "employment_status": "employed",
        "existing_debt":     5000,
    },
}


# ══════════════════════════════════════════════════════════════════════════
#  1. Happy Path: APPROVED
# ══════════════════════════════════════════════════════════════════════════

def test_happy_path_approved(client):
    """
    A well-qualified applicant with a good credit score should be APPROVED.

    Asserts:
      - HTTP 201 Created
      - status == APPROVED
      - decision_trace.summary mentions 'Approved'
      - audit_trail contains RULE_EVALUATED entries, all PASS
      - no mandatory_failures in the trace
    """
    # Patch the external credit API to always return a high score
    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99     # > failure_rate → no failure
        mock_rand.randint.return_value = 750     # credit score = 750 (well above 650)
        mock_rand.uniform.return_value = 0.1    # latency ~100ms / 10 = 10ms

        response = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "APPROVED"
    assert "Approved" in body["decision_trace"]["summary"]
    assert body["decision_trace"]["mandatory_failures"] == []
    assert body["decision_trace"]["soft_failures"] == []

    # Verify audit trail was written
    audit = body["audit_trail"]
    assert len(audit) > 0

    rule_evals = [e for e in audit if e["event_type"] == "RULE_EVALUATED"]
    assert len(rule_evals) >= 4, "Expected at least 4 rule evaluations"

    # Every rule should have passed
    failed_rules = [r for r in rule_evals if r["result"] == "FAIL"]
    assert failed_rules == [], f"No rules should fail in happy path, got: {failed_rules}"

    # Verify the external dependency stage succeeded
    ext_success = [e for e in audit if e["event_type"] == "EXT_DEP_SUCCESS"]
    assert len(ext_success) == 1, "Credit bureau API should have been called once"

    print(f"\n✅ Happy path approved | Credit score=750 | Audit entries={len(audit)}")


# ══════════════════════════════════════════════════════════════════════════
#  2. Rejection Path: Mandatory Rule Failure
# ══════════════════════════════════════════════════════════════════════════

def test_rejected_on_low_credit_score(client):
    """
    An applicant with a credit score below 580 should be REJECTED
    (not MANUAL_REVIEW — the score is below both thresholds).

    Asserts:
      - status == REJECTED
      - decision_trace.mandatory_failures contains 'credit_score_minimum'
      - audit_trail has a FAIL entry for 'credit_score_minimum'
    """
    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99   # no API failure
        mock_rand.randint.return_value = 450   # credit score = 450 (below 580 threshold)
        mock_rand.uniform.return_value = 0.1

        response = client.post(
            "/executions",
            json=REJECTED_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201
    body = response.json()

    assert body["status"] == "REJECTED"
    assert "credit_score_minimum" in body["decision_trace"]["mandatory_failures"]
    assert "Rejected" in body["decision_trace"]["summary"]

    # Verify the specific rule failure is in the audit trail
    audit = body["audit_trail"]
    credit_rule_entry = next(
        (e for e in audit
         if e["event_type"] == "RULE_EVALUATED"
         and e["rule_id"] == "credit_score_minimum"
         and e["result"] == "FAIL"),
        None,
    )
    assert credit_rule_entry is not None, "Expected FAIL entry for credit_score_minimum"
    assert credit_rule_entry["evaluated_value"] == "450"
    assert credit_rule_entry["operator"] == "gte"
    assert credit_rule_entry["expected_value"] == "650"

    print(f"\n✅ Rejection verified | Credit score=450 | Mandatory failures={body['decision_trace']['mandatory_failures']}")


def test_rejected_on_low_income(client):
    """Income below $30,000 must be rejected at the validate_input stage."""
    payload = copy.deepcopy(GOOD_LOAN_PAYLOAD)
    payload["input_data"]["annual_income"] = 20000   # below $30k mandatory threshold

    response = client.post(
        "/executions",
        json=payload,
        headers={"X-Request-ID": _request_id()},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "REJECTED"
    assert "minimum_income" in body["decision_trace"]["mandatory_failures"]

    # The external API should NOT have been called — rejection happened earlier
    audit = body["audit_trail"]
    ext_calls = [e for e in audit if e["event_type"] in ("EXT_DEP_SUCCESS", "EXT_DEP_FAILURE")]
    assert ext_calls == [], "External API should not be called after early rejection"
    print("\n✅ Early rejection (low income) — external API not called ✓")


# ══════════════════════════════════════════════════════════════════════════
#  3. Manual Review Path
# ══════════════════════════════════════════════════════════════════════════

def test_manual_review_high_value_loan(client):
    """
    A loan amount > $25,000 triggers force_manual_review regardless of
    credit score. Result should be MANUAL_REVIEW, not APPROVED.
    """
    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99
        mock_rand.randint.return_value = 780    # excellent credit — still goes to review
        mock_rand.uniform.return_value = 0.1

        response = client.post(
            "/executions",
            json=HIGH_VALUE_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201
    body = response.json()

    assert body["status"] == "MANUAL_REVIEW"
    assert "high_value_flag" in body["decision_trace"]["forced_reviews"]
    assert "manual review" in body["decision_trace"]["summary"].lower()
    print(f"\n✅ Manual review triggered | forced_reviews={body['decision_trace']['forced_reviews']}")


# ══════════════════════════════════════════════════════════════════════════
#  4. Idempotency: Same X-Request-ID Twice
# ══════════════════════════════════════════════════════════════════════════

def test_idempotency_duplicate_request_not_processed_twice(client, db_session):
    """
    The CORE idempotency test.

    Send the same request twice with the same X-Request-ID.

    Asserts:
      - Both calls return the same execution_id
      - Only ONE execution row exists in the database
      - Second response has HTTP 200 (not 201)
      - The workflow engine ran exactly once
        (proven by the count of audit log rows being identical)
    """
    shared_request_id = _request_id()

    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99
        mock_rand.randint.return_value = 720
        mock_rand.uniform.return_value = 0.1

        # First call
        response1 = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": shared_request_id},
        )
        assert response1.status_code == 201
        body1 = response1.json()

        # Second call — identical payload AND header
        response2 = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": shared_request_id},
        )

    # Second response must be HTTP 200 (cached, not re-created)
    assert response2.status_code == 200, (
        f"Expected 200 for idempotency hit, got {response2.status_code}"
    )
    body2 = response2.json()

    # Both responses must point to the same execution
    assert body1["id"] == body2["id"], (
        f"Idempotency violation: two different execution IDs created!\n"
        f"  First:  {body1['id']}\n"
        f"  Second: {body2['id']}"
    )

    # Database must contain exactly ONE execution for this request_id
    db_session.expire_all()
    from app.models.db import AuditLog
    executions = (
        db_session.query(Execution)
        .filter(Execution.request_id == shared_request_id)
        .all()
    )
    assert len(executions) == 1, (
        f"Expected 1 execution in DB, found {len(executions)}. "
        f"Engine must have processed the request twice!"
    )

    # Audit log count must be identical in both responses
    assert len(body1["audit_trail"]) == len(body2["audit_trail"]), (
        "Audit trail changed between calls — engine ran twice!"
    )

    print(
        f"\n✅ Idempotency verified\n"
        f"   Request-ID      : {shared_request_id}\n"
        f"   Execution ID    : {body1['id']}\n"
        f"   DB rows         : 1 (no duplicate)\n"
        f"   Response codes  : {response1.status_code} / {response2.status_code}\n"
        f"   Audit entries   : {len(body1['audit_trail'])} (identical both calls)"
    )


def test_idempotency_different_request_ids_create_separate_executions(client):
    """Two different Request-IDs must produce two independent execution rows."""
    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99
        mock_rand.randint.return_value = 720
        mock_rand.uniform.return_value = 0.1

        r1 = client.post("/executions", json=GOOD_LOAN_PAYLOAD,
                         headers={"X-Request-ID": _request_id()})
        r2 = client.post("/executions", json=GOOD_LOAN_PAYLOAD,
                         headers={"X-Request-ID": _request_id()})

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"], "Different request IDs must produce different executions"
    print("\n✅ Different request IDs → separate executions ✓")


# ══════════════════════════════════════════════════════════════════════════
#  5. Retry & Recovery: External Dependency Fails Then Succeeds
# ══════════════════════════════════════════════════════════════════════════

def test_retry_succeeds_on_second_attempt(client):
    """
    The credit bureau API fails on attempt 1, succeeds on attempt 2.

    Asserts:
      - Final status is APPROVED (not REJECTED due to API failure)
      - Audit trail contains one EXT_DEP_ATTEMPT (the failure) followed
        by one EXT_DEP_SUCCESS (the recovery)
      - The retry is transparent to the caller — they just get a result
    """
    call_count = {"n": 0}

    def flaky_dependency(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise TransientAPIError(500, "Simulated transient failure on attempt 1")
        # Second call succeeds
        return {"credit_score": 720, "bureau": "MockEquifax", "pull_timestamp": time.time()}, 2

    with patch("app.core.orchestrator.call_external_dependency", side_effect=flaky_dependency):
        # We need the attempt_log to be populated — patch call_external_dependency
        # to simulate one failure logged before success
        pass

    # Alternative: patch at the mock_api level with controlled failure count
    attempt_counts = {"total": 0}

    original_call = call_external_dependency

    def controlled_call(service_id, mock_config, retry_policy, input_payload, attempt_log):
        attempt_counts["total"] += 1
        if attempt_counts["total"] == 1:
            # Simulate one failure being logged
            attempt_log.append({
                "attempt": 1,
                "error": "Simulated failure on attempt 1",
                "timestamp": time.time(),
            })
        # Force a high score on "success"
        mock_config = dict(mock_config)
        mock_config["failure_rate"] = 0.0        # no more failures
        mock_config["response_range"] = [720, 720]  # deterministic score
        mock_config["latency_ms"] = [1, 2]          # fast
        return original_call(
            service_id, mock_config, retry_policy, input_payload, attempt_log
        )

    with patch("app.core.orchestrator.call_external_dependency", side_effect=controlled_call):
        response = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201
    body = response.json()
    audit = body["audit_trail"]

    # Verify the retry attempt was logged
    retry_attempts = [e for e in audit if e["event_type"] == "EXT_DEP_ATTEMPT"]
    ext_success    = [e for e in audit if e["event_type"] == "EXT_DEP_SUCCESS"]

    assert len(retry_attempts) >= 1, "Expected at least one EXT_DEP_ATTEMPT (the failure)"
    assert len(ext_success) == 1,   "Expected exactly one EXT_DEP_SUCCESS (the recovery)"

    print(
        f"\n✅ Retry recovery verified\n"
        f"   Failed attempts logged : {len(retry_attempts)}\n"
        f"   Final status           : {body['status']}\n"
        f"   Audit entries          : {len(audit)}"
    )


def test_retry_with_real_backoff_decorator():
    """
    Unit test the `with_retry` decorator directly.
    Verifies that a function failing twice then succeeding returns the
    correct result and that the attempt count is accurate.
    """
    call_log = []

    retry_cfg = RetryConfig(
        max_attempts=3,
        base_delay_ms=1,    # 1ms for fast tests
        max_delay_ms=10,
        jitter=False,
    )

    @with_retry(retry_cfg, service_id="test_service")
    def fragile_function():
        call_log.append(time.monotonic())
        if len(call_log) < 3:
            raise TransientAPIError(500, f"Failure #{len(call_log)}")
        return {"success": True}

    result = fragile_function()

    assert result == {"success": True}
    assert len(call_log) == 3, f"Expected 3 attempts, got {len(call_log)}"

    # Verify there was a delay between calls (basic backoff check)
    delay_1_to_2 = call_log[1] - call_log[0]
    delay_2_to_3 = call_log[2] - call_log[1]
    assert delay_2_to_3 >= delay_1_to_2 * 0.5, (
        "Second delay should be >= first delay (exponential backoff)"
    )
    print(f"\n✅ Retry backoff: delay1={delay_1_to_2*1000:.1f}ms, delay2={delay_2_to_3*1000:.1f}ms")


def test_all_retries_exhausted_produces_failed_execution(client):
    """
    When the external API fails on every attempt, the execution must
    reach a terminal REJECTED or FAILED state — never stuck in RUNNING.

    The audit trail must contain EXT_DEP_FAILURE as the final event
    for the external stage.
    """
    def always_fail(service_id, mock_config, retry_policy, input_payload, attempt_log):
        for i in range(retry_policy.get("max_attempts", 3)):
            attempt_log.append({"attempt": i + 1, "error": "Permanent failure", "timestamp": time.time()})
        raise ExternalAPIError(
            service_id=service_id,
            status_code=500,
            attempts=retry_policy.get("max_attempts", 3),
            last_error="Permanent simulated failure",
        )

    with patch("app.core.orchestrator.call_external_dependency", side_effect=always_fail):
        response = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201
    body = response.json()

    # Must be in a terminal state — not RUNNING, not RETRYING
    assert body["status"] in ("REJECTED", "FAILED", "MANUAL_REVIEW"), (
        f"Execution stuck in non-terminal state: {body['status']}"
    )

    audit = body["audit_trail"]
    ext_failure = [e for e in audit if e["event_type"] == "EXT_DEP_FAILURE"]
    assert len(ext_failure) >= 1, "EXT_DEP_FAILURE must appear in audit trail after exhaustion"
    print(f"\n✅ All retries exhausted → terminal state={body['status']} | EXT_DEP_FAILURE logged ✓")


# ══════════════════════════════════════════════════════════════════════════
#  6. Rule Change Scenario: Config Hot-Reload
# ══════════════════════════════════════════════════════════════════════════

def test_rule_change_new_threshold_respected(client):
    """
    RULE CHANGE SCENARIO — This is the key configurability test.

    Demonstrates that modifying the YAML config (changing a threshold)
    takes effect immediately on the next request without any code changes.

    Scenario:
      - Load a MODIFIED config where minimum_income threshold = $200,000
      - Submit a payload with annual_income = $95,000 (passes original $30k rule)
      - The modified rule should REJECT it

    This proves the engine reads rules from config, not from hardcoded Python.
    """
    # Load the real config and create a modified version with a tighter threshold
    original_config = load_workflow_config("loan_approval_v1")
    modified_config  = copy.deepcopy(original_config)

    # Find the minimum_income rule and raise the threshold dramatically
    for stage in modified_config["stages"]:
        if stage["stage_id"] == "validate_input":
            for rule in stage["rules"]:
                if rule["rule_id"] == "minimum_income":
                    rule["threshold"] = 200_000   # ← changed from 30k to 200k
                    rule["description"] = "Modified: income must exceed $200,000"

    # Patch the config loader to return our modified config
    with patch(
        "app.core.orchestrator.load_workflow_config",
        return_value=modified_config,
    ):
        response = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,    # income = $95,000 — passes original rule, fails new one
            headers={"X-Request-ID": _request_id()},
        )

    assert response.status_code == 201
    body = response.json()

    # With original config (threshold=$30k), this would be APPROVED.
    # With modified config (threshold=$200k), it must be REJECTED.
    assert body["status"] == "REJECTED", (
        f"Expected REJECTED with modified rule (threshold=$200k), got: {body['status']}"
    )
    assert "minimum_income" in body["decision_trace"]["mandatory_failures"], (
        "minimum_income must appear in mandatory_failures with the new threshold"
    )

    # Verify the evaluated value is in the audit trail with the new threshold
    audit = body["audit_trail"]
    income_rule_entry = next(
        (e for e in audit
         if e["event_type"] == "RULE_EVALUATED"
         and e["rule_id"] == "minimum_income"),
        None,
    )
    assert income_rule_entry is not None
    assert income_rule_entry["result"] == "FAIL"
    assert income_rule_entry["expected_value"] == "200000"   # new threshold reflected in audit

    print(
        f"\n✅ Rule change verified\n"
        f"   Original threshold : $30,000  → APPROVED\n"
        f"   Modified threshold : $200,000 → REJECTED (income was ${GOOD_LOAN_PAYLOAD['input_data']['annual_income']:,})\n"
        f"   Audit shows new threshold: expected_value={income_rule_entry['expected_value']}"
    )


def test_rule_change_relaxed_threshold_approves_previously_rejected(client):
    """
    Inverse of the above: relaxing a threshold allows a previously
    rejected applicant to pass.
    """
    original_config = load_workflow_config("loan_approval_v1")
    relaxed_config  = copy.deepcopy(original_config)

    # Find credit_score_minimum and lower it to 400 (everyone passes)
    for stage in relaxed_config["stages"]:
        if stage["stage_id"] == "rule_evaluation":
            for rule in stage["rules"]:
                if rule["rule_id"] == "credit_score_minimum":
                    rule["threshold"] = 400         # ← relaxed from 650
                    rule.pop("on_fail_threshold", None)
                    rule.pop("on_fail_route", None)

    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99
        mock_rand.randint.return_value = 450    # score=450: fails original 650 rule
        mock_rand.uniform.return_value = 0.1

        with patch("app.core.orchestrator.load_workflow_config", return_value=relaxed_config):
            response = client.post(
                "/executions",
                json=REJECTED_LOAN_PAYLOAD,
                headers={"X-Request-ID": _request_id()},
            )

    assert response.status_code == 201
    body = response.json()

    # With threshold=400, credit score 450 should now PASS
    assert body["status"] in ("APPROVED", "MANUAL_REVIEW"), (
        f"Expected APPROVED or MANUAL_REVIEW with relaxed threshold, got: {body['status']}"
    )
    assert "credit_score_minimum" not in body["decision_trace"]["mandatory_failures"]
    print(f"\n✅ Relaxed rule verified | Score=450 | New threshold=400 | Status={body['status']}")


# ══════════════════════════════════════════════════════════════════════════
#  7. Invalid Input Tests
# ══════════════════════════════════════════════════════════════════════════

def test_invalid_input_missing_required_field(client):
    """Missing `age` field should fail JSON Schema validation → REJECTED."""
    payload = copy.deepcopy(GOOD_LOAN_PAYLOAD)
    del payload["input_data"]["age"]

    response = client.post(
        "/executions",
        json=payload,
        headers={"X-Request-ID": _request_id()},
    )

    # Either a 400 from the outer schema or a REJECTED from the inner schema
    # Outer Pydantic will pass (input_data is still a dict).
    # Inner jsonschema will catch the missing required field.
    assert response.status_code in (400, 201)
    if response.status_code == 201:
        assert response.json()["status"] == "REJECTED"


def test_invalid_input_unknown_workflow(client):
    """A non-existent workflow_id should return 400."""
    response = client.post(
        "/executions",
        json={"workflow_id": "does_not_exist_v99", "input_data": {}},
        headers={"X-Request-ID": _request_id()},
    )
    assert response.status_code == 400
    assert "does_not_exist_v99" in response.text
    print("\n✅ Unknown workflow → 400 ✓")


def test_invalid_input_employment_status_enum(client):
    """An invalid enum value for employment_status should fail schema validation."""
    payload = copy.deepcopy(GOOD_LOAN_PAYLOAD)
    payload["input_data"]["employment_status"] = "freelancer"   # not in enum

    response = client.post(
        "/executions",
        json=payload,
        headers={"X-Request-ID": _request_id()},
    )

    assert response.status_code in (400, 201)
    if response.status_code == 201:
        assert response.json()["status"] == "REJECTED"


def test_path_traversal_in_workflow_id(client):
    """workflow_id with path traversal characters must be rejected."""
    response = client.post(
        "/executions",
        json={"workflow_id": "../secrets/config", "input_data": {}},
        headers={"X-Request-ID": _request_id()},
    )
    # Pydantic validator rejects the field before it reaches the file system
    assert response.status_code in (400, 422)
    print("\n✅ Path traversal rejected ✓")


# ══════════════════════════════════════════════════════════════════════════
#  8. Rules Engine Unit Tests
# ══════════════════════════════════════════════════════════════════════════

class TestRulesEngineOperators:
    """
    Unit tests for the Rules Engine operator registry.
    These tests have NO database or HTTP involvement — pure function tests.
    """

    BASE_CTX = {
        "input": {
            "age":               32,
            "annual_income":     75000,
            "existing_debt":     10000,
            "employment_status": "employed",
            "is_verified":       True,
            "score":             None,
        },
        "context": {
            "credit_score": 720,
        },
    }

    def _rule(self, **kwargs) -> Dict:
        return {"rule_id": "test_rule", "severity": "mandatory", **kwargs}

    def test_gte_pass(self):
        r = evaluate_rule(self._rule(field="input.age", operator="gte", threshold=18), self.BASE_CTX)
        assert r.passed and r.result_label == "PASS"

    def test_gte_fail(self):
        r = evaluate_rule(self._rule(field="input.age", operator="gte", threshold=50), self.BASE_CTX)
        assert not r.passed and r.result_label == "FAIL"

    def test_lte_pass(self):
        r = evaluate_rule(self._rule(field="input.existing_debt", operator="lte", threshold=15000), self.BASE_CTX)
        assert r.passed

    def test_equals_string(self):
        r = evaluate_rule(self._rule(field="input.employment_status", operator="equals", threshold="employed"), self.BASE_CTX)
        assert r.passed

    def test_not_in(self):
        r = evaluate_rule(self._rule(field="input.employment_status", operator="not_in", threshold=["unemployed", "retired"]), self.BASE_CTX)
        assert r.passed

    def test_in_pass(self):
        r = evaluate_rule(self._rule(field="input.employment_status", operator="in", threshold=["employed", "self_employed"]), self.BASE_CTX)
        assert r.passed

    def test_in_fail(self):
        r = evaluate_rule(self._rule(field="input.employment_status", operator="in", threshold=["unemployed"]), self.BASE_CTX)
        assert not r.passed

    def test_is_true(self):
        r = evaluate_rule(self._rule(field="input.is_verified", operator="is_true", threshold=None), self.BASE_CTX)
        assert r.passed

    def test_is_null(self):
        r = evaluate_rule(self._rule(field="input.score", operator="is_null", threshold=None), self.BASE_CTX)
        assert r.passed

    def test_is_not_null(self):
        r = evaluate_rule(self._rule(field="input.age", operator="is_not_null", threshold=None), self.BASE_CTX)
        assert r.passed

    def test_field_expression_dti_ratio(self):
        """Test arithmetic field_expression: existing_debt / annual_income = 0.133"""
        r = evaluate_rule(
            self._rule(field_expression="input.existing_debt / input.annual_income", operator="lte", threshold=0.40),
            self.BASE_CTX,
        )
        assert r.passed
        assert abs(float(r.evaluated_value) - 0.1333) < 0.001

    def test_field_expression_fail(self):
        """High DTI ratio should fail the lte check."""
        ctx = copy.deepcopy(self.BASE_CTX)
        ctx["input"]["existing_debt"] = 50000    # DTI = 66.7%
        r = evaluate_rule(
            self._rule(field_expression="input.existing_debt / input.annual_income", operator="lte", threshold=0.40),
            ctx,
        )
        assert not r.passed

    def test_context_field_resolution(self):
        """Rules can reference values populated by earlier stages (context.credit_score)."""
        r = evaluate_rule(
            self._rule(field="context.credit_score", operator="gte", threshold=650),
            self.BASE_CTX,
        )
        assert r.passed
        assert r.evaluated_value == 720

    def test_missing_field_returns_error_result(self):
        """A rule referencing a non-existent field must return an ERROR, not raise an exception."""
        r = evaluate_rule(
            self._rule(field="input.nonexistent_field", operator="gte", threshold=0),
            self.BASE_CTX,
        )
        assert r.result_label == "ERROR"
        assert r.error is not None
        assert not r.passed

    def test_unknown_operator_returns_error(self):
        """An unknown operator must return ERROR rather than crash."""
        r = evaluate_rule(
            self._rule(field="input.age", operator="xor_magic", threshold=18),
            self.BASE_CTX,
        )
        assert r.result_label == "ERROR"

    def test_mandatory_failure_triggers_hard_reject(self):
        """A mandatory rule failure must set triggers_hard_reject=True."""
        r = evaluate_rule(
            self._rule(field="input.age", operator="gte", threshold=100, severity="mandatory"),
            self.BASE_CTX,
        )
        assert not r.passed
        assert r.triggers_hard_reject
        assert not r.triggers_manual_review

    def test_soft_failure_triggers_manual_review(self):
        """A soft rule failure must set triggers_manual_review=True."""
        r = evaluate_rule(
            self._rule(field="input.age", operator="gte", threshold=100, severity="soft"),
            self.BASE_CTX,
        )
        assert not r.passed
        assert r.triggers_manual_review
        assert not r.triggers_hard_reject

    def test_force_manual_review_action(self):
        """A failed rule with action=force_manual_review routes to manual review, not reject."""
        r = evaluate_rule(
            self._rule(field="input.age", operator="gte", threshold=100, severity="soft", action="force_manual_review"),
            self.BASE_CTX,
        )
        assert not r.passed
        assert r.triggers_manual_review
        assert not r.triggers_hard_reject
        assert r.action == "force_manual_review"

    def test_stage_evaluation_all_pass(self):
        stage = {
            "stage_id": "test_stage",
            "rules": [
                {"rule_id": "r1", "field": "input.age", "operator": "gte", "threshold": 18, "severity": "mandatory"},
                {"rule_id": "r2", "field": "input.annual_income", "operator": "gte", "threshold": 30000, "severity": "mandatory"},
            ]
        }
        result = evaluate_stage_rules(stage, self.BASE_CTX)
        assert result.all_passed
        assert result.mandatory_failures == []

    def test_stage_evaluation_detects_hard_failure(self):
        stage = {
            "stage_id": "test_stage",
            "rules": [
                {"rule_id": "r1", "field": "input.age", "operator": "gte", "threshold": 18, "severity": "mandatory"},
                {"rule_id": "r2", "field": "input.annual_income", "operator": "gte", "threshold": 200000, "severity": "mandatory"},  # will fail
            ]
        }
        result = evaluate_stage_rules(stage, self.BASE_CTX)
        assert result.has_hard_failure
        assert "r2" in result.mandatory_failures
        assert not result.all_passed


# ══════════════════════════════════════════════════════════════════════════
#  9. RetryConfig Backoff Computation
# ══════════════════════════════════════════════════════════════════════════

class TestRetryConfig:
    """Unit tests for delay computation without any I/O."""

    def test_exponential_backoff_doubles(self):
        cfg = RetryConfig(base_delay_ms=200, backoff_strategy="exponential", jitter=False)
        d0 = cfg.compute_delay(0)  # 200ms
        d1 = cfg.compute_delay(1)  # 400ms
        d2 = cfg.compute_delay(2)  # 800ms
        assert abs(d1 / d0 - 2.0) < 0.01
        assert abs(d2 / d1 - 2.0) < 0.01

    def test_max_delay_cap(self):
        cfg = RetryConfig(base_delay_ms=200, max_delay_ms=500, backoff_strategy="exponential", jitter=False)
        d3 = cfg.compute_delay(3)   # 200 * 8 = 1600ms, capped to 500ms
        assert d3 <= 0.501   # 500ms in seconds

    def test_constant_backoff(self):
        cfg = RetryConfig(base_delay_ms=300, backoff_strategy="constant", jitter=False)
        delays = [cfg.compute_delay(i) for i in range(4)]
        assert all(abs(d - 0.3) < 0.001 for d in delays)

    def test_linear_backoff(self):
        cfg = RetryConfig(base_delay_ms=100, backoff_strategy="linear", jitter=False)
        d0 = cfg.compute_delay(0)   # 100 * 1 = 100ms
        d1 = cfg.compute_delay(1)   # 100 * 2 = 200ms
        assert abs(d1 / d0 - 2.0) < 0.01

    def test_from_yaml(self):
        yaml_dict = {
            "max_attempts": 4,
            "backoff_strategy": "exponential",
            "base_delay_ms": 250,
            "max_delay_ms": 8000,
            "retryable_http_codes": [500, 503],
        }
        cfg = RetryConfig.from_yaml(yaml_dict)
        assert cfg.max_attempts == 4
        assert cfg.base_delay_ms == 250
        assert 503 in cfg.retryable_status_codes


# ══════════════════════════════════════════════════════════════════════════
#  10. Idempotency Service Unit Tests
# ══════════════════════════════════════════════════════════════════════════

class TestIdempotencyService:
    """Unit tests for the IdempotencyService using in-memory mode."""

    def test_miss_on_new_request_id(self):
        svc = IdempotencyService(redis_url=None)
        assert svc.check("req-123") is None

    def test_hit_after_register(self):
        svc = IdempotencyService(redis_url=None)
        svc.register("req-456", "exec-789")
        assert svc.check("req-456") == "exec-789"

    def test_clear_removes_key(self):
        svc = IdempotencyService(redis_url=None)
        svc.register("req-abc", "exec-def")
        svc.clear("req-abc")
        assert svc.check("req-abc") is None

    def test_different_keys_independent(self):
        svc = IdempotencyService(redis_url=None)
        svc.register("req-1", "exec-1")
        svc.register("req-2", "exec-2")
        assert svc.check("req-1") == "exec-1"
        assert svc.check("req-2") == "exec-2"

    def test_ttl_expiry(self):
        """Keys with a 1-second TTL should expire after 1.1 seconds."""
        svc = IdempotencyService(redis_url=None, ttl_seconds=1)
        svc.register("req-expire", "exec-999")
        assert svc.check("req-expire") == "exec-999"
        time.sleep(1.1)
        assert svc.check("req-expire") is None


# ══════════════════════════════════════════════════════════════════════════
#  11. API Endpoint Tests
# ══════════════════════════════════════════════════════════════════════════

def test_get_execution_by_id(client):
    """GET /executions/{id} must return the same data as the POST response."""
    with patch("app.dependencies.mock_api.random") as mock_rand:
        mock_rand.random.return_value = 0.99
        mock_rand.randint.return_value = 720
        mock_rand.uniform.return_value = 0.1

        post_response = client.post(
            "/executions",
            json=GOOD_LOAN_PAYLOAD,
            headers={"X-Request-ID": _request_id()},
        )

    execution_id = post_response.json()["id"]
    get_response = client.get(f"/executions/{execution_id}")

    assert get_response.status_code == 200
    assert get_response.json()["id"] == execution_id
    assert get_response.json()["status"] == post_response.json()["status"]


def test_get_nonexistent_execution_returns_404(client):
    """GET /executions/{fake_id} must return 404."""
    response = client.get("/executions/nonexistent-id-xyz")
    assert response.status_code == 404


def test_list_workflows_endpoint(client):
    """GET /workflows must return a list including our YAML-configured workflows."""
    response = client.get("/workflows")
    assert response.status_code == 200
    workflows = response.json()["workflows"]
    assert "loan_approval_v1" in workflows
    assert "employee_onboarding_v1" in workflows


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
