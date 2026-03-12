# Resilient Decision System

A **configuration-driven workflow execution engine** that processes structured business requests through auditable, rule-evaluated pipelines — with built-in failure handling, idempotency, and full explainability.

Supports any business domain (loan approvals, employee onboarding, document verification, vendor approvals) via YAML configuration — **zero code changes required** to add a new workflow.

---

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Running with Docker](#running-with-docker)
- [Running with Python venv](#running-with-python-venv)
- [API Usage](#api-usage)
- [Configuration Model](#configuration-model)
- [Project Structure](#project-structure)
- [Running Tests](#running-tests)
- [Scaling Considerations](#scaling-considerations)

---

## Features

| Capability | Implementation |
|---|---|
| **Configuration-driven** | Workflows and rules defined in YAML — no code deploys for rule changes |
| **State machine** | Every execution follows PENDING → RUNNING → APPROVED / REJECTED / MANUAL_REVIEW / FAILED |
| **Rules engine** | 15+ operators: `gte`, `lte`, `in`, `not_in`, `equals`, `regex`, `is_true`, `is_null`, arithmetic expressions, and more |
| **External dependencies** | Pluggable stage type with simulated 20% failure rate |
| **Exponential backoff** | Configurable retry policy per stage: max attempts, base delay, jitter, retryable HTTP codes |
| **Idempotency** | Redis (or in-memory fallback) deduplication via `X-Request-ID` header |
| **Full audit trail** | Immutable, append-only log of every rule evaluation, state transition, and retry attempt |
| **Decision trace** | Human-readable `decision_trace` on every response explaining exactly why a decision was made |
| **Multi-workflow** | Two example workflows included: loan approval + employee onboarding |

---

## Quick Start

```bash
git clone <repo>
cd resilient-decision-system

# Install dependencies
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Start the server (SQLite, no Redis required)
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

## Running with Docker

```bash
# Build and run the full stack (app + Redis + PostgreSQL)
docker compose up --build

# Services:
#   API:        http://localhost:8000
#   Swagger UI: http://localhost:8000/docs
#   PostgreSQL: localhost:5432
#   Redis:      localhost:6379
```

**`docker-compose.yml`**

```yaml
version: "3.9"
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://decisions:decisions@db/decisions
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - db
      - redis

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: decisions
      POSTGRES_USER: decisions
      POSTGRES_PASSWORD: decisions
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine

volumes:
  pgdata:
```

---

## Running with Python venv

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Configure environment
cp .env.example .env
# Edit .env to set DATABASE_URL and REDIS_URL

# 4. Run the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Default: SQLite (./decisions.db) + in-memory idempotency fallback
# Production: Set DATABASE_URL=postgresql://... and REDIS_URL=redis://...
```

**`.env.example`**

```env
DATABASE_URL=sqlite:///./decisions.db
REDIS_URL=
LOG_LEVEL=INFO
```

---

## API Usage

### Submit a Workflow Execution

```bash
curl -X POST http://localhost:8000/executions \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $(uuidgen)" \
  -d '{
    "workflow_id": "loan_approval_v1",
    "input_data": {
      "applicant_id": "APP-001",
      "full_name": "Alice Johnson",
      "age": 35,
      "annual_income": 95000,
      "requested_amount": 18000,
      "employment_status": "employed",
      "existing_debt": 5000
    }
  }'
```

**Response (HTTP 201 Created):**

```json
{
  "id": "f3a2b1c0-...",
  "status": "APPROVED",
  "decision_trace": {
    "final_status": "APPROVED",
    "summary": "Approved: all 3 stage(s) passed with no mandatory failures.",
    "mandatory_failures": [],
    "rule_trace": [...]
  },
  "audit_trail": [...]
}
```

### Idempotency — Replay a Request

```bash
# Using the same X-Request-ID returns the cached result (HTTP 200)
curl -X POST http://localhost:8000/executions \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: SAME-UUID-AS-BEFORE" \
  -d '{ ... }'
# → HTTP 200 (cached, not re-processed)
```

### Retrieve an Execution

```bash
curl http://localhost:8000/executions/{execution_id}
```

### List Executions

```bash
# All executions
curl "http://localhost:8000/executions?limit=20&offset=0"

# Filter by workflow
curl "http://localhost:8000/executions?workflow_id=loan_approval_v1"

# Filter by status
curl "http://localhost:8000/executions?status=MANUAL_REVIEW"
```

### List Available Workflows

```bash
curl http://localhost:8000/workflows
# → {"workflows": ["loan_approval_v1", "employee_onboarding_v1"]}
```

### Health Check

```bash
curl http://localhost:8000/health
# → {"status": "ok", "version": "1.0.0"}
```

---

## Configuration Model

Adding a new workflow requires **only a new YAML file** in `app/workflows/`. No Python changes.

```yaml
# app/workflows/my_new_workflow.yaml

workflow_id: "my_new_workflow_v1"
name: "My New Workflow"
version: "1.0.0"

input_schema:
  type: object
  required: [applicant_id, amount]
  properties:
    applicant_id: { type: string }
    amount:       { type: number, minimum: 0 }

stages:
  - stage_id: "eligibility_check"
    type: rule_evaluation
    on_failure: reject
    rules:
      - rule_id: "amount_limit"
        field: "input.amount"
        operator: lte
        threshold: 50000
        severity: mandatory
        description: "Amount must be under $50,000"

  - stage_id: "external_verification"
    type: external_dependency
    on_failure: manual_review
    dependency:
      service_id: "my_verification_api"
      mock: true
      mock_config:
        failure_rate: 0.10
        response_field: "verified"
        latency_ms: [100, 300]
    retry_policy:
      max_attempts: 3
      backoff_strategy: exponential
      base_delay_ms: 200

decision_logic:
  approved_if: all_mandatory_pass
  manual_review_if: [any_soft_fail]
  rejected_if: [any_mandatory_fail]

metadata:
  owner: "my_team"
  review_queue: "my_review_queue"
  sla_hours: 24
```

**Available rule operators:**

| Operator | Description | Example |
|---|---|---|
| `gte` / `lte` / `gt` / `lt` | Numeric comparison | `amount > 1000` |
| `equals` / `not_equals` | Equality | `status == "active"` |
| `in` / `not_in` | Membership | `employment in [employed, self_employed]` |
| `contains` | String contains | `name contains "LLC"` |
| `regex` | Regex match | `id matches "^[A-Z]{3}"` |
| `is_true` / `is_false` | Boolean check | `consented == true` |
| `is_null` / `is_not_null` | Null check | `score is not null` |
| `field_expression` | Arithmetic | `debt / income <= 0.40` |

---

## Project Structure

```
resilient-decision-system/
├── app/
│   ├── main.py                    # FastAPI app, routes, DI wiring
│   ├── config.py                  # Pydantic-Settings configuration
│   ├── core/
│   │   ├── orchestrator.py        # Workflow driver (state machine)
│   │   └── rules_engine.py        # Rule evaluator (pure, stateless)
│   ├── dependencies/
│   │   └── mock_api.py            # External service mocks + retry decorator
│   ├── models/
│   │   ├── db.py                  # SQLAlchemy ORM models
│   │   └── schemas.py             # Pydantic request/response schemas
│   ├── services/
│   │   ├── config_loader.py       # YAML workflow config registry
│   │   └── idempotency.py         # Redis-backed idempotency service
│   └── workflows/
│       ├── loan_approval_v1.yaml
│       └── employee_onboarding_v1.yaml
├── tests/
│   └── test_engine.py             # 49 tests covering all scenarios
├── docs/
│   └── architecture.md            # Full architecture + trade-off defence
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## Running Tests

```bash
# Run all 49 tests
pytest tests/test_engine.py -v

# Run a specific test category
pytest tests/test_engine.py -v -k "idempotency"
pytest tests/test_engine.py -v -k "TestRulesEngine"
pytest tests/test_engine.py -v -k "retry"

# With coverage report
pip install pytest-cov
pytest tests/test_engine.py --cov=app --cov-report=term-missing
```

**Test categories:**

| Category | Tests |
|---|---|
| Happy path / routing | APPROVED, REJECTED, MANUAL_REVIEW scenarios |
| Idempotency | Duplicate request detection, separate request isolation |
| Retry & resilience | Backoff decorator, partial failures, total exhaustion |
| Rule change | Config hot-reload, threshold tightening/relaxation |
| Input validation | Missing fields, invalid enums, path traversal |
| Rules engine unit | All 15 operators, field expressions, error handling |
| RetryConfig unit | Exponential, linear, constant backoff, delay cap |
| IdempotencyService unit | Miss, hit, TTL expiry, key isolation |
| API endpoints | GET by ID, 404, list workflows, health |

---

## Scaling Considerations

### Database

- **Development**: SQLite (`sqlite:///./decisions.db`) — zero configuration, single process
- **Production**: PostgreSQL with connection pooling via PgBouncer (`pool_mode=transaction`, `max_pool_size=100`)
- **Indexes already in place**: `executions.request_id` (idempotency lookups), `executions.status` (queue filtering), `audit_logs.(execution_id, sequence_number)` (trace retrieval)
- **Audit log scaling**: At 10k req/s, switch audit log writes to a batch-insert buffer (flush every 100ms) or stream to Kafka → ClickHouse for analytics

### Horizontal Scaling

The API layer is **stateless** — any number of uvicorn workers can run behind a load balancer. The only shared state is:

1. PostgreSQL (execution rows + audit logs)
2. Redis (idempotency keys)

Both are naturally shared across workers. Scale API workers independently of database workers.

```
Load Balancer (nginx / AWS ALB)
    │         │         │
 Worker 1  Worker 2  Worker 3   ← uvicorn (4 processes each)
    └─────────┼─────────┘
              │
    ┌─────────┴──────────┐
    │   PostgreSQL        │   ← primary + 2 read replicas
    │   Redis Cluster     │   ← 3 primaries for idempotency
    └────────────────────┘
```

### Moving to Async (Celery + Redis)

The current synchronous design is correct for workflows completing in <2 seconds. For long-running workflows or sustained load >1,000 req/s:

```python
# app/main.py — change in the route handler only
# BEFORE (sync):
run_workflow(execution_id=execution.id, db=db)

# AFTER (async with Celery):
from app.tasks import run_workflow_task
run_workflow_task.delay(execution_id=execution.id)
return JSONResponse(status_code=202, content={"execution_id": execution.id})
```

The orchestrator, rules engine, and audit layer are **unchanged**. The Celery task wraps the same `run_workflow()` call.

```bash
# Start Celery worker
celery -A app.tasks worker --concurrency=16 --loglevel=info

# Monitor
celery -A app.tasks flower
```

### Config Caching

Workflow YAML configs are cached in-process via `@functools.lru_cache`. In a multi-process deployment, each worker caches independently. For hot-reload without restart:

```python
# Admin endpoint to invalidate config cache
@app.post("/admin/reload-configs")
def reload_configs():
    load_workflow_config.cache_clear()
    return {"cleared": True}
```

### Recommended Production Stack

| Component | Recommendation |
|---|---|
| API | 3+ uvicorn containers behind AWS ALB |
| Database | AWS RDS PostgreSQL (Multi-AZ), `db.r6g.xlarge` |
| Cache/Queue | AWS ElastiCache Redis 7 (cluster mode) |
| Async workers | Celery on ECS (auto-scale on queue depth) |
| Monitoring | Datadog APM + custom metrics on execution latency per workflow |
| Config storage | S3-backed YAML configs with hot-reload on change event |
