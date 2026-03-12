# Architecture Document: Resilient Decision System

**Version:** 1.0.0  
**Date:** 2025  
**Author:** Engineering Team  
**Status:** Final

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architectural Goals and Constraints](#2-architectural-goals-and-constraints)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Component Design](#4-component-design)
5. [Data Flow](#5-data-flow)
6. [Data Model Design](#6-data-model-design)
7. [Idempotency Strategy](#7-idempotency-strategy)
8. [Failure Handling and Resilience](#8-failure-handling-and-resilience)
9. [Configurability Model](#9-configurability-model)
10. [Trade-off Defence](#10-trade-off-defence)
11. [Scaling Considerations](#11-scaling-considerations)
12. [Security Considerations](#12-security-considerations)
13. [Assumptions and Constraints](#13-assumptions-and-constraints)

---

## 1. System Overview

The Resilient Decision System is a **configuration-driven workflow execution engine** designed to process structured business requests through a series of configurable stages, evaluate domain-specific rules, call external dependencies, and produce auditable, explainable decisions.

The system is intentionally **domain-agnostic**. The same engine binary processes loan applications, employee onboarding approvals, document verifications, or vendor onboarding — entirely determined by which YAML workflow configuration file is referenced in the incoming request.

### Core Properties

| Property | Implementation |
|---|---|
| **Configurability** | Workflows and rules are pure YAML data — no Python changes required for new business logic |
| **Auditability** | Every state transition and rule evaluation writes an immutable audit log row |
| **Idempotency** | Redis-backed request deduplication using client-supplied `X-Request-ID` headers |
| **Resilience** | Exponential backoff retry on external dependencies; all execution paths terminate in a defined state |
| **Explainability** | `decision_trace` field on every execution summarises exactly which rules fired and why |

---

## 2. Architectural Goals and Constraints

### Goals (derived from requirements)

1. **G1 — Tolerate requirement changes**: Business rules must be modifiable without code redeployment.
2. **G2 — Full audit logs**: Every decision step must be traceable and replayable.
3. **G3 — Idempotency**: Duplicate submissions must be safely ignored.
4. **G4 — External dependency simulation**: The system must handle and retry transient failures.
5. **G5 — Configurability**: Adding a new workflow requires a new YAML file only.
6. **G6 — Explainability**: Decision reasoning must be inspectable by humans without database access.

### Constraints

- Must be runnable locally with zero infrastructure (SQLite mode).
- Must support a production upgrade path to PostgreSQL + Redis without code changes.
- Must demonstrate at least one external dependency with simulated failures.

---

## 3. High-Level Architecture

The system uses a **Layered Architecture** with a strict dependency rule: layers only call downward, never upward.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LAYER 1: API GATEWAY                        │
│                                                                     │
│   FastAPI  │  Pydantic validation  │  Idempotency middleware        │
│   POST /executions → X-Request-ID guard → route to Orchestrator    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ (sync call)
┌───────────────────────────────▼─────────────────────────────────────┐
│                      LAYER 2: ORCHESTRATOR                          │
│                                                                     │
│   Drives the state machine.  Loads YAML config.  Calls each        │
│   stage executor in order.  Writes audit logs.  Builds final        │
│   decision trace.  Guarantees terminal state.                       │
└──────┬──────────────────────────────┬────────────────────────────────┘
       │                              │
┌──────▼──────────┐        ┌──────────▼─────────────────────────────┐
│  LAYER 3a:      │        │  LAYER 3b:                             │
│  RULES ENGINE   │        │  STAGE EXECUTORS                       │
│                 │        │                                        │
│  Stateless.     │        │  rule_evaluation  → Rules Engine       │
│  No DB access.  │        │  external_dep     → Mock API + Retry   │
│  Returns typed  │        │                                        │
│  RuleResult.    │        │  Each executor is pluggable.           │
└──────┬──────────┘        └──────────┬─────────────────────────────┘
       │                              │
┌──────▼──────────────────────────────▼─────────────────────────────┐
│                      LAYER 4: PERSISTENCE                          │
│                                                                     │
│   SQLAlchemy ORM │ SQLite (dev) │ PostgreSQL (prod)                │
│                                                                     │
│   executions  │  audit_logs  ║  Redis: idempotency key store       │
└─────────────────────────────────────────────────────────────────────┘
```

### Architectural Pattern: State Machine + Rules Engine

The two patterns are composed, not alternatives:

- **State Machine** governs the lifecycle of an `Execution` (`PENDING → RUNNING → APPROVED/REJECTED/MANUAL_REVIEW/FAILED`). It enforces valid transitions and prevents undefined states.
- **Rules Engine** is a pure function called within a state. It evaluates the business conditions that determine *which* transition to take.

This separation means the state machine is workflow-agnostic (it always follows the same transitions) while the rules engine is entirely driven by configuration data.

---

## 4. Component Design

### 4.1 API Gateway (`app/main.py`)

**Responsibilities:**
- Accept `POST /executions` with a JSON body and `X-Request-ID` header.
- Validate the outer request structure via Pydantic.
- Check idempotency before any work begins.
- Create the `Execution` row in `PENDING` state.
- Call `run_workflow()` synchronously.
- Return the full `ExecutionResponse` including audit trail.

**Design rationale:** Keeping the route handler thin — it only orchestrates service calls, never contains business logic. All intelligence lives in lower layers.

### 4.2 Orchestrator (`app/core/orchestrator.py`)

**Responsibilities:**
- Load the YAML config via `config_loader`.
- Validate the input payload against the workflow's `input_schema`.
- Iterate through stages, calling the appropriate executor for each `stage_type`.
- Write a `STATUS_TRANSITION` audit entry on every state change.
- Build the final `decision_trace` after the last stage.
- **Guarantee**: Every code path through `run_workflow()` terminates in a `db.flush()` with the execution in a terminal state. There is no code path that leaves an execution stuck in `RUNNING`.

**State machine implementation:** The orchestrator maintains the execution status directly on the `Execution` ORM object. Transitions are validated implicitly by the logic flow — invalid transitions cannot occur because the code only calls `_transition()` with known-valid target states.

### 4.3 Rules Engine (`app/core/rules_engine.py`)

**Responsibilities:**
- Parse a single rule dict from the YAML config.
- Resolve a value from the runtime context using dot-notation field paths.
- Safely evaluate a binary arithmetic `field_expression` without using `eval()`.
- Apply an operator from the `OPERATOR_REGISTRY`.
- Return a `RuleResult` dataclass with full audit detail.

**Key design choice — Operator Registry:**
```python
OPERATOR_REGISTRY: Dict[str, Callable] = {
    "gte": op.ge,
    "lte": op.le,
    "in":  lambda v, t: v in t,
    # adding a new operator = one line here, zero changes elsewhere
}
```
This is the **Open/Closed Principle** applied: the engine is open for extension (add a new operator) but closed for modification (no existing code changes).

**Zero side effects:** The Rules Engine never touches the database. This makes it trivially unit-testable and means a rules evaluation bug cannot corrupt the audit trail.

### 4.4 External Dependency Layer (`app/dependencies/mock_api.py`)

**Responsibilities:**
- Provide `call_external_dependency()` as the single entry point.
- Build a `RetryConfig` from the YAML `retry_policy` dict.
- Apply `with_retry()` decorator that catches `TransientAPIError` and implements exponential backoff with ±25% jitter.
- Simulate realistic latency and a configurable failure rate.
- Pass a mutable `attempt_log` list to the caller so the orchestrator can write per-attempt audit entries.

**Jitter rationale:** Without jitter, all workers waiting on a failed service would send their retry simultaneously, potentially overloading the recovering service. Adding ±25% random noise spreads the retry storm.

### 4.5 Configuration Loader (`app/services/config_loader.py`)

**Responsibilities:**
- Map `workflow_id` → YAML file path.
- Parse and return the config dict.
- Cache results with `@functools.lru_cache` to avoid repeated disk reads.
- Protect against path traversal attacks before building the file path.

**Cache invalidation:** Call `load_workflow_config.cache_clear()` to reload configs at runtime (used in tests and for hot-reload scenarios in production).

### 4.6 Idempotency Service (`app/services/idempotency.py`)

**Responsibilities:**
- Accept a `request_id` (from `X-Request-ID` header).
- Check Redis for an existing `execution_id` mapping.
- Register the mapping after the execution row is created.
- Fall back gracefully to an in-process `threading.Lock`-protected dict when Redis is unavailable.

---

## 5. Data Flow

### Request Processing (Happy Path)

```
1.  Client sends POST /executions with X-Request-ID: <uuid>

2.  Gateway:
      a. Pydantic validates outer schema (workflow_id type, input_data present)
      b. Redis GET idempotency:<request_id> → MISS
      c. Workflow YAML config exists? → YES
      d. INSERT Execution (status=PENDING)
      e. Redis SET idempotency:<request_id> → execution_id (TTL=24h)

3.  Orchestrator run_workflow(execution_id):
      a. UPDATE Execution → status=RUNNING  [AUDIT: STATUS_TRANSITION]
      b. WRITE AUDIT: EXECUTION_CREATED
      c. jsonschema.validate(input_payload, workflow.input_schema)
      d. Build context = { "input": payload, "context": {} }

4.  For each stage in workflow.stages:

      [type=rule_evaluation]
        i.  WRITE AUDIT: STAGE_STARTED
        ii. For each rule:
              → evaluate_rule(rule_config, context) → RuleResult
              → WRITE AUDIT: RULE_EVALUATED (field, value, operator, threshold, result)
        iii. Aggregate StageEvaluationResult
        iv. WRITE AUDIT: STAGE_COMPLETED or STAGE_FAILED
        v.  Return routing: "continue" | "reject" | "manual_review"

      [type=external_dependency]
        i.  WRITE AUDIT: STAGE_STARTED
        ii. UPDATE Execution → status=RETRYING
        iii. call_external_dependency() [with retry loop]:
               attempt 1: TransientAPIError (HTTP 500) → wait 200ms
                 WRITE AUDIT: EXT_DEP_ATTEMPT (fail)
               attempt 2: success
                 WRITE AUDIT: EXT_DEP_SUCCESS
        iv. Map response into context["context"]
        v.  UPDATE Execution → status=RUNNING
        vi. WRITE AUDIT: STAGE_COMPLETED
        vii.Return routing: "continue"

5.  After all stages:
      → _apply_final_routing() → UPDATE Execution → status=APPROVED
      → _build_decision_trace() → aggregate all RULE_EVALUATED logs
      → UPDATE Execution.decision_trace = { summary, rule_trace, ... }
      → WRITE AUDIT: EXECUTION_FINAL

6.  db.commit()

7.  Gateway returns ExecutionResponse (status=201, body=full JSON with audit_trail)
```

### Duplicate Request Flow (Idempotency)

```
1.  Client re-sends same X-Request-ID: <uuid>

2.  Gateway:
      Redis GET idempotency:<uuid> → HIT → execution_id=<id>
      SELECT Execution WHERE id=<id>
      Return cached ExecutionResponse (HTTP 200, not 201)

      → NO work is performed. Engine never called.
```

---

## 6. Data Model Design

### `executions` table

```sql
CREATE TABLE executions (
    id              VARCHAR(36) PRIMARY KEY,     -- UUID
    request_id      VARCHAR(255) UNIQUE NOT NULL, -- Client X-Request-ID
    workflow_id     VARCHAR(100) NOT NULL,
    status          ENUM NOT NULL,               -- State machine status
    input_payload   JSON NOT NULL,               -- Immutable original input
    context         JSON NOT NULL DEFAULT '{}',  -- Mutable runtime scratchpad
    decision_trace  JSON,                        -- Final summary (populated on completion)
    current_stage   VARCHAR(100),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX ix_executions_request_id   ON executions(request_id);
CREATE INDEX ix_executions_workflow_id  ON executions(workflow_id);
CREATE INDEX ix_executions_status       ON executions(status);
```

### `audit_logs` table

```sql
CREATE TABLE audit_logs (
    id               VARCHAR(36) PRIMARY KEY,
    execution_id     VARCHAR(36) NOT NULL REFERENCES executions(id),
    sequence_number  INTEGER NOT NULL,
    event_type       ENUM NOT NULL,
    stage_id         VARCHAR(100),
    rule_id          VARCHAR(100),
    field_path       VARCHAR(255),
    evaluated_value  VARCHAR(512),
    expected_value   VARCHAR(512),
    operator         VARCHAR(50),
    result           VARCHAR(20),       -- PASS | FAIL | ERROR
    message          TEXT NOT NULL,
    extra_metadata   JSON,
    created_at       TIMESTAMPTZ NOT NULL,
    duration_ms      FLOAT
);
CREATE INDEX ix_audit_execution_seq ON audit_logs(execution_id, sequence_number);
```

**Key design choices:**
- `audit_logs` has no UPDATE path in the application code. Rows are inserted and never modified.
- `sequence_number` enables deterministic chronological ordering within an execution's trail, independent of clock skew.
- `extra_metadata` is a schema-free JSON column used for event-specific data (retry counts, HTTP codes, API response bodies) that doesn't warrant its own column.

---

## 7. Idempotency Strategy

**Mechanism:** Client-controlled `X-Request-ID` header (UUID v4 recommended).

**Why client-controlled rather than payload hash?**
- Payload hashing would prevent a client from legitimately re-submitting a corrected request (same data, different intent).
- Client-controlled keys are the industry standard (used by Stripe, Adyen, Twilio). The client owns the uniqueness guarantee.
- Hash collisions, while astronomically unlikely, would silently suppress legitimate resubmissions.

**Deduplication window:** 24 hours (configurable via `IDEMPOTENCY_TTL_SECONDS`).

**Redis key structure:** `idempotency:<request_id>` → `<execution_id>`

**Race condition handling:**
The sequence is: (1) check Redis → MISS, (2) INSERT execution row, (3) SET Redis key. If the process crashes between steps 2 and 3, the next attempt will:
- Check Redis → MISS again (key was never set)
- Attempt INSERT → fail with UNIQUE constraint violation on `request_id` column
- Catch the constraint error, query the existing execution, return its result

This means the database UNIQUE constraint on `executions.request_id` is a safety net that guarantees correctness even when Redis is not atomically consistent with the DB write.

---

## 8. Failure Handling and Resilience

### 8.1 External Dependency Failures

**Retry policy (configurable per-stage in YAML):**

```
Attempt 1 → FAIL (HTTP 500)
  Wait: base_delay * 2^0 * jitter(0.75..1.25)  → ~200ms
Attempt 2 → FAIL (HTTP 500)
  Wait: base_delay * 2^1 * jitter              → ~400ms
Attempt 3 → FAIL (HTTP 500)
  → ExternalAPIError raised, stage routes per on_failure policy
```

**`on_failure` routing options (per stage in YAML):**
- `reject` — hard reject the execution
- `manual_review` — route to human review queue
- `continue` — skip the stage result and proceed (for optional enrichment calls)
- `retry` — handled by the retry decorator; this key controls what happens after all retries are exhausted

### 8.2 Orchestrator-Level Safety Net

The orchestrator's main `try/except` block wraps the entire stage loop:

```python
try:
    for stage_cfg in stages:
        # ... execute stage
except Exception as exc:
    execution.error_message = f"Unexpected error: {exc}"
    final_routing = "failed"
    # Still calls _apply_final_routing → status=FAILED
    # Still calls _finalise → decision_trace is written
```

This guarantees that even an unexpected Python exception (programming error, OOM, etc.) results in a `FAILED` execution with an audit log entry — never a stuck `RUNNING` row.

### 8.3 Database Failure Handling

The route handler wraps `run_workflow()` in a try/except that calls `db.rollback()` on failure. This prevents partial audit log writes from persisting in an inconsistent state. The cost is that a database failure during execution produces no audit trail, which is the correct trade-off (no false audit data is better than incomplete audit data).

---

## 9. Configurability Model

### Adding a New Workflow (Zero Code Change)

1. Create `app/workflows/vendor_approval_v1.yaml`
2. Define `input_schema`, `stages`, `decision_logic`, and `metadata`
3. Deploy (or hot-reload by calling `load_workflow_config.cache_clear()`)
4. `POST /executions` with `"workflow_id": "vendor_approval_v1"`

No Python file is modified. The engine treats the new YAML like any other config.

### Adding a New Rule Operator (One-Line Code Change)

```python
# app/core/rules_engine.py — OPERATOR_REGISTRY
OPERATOR_REGISTRY = {
    # ... existing operators
    "between": lambda v, t: t[0] <= v <= t[1],   # ← one new line
}
```

All existing workflows continue to work. Any YAML can now use `operator: between`.

### Modifying an Existing Rule (Config Change Only)

```yaml
# Before: minimum income = $30,000
- rule_id: "minimum_income"
  threshold: 30000

# After: raise threshold to $40,000
- rule_id: "minimum_income"
  threshold: 40000
```

The engine picks up the change on the next request (cache cleared automatically in production via config hot-reload or process restart).

---

## 10. Trade-off Defence

This section explicitly defends the three most significant architectural decisions against their common alternatives.

---

### 10.1 YAML/JSON Configuration vs. Hardcoded Python Logic

**Decision:** All workflow definitions (stages, rules, thresholds, routing logic) live in YAML files. The Python engine contains zero business-domain conditionals.

**The alternative:** Hardcoding rules as Python functions:
```python
# The anti-pattern we explicitly avoided:
def evaluate_loan(applicant):
    if applicant["age"] < 18:
        return "REJECTED"
    if applicant["annual_income"] < 30000:
        return "REJECTED"
    if applicant["credit_score"] < 650:
        return "MANUAL_REVIEW"
    return "APPROVED"
```

**Why we rejected it:**

| Dimension | Hardcoded Python | YAML Config (our choice) |
|---|---|---|
| Changing a threshold | Code change + PR + review + deploy | Edit YAML file (can be done by a business analyst) |
| Adding a new workflow | New Python module + wiring | New YAML file |
| Auditing "what rules were live on Jan 15th" | Git blame (fragile) | Version-controlled YAML + `workflow_version` field on every execution |
| Testing a rule change | Requires code deployment | Can be tested locally by modifying YAML |
| Risk of regression | High (touching logic code) | Low (logic code is unchanged) |
| Non-engineer access | Impossible | Possible (YAML is readable) |

**The cost of our choice:** YAML can only express what the engine's operator set supports. Highly bespoke rules (e.g., "ML model score > 0.82") require a new operator or a new stage type. This is acceptable — we extend the engine at the operator level, not the workflow level, so all workflows automatically gain access to new operators.

**Conclusion:** Configuration-driven rules separate the *what* (business logic, owned by product teams) from the *how* (execution mechanics, owned by engineering). This is the correct boundary.

---

### 10.2 Synchronous REST API vs. Asynchronous Message Queue

**Decision:** `POST /executions` is a synchronous call that runs the workflow inline and returns the complete result in the HTTP response body. We do NOT use Kafka, RabbitMQ, or Celery.

**The alternative — fully async architecture:**
```
Client → POST /executions → 202 Accepted {execution_id}
                          ↓
                       Kafka Topic: workflow.requested
                          ↓
                       Worker pool → process → update DB
                          ↓
                       Client polls GET /executions/{id}
```

**Why synchronous is the right choice for this scope:**

*1. Latency profile:* Our workflows complete in 200–800ms (dominated by the simulated external API latency). This is entirely within acceptable HTTP response time bounds. Users get their answer immediately — no polling loop required.

*2. Debuggability:* A synchronous call produces a deterministic, traceable stack. When something goes wrong, the error surface is: HTTP request → route handler → orchestrator → stage → failure. An async system adds message broker lag, worker assignment, consumer group rebalancing, and dead-letter queue complexity to every debugging session.

*3. Operational complexity:* Kafka requires brokers, ZooKeeper/KRaft, consumer groups, offset management, and monitoring for consumer lag. For this scope, that is months of infrastructure work that provides no user-visible benefit.

*4. Idempotency is already solved:* Async systems need idempotency because message delivery can be "at least once." Our sync approach still needs idempotency (client retries on HTTP timeout), but the problem space is dramatically simpler. Redis + `X-Request-ID` handles it with a single key lookup.

**Where async becomes necessary (the scale threshold):**

| Signal | Migration trigger |
|---|---|
| Workflow execution exceeds 5 seconds (long-running external calls) | Async mandatory — HTTP timeout risk |
| Throughput > 500 req/s sustained | Worker pool saturation; queue provides natural backpressure |
| Workflows need to pause for human input (multi-day processes) | Sync HTTP cannot hold a connection for 48 hours |
| Need fan-out (one execution triggers multiple parallel sub-workflows) | DAG execution model needs async coordination |

**The upgrade path (zero breaking API change):**
```python
# Route handler change:
# BEFORE (sync):
run_workflow(execution_id=execution.id, db=db)
return _build_response(execution, db)

# AFTER (async with Celery):
task = run_workflow_task.delay(execution_id=execution.id)
# Return 202 Accepted with execution_id; client polls
return JSONResponse(status_code=202, content={"execution_id": execution.id})
```

The orchestrator, rules engine, and audit layer are unchanged. Only the route handler and task dispatch change. This is the value of the strict layering.

**Conclusion:** Synchronous REST is the correct choice for workflows that complete in under 2 seconds and at modest scale. It delivers simpler operations, better debuggability, and immediate user feedback. The architecture explicitly documents the async upgrade path, so the decision is reversible when the scale signals appear.

---

### 10.3 Relational Database with JSON Columns vs. Pure NoSQL Document Store

**Decision:** We use SQLite (dev) / PostgreSQL (prod) via SQLAlchemy, with `JSON` columns for `input_payload`, `context`, and `decision_trace`, rather than a document database such as MongoDB.

**The alternative — MongoDB:**
```javascript
// MongoDB document: natural fit for variable-shape workflow data
{
  "_id": "exec-abc",
  "workflow_id": "loan_approval_v1",
  "status": "APPROVED",
  "input": { "age": 32, "income": 75000, ... },
  "context": { "credit_score": 720 },
  "audit_logs": [
    { "event": "RULE_EVALUATED", "rule_id": "age_check", "result": "PASS" },
    ...
  ]
}
```

**Why we chose relational:**

*1. Audit logs are relational by nature:* Each audit log entry is a structured row with a fixed schema (execution_id FK, sequence_number, event_type, result, timestamps). The `(execution_id, sequence_number)` composite index — which gives O(log n) ordered trace retrieval — is a first-class relational concept. Achieving equivalent ordered queries in MongoDB requires careful index design and provides no advantage over a B-tree index.

*2. ACID guarantees:* An execution status update and its corresponding audit log entry must succeed or fail atomically. PostgreSQL's ACID transactions provide this for free. MongoDB offers multi-document transactions since 4.0, but they carry performance overhead and are less battle-tested than Postgres transactions.

*3. Foreign key integrity:* `audit_logs.execution_id` references `executions.id`. PostgreSQL enforces this at the database level, preventing orphaned audit rows if an execution is deleted. MongoDB has no native equivalent.

*4. The JSON column gives us NoSQL flexibility where we need it:* `input_payload`, `context`, and `decision_trace` are truly schema-free — they vary by workflow. PostgreSQL's `JSONB` column (used in production mode) provides:
   - Full JSON read/write without schema migration
   - GIN indexes on JSONB fields for fast JSON-path queries (e.g., `WHERE input_payload->>'employment_status' = 'employed'`)
   - `jsonb_path_query` for analytics across submissions

*5. Single technology, split benefits:* The relational schema governs the execution lifecycle (structured, queryable, FK-constrained). The JSON columns handle the variable-shape payload data. We get the benefits of both models without running two database systems.

**The cost of our choice:** PostgreSQL requires a schema migration when we add new columns (e.g., a `priority` field). MongoDB would not. However, new columns on the structured tables (`executions`, `audit_logs`) are rare — business logic changes are absorbed by the JSON columns. In practice, a schema migration occurs perhaps once per quarter.

**When MongoDB would win:**
- The workflow input schema varies so wildly that even `JSON` column queries are impractical.
- The team has zero SQL expertise and strong MongoDB expertise.
- Audit logs don't need to be queried relationally (no cross-execution analytics needed).

None of these conditions apply here.

**Conclusion:** A relational database with selective JSON columns gives us strong consistency guarantees for structured execution state, referential integrity for the audit trail, and schema flexibility for the variable workflow payloads — with a single, well-understood technology stack. NoSQL provides no net benefit for this access pattern.

---

## 11. Scaling Considerations

### Current Architecture Capacity (Rough Estimate)

| Component | Approximate Ceiling |
|---|---|
| FastAPI + uvicorn (single worker) | ~500 req/s (CPU-bound at rules evaluation) |
| SQLite | ~100 write req/s (single-writer architecture) |
| PostgreSQL (single node, connection pool) | ~2,000–5,000 write req/s |
| Redis (single node) | ~100,000 GET/SET operations/s |

### Path to 10,000 Requests/Second

**Step 1 — Horizontal API scaling (0 → 1,000 req/s):**
```
                    Load Balancer (nginx / AWS ALB)
                    /           |            \
          uvicorn worker   uvicorn worker   uvicorn worker
          (4 processes)    (4 processes)    (4 processes)
```
FastAPI is stateless. Multiple workers behind a load balancer require only that Redis (idempotency) and PostgreSQL (executions, audit_logs) are shared. No application code changes needed.

**Step 2 — Async workflow execution (1,000 → 5,000 req/s):**
```
POST /executions → 202 Accepted (execution_id)
                 → Celery task dispatched to Redis/RabbitMQ queue
                 → Celery worker pool processes workflow
                 → Client polls GET /executions/{id}
```
The API layer is freed from blocking on external dependency calls (the dominant latency source). Celery worker count scales independently of API worker count.

**Step 3 — Database sharding and read replicas (5,000+ req/s):**
- **Write path:** Shard `executions` by `workflow_id` hash. Each shard owns a subset of workflow types.
- **Read path:** PostgreSQL streaming replicas for `GET /executions/{id}` queries (reads outnumber writes typically 10:1).
- **Audit logs:** Consider streaming to Apache Kafka instead of direct DB writes. A consumer writes to a time-series store (ClickHouse, TimescaleDB) optimised for append-only, high-throughput writes. The relational DB only stores the final execution summary.

**Step 4 — Caching and query optimisation:**

| Bottleneck | Solution |
|---|---|
| YAML config reads | `@lru_cache` (already implemented) |
| `GET /executions/{id}` | Redis response cache (TTL = 30s for terminal states) |
| Audit log queries | Materialized view pre-aggregating `RULE_EVALUATED` rows per execution |
| PostgreSQL connection limit | PgBouncer connection pooler (`pool_mode=transaction`) |

**Step 5 — Database write scaling (PostgreSQL → write-optimised):**
For pure audit log throughput (10k writes/s):
- Buffer writes in-process (100ms flush window, batch INSERT)
- Or: Kafka → ClickHouse (columnar, optimised for high-ingest append workloads)
- The relational `audit_logs` table becomes an online store; ClickHouse becomes the analytics store.

---

## 12. Security Considerations

| Threat | Mitigation |
|---|---|
| Path traversal via `workflow_id` | Pydantic validator rejects `/` and `..`; config loader verifies path stays within workflows dir |
| Arbitrary code execution via rule config | `field_expression` uses regex-parsed arithmetic only; no `eval()` |
| Replay attacks | `X-Request-ID` deduplication prevents re-processing; TTL=24h limits window |
| Audit log tampering | No UPDATE issued against `audit_logs`; append-only enforced at application layer |
| SQL injection | All queries via SQLAlchemy ORM parameterized queries |
| Mass input flooding | Rate limiting should be added at the load balancer layer (not in scope) |

---

## 13. Assumptions and Constraints

| ID | Assumption |
|---|---|
| A1 | Workflow execution is synchronous and completes within a single HTTP request lifetime (<30s) |
| A2 | `X-Request-ID` uniqueness is the client's responsibility |
| A3 | The YAML workflow configs are trusted input (written by engineers, not end users) |
| A4 | `mock: true` in the YAML dependency config enables simulation mode; `mock: false` would call a real HTTP endpoint |
| A5 | SQLite is acceptable for development; PostgreSQL is required for production concurrency |
| A6 | Redis availability is non-critical; the in-memory fallback keeps the system functional at the cost of multi-process idempotency |

---

*Document maintained by the Engineering Team. Update this document when significant architectural decisions are made.*
