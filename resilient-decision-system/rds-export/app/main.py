"""
app/main.py
─────────────────────────────────────────────────────────────────────────────
FastAPI application — entry point for uvicorn.

Wires together:
  • Database engine + session factory
  • IdempotencyService
  • API router
  • Startup/shutdown lifecycle hooks
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.db import Execution, ExecutionStatus, get_engine, get_session_factory, init_db
from app.models.schemas import (
    ErrorResponse,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionSummary,
)
from app.core.orchestrator import run_workflow
from app.services.config_loader import list_available_workflows, load_workflow_config
from app.services.idempotency import IdempotencyService

logger  = logging.getLogger(__name__)
settings = get_settings()

# ── Database singleton ────────────────────────────────────────────────────
engine         = get_engine(settings.database_url)
SessionFactory = get_session_factory(engine)

# ── Idempotency singleton ─────────────────────────────────────────────────
idempotency_svc = IdempotencyService(
    redis_url=settings.redis_url,
    ttl_seconds=settings.idempotency_ttl_seconds,
)


# ── DB session dependency ─────────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


# ── App lifecycle ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level)
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    init_db(engine)
    logger.info("Database initialised at: %s", settings.database_url)
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Configuration-driven workflow decision engine. "
        "Supports any business workflow defined in YAML — "
        "loan approvals, employee onboarding, document verification, and more."
    ),
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════

@app.post(
    "/executions",
    response_model=ExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a workflow execution request",
    responses={
        200: {"description": "Idempotency hit — returning cached result"},
        400: {"model": ErrorResponse, "description": "Invalid input or unknown workflow"},
        500: {"model": ErrorResponse, "description": "Internal engine error"},
    },
)
def create_execution(
    payload: ExecutionRequest,
    x_request_id: str = Header(
        default_factory=lambda: str(uuid.uuid4()),
        description="Client-supplied idempotency key (UUID v4 recommended)",
    ),
    db: Session = Depends(get_db),
):
    """
    Submit a new workflow execution.

    **Idempotency**: Supply the same `X-Request-ID` to replay a request
    without re-processing. The cached result is returned immediately (HTTP 200).

    **Workflow config**: The `workflow_id` must match a YAML file in
    `app/workflows/`. Input is validated against that file's `input_schema`.
    """
    # ── 1. Idempotency check ──────────────────────────────────────────────
    existing_execution_id = idempotency_svc.check(x_request_id)
    if existing_execution_id:
        existing = db.query(Execution).filter(Execution.id == existing_execution_id).first()
        if existing:
            logger.info("Idempotency hit for request_id=%s → execution=%s",
                        x_request_id, existing_execution_id)
            response = _build_response(existing, db)
            return JSONResponse(status_code=200, content=response.model_dump(mode="json"))

    # ── 2. Validate workflow exists ───────────────────────────────────────
    try:
        load_workflow_config(payload.workflow_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workflow_id: '{payload.workflow_id}'. "
                   f"Available: {list_available_workflows()}",
        )

    # ── 3. Create Execution row ───────────────────────────────────────────
    execution = Execution(
        request_id      = x_request_id,
        workflow_id     = payload.workflow_id,
        input_payload   = payload.input_data,
        context         = {},
        status          = ExecutionStatus.PENDING,
    )
    db.add(execution)
    db.flush()  # get the auto-generated id before running

    # ── 4. Register idempotency key ───────────────────────────────────────
    idempotency_svc.register(x_request_id, execution.id)

    # ── 5. Run the workflow ───────────────────────────────────────────────
    try:
        run_workflow(execution_id=execution.id, db=db)
        db.commit()
        db.refresh(execution)
    except Exception as exc:
        db.rollback()
        logger.exception("Workflow execution failed for request_id=%s: %s", x_request_id, exc)
        raise HTTPException(status_code=500, detail=f"Engine error: {exc}") from exc

    # ── 6. Build and return response ──────────────────────────────────────
    return _build_response(execution, db)


@app.get(
    "/executions/{execution_id}",
    response_model=ExecutionResponse,
    summary="Retrieve a workflow execution by ID",
)
def get_execution(execution_id: str, db: Session = Depends(get_db)):
    """Fetch a single execution with its full audit trail."""
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found.")
    return _build_response(execution, db)


@app.get(
    "/executions",
    response_model=list[ExecutionSummary],
    summary="List recent workflow executions",
)
def list_executions(
    workflow_id: str | None = None,
    status:      str | None = None,
    limit:       int        = 20,
    offset:      int        = 0,
    db: Session = Depends(get_db),
):
    """Paginated list of executions, optionally filtered by workflow_id or status."""
    q = db.query(Execution)
    if workflow_id:
        q = q.filter(Execution.workflow_id == workflow_id)
    if status:
        q = q.filter(Execution.status == status)
    executions = q.order_by(Execution.created_at.desc()).offset(offset).limit(limit).all()
    return [ExecutionSummary.model_validate(e) for e in executions]


@app.get("/workflows", summary="List available workflow configurations")
def list_workflows():
    """Return the workflow_ids of all loaded YAML configs."""
    return {"workflows": list_available_workflows()}


@app.get("/health", summary="Health check")
def health_check():
    return {"status": "ok", "version": settings.app_version}


# ── Response builder helper ───────────────────────────────────────────────

def _build_response(execution: Execution, db: Session) -> ExecutionResponse:
    """Build a full ExecutionResponse including audit trail from ORM objects."""
    from app.models.db import AuditLog
    from app.models.schemas import AuditLogEntry

    logs = (
        db.query(AuditLog)
        .filter(AuditLog.execution_id == execution.id)
        .order_by(AuditLog.sequence_number)
        .all()
    )
    audit_trail = [AuditLogEntry.model_validate(log) for log in logs]

    return ExecutionResponse(
        id              = execution.id,
        request_id      = execution.request_id,
        workflow_id     = execution.workflow_id,
        status          = execution.status,
        current_stage   = execution.current_stage,
        input_payload   = execution.input_payload,
        context         = execution.context or {},
        decision_trace  = execution.decision_trace,
        error_message   = execution.error_message,
        created_at      = execution.created_at,
        updated_at      = execution.updated_at,
        completed_at    = execution.completed_at,
        audit_trail     = audit_trail,
    )
