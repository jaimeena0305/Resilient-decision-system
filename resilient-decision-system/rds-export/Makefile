# ── Resilient Decision System — Developer Makefile ────────────────────────

.PHONY: install run test test-cov docker-up docker-down clean lint help

# ── Setup ─────────────────────────────────────────────────────────────────

install:
	@echo "Creating virtual environment..."
	python -m venv .venv
	@echo "Installing dependencies..."
	.venv/bin/pip install -r requirements.txt
	@echo "\n✅ Done. Activate with: source .venv/bin/activate"

# ── Run ───────────────────────────────────────────────────────────────────

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-prod:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# ── Tests ────────────────────────────────────────────────────────────────

test:
	pytest tests/test_engine.py -v

test-cov:
	pytest tests/test_engine.py -v \
		--cov=app \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@echo "\n📊 Coverage report: open htmlcov/index.html"

test-fast:
	pytest tests/test_engine.py -v -m "not slow"

# ── Docker ────────────────────────────────────────────────────────────────

docker-up:
	docker compose up --build -d
	@echo "\n🚀 API running at http://localhost:8000"
	@echo "📖 Swagger UI at http://localhost:8000/docs"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

# ── Demo ─────────────────────────────────────────────────────────────────

demo-approve:
	@echo "Submitting a loan application that should be APPROVED...\n"
	curl -s -X POST http://localhost:8000/executions \
		-H "Content-Type: application/json" \
		-H "X-Request-ID: demo-approve-$$(date +%s)" \
		-d '{"workflow_id":"loan_approval_v1","input_data":{"applicant_id":"DEMO01","full_name":"Alice Johnson","age":35,"annual_income":95000,"requested_amount":18000,"employment_status":"employed","existing_debt":5000}}' \
		| python -m json.tool

demo-reject:
	@echo "Submitting a loan application that should be REJECTED...\n"
	curl -s -X POST http://localhost:8000/executions \
		-H "Content-Type: application/json" \
		-H "X-Request-ID: demo-reject-$$(date +%s)" \
		-d '{"workflow_id":"loan_approval_v1","input_data":{"applicant_id":"DEMO02","full_name":"Bob Smith","age":28,"annual_income":55000,"requested_amount":12000,"employment_status":"employed","existing_debt":3000}}' \
		| python -m json.tool

demo-idempotency:
	@echo "Sending the same request twice to demonstrate idempotency...\n"
	@REQ_ID="idempotency-test-$$(date +%s)"; \
	echo "First call (expect 201):"; \
	curl -s -o /dev/null -w "HTTP %%{http_code}\n" -X POST http://localhost:8000/executions \
		-H "Content-Type: application/json" \
		-H "X-Request-ID: $$REQ_ID" \
		-d '{"workflow_id":"loan_approval_v1","input_data":{"applicant_id":"DEMO03","full_name":"Carol White","age":40,"annual_income":80000,"requested_amount":15000,"employment_status":"employed","existing_debt":2000}}'; \
	echo "Second call (expect 200 — cached):"; \
	curl -s -o /dev/null -w "HTTP %%{http_code}\n" -X POST http://localhost:8000/executions \
		-H "Content-Type: application/json" \
		-H "X-Request-ID: $$REQ_ID" \
		-d '{"workflow_id":"loan_approval_v1","input_data":{"applicant_id":"DEMO03","full_name":"Carol White","age":40,"annual_income":80000,"requested_amount":15000,"employment_status":"employed","existing_debt":2000}}'

# ── Maintenance ───────────────────────────────────────────────────────────

lint:
	ruff check app/ tests/

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null; true
	rm -rf htmlcov/ .coverage coverage.xml
	rm -f decisions.db

help:
	@echo ""
	@echo "Resilient Decision System — Available Commands"
	@echo "─────────────────────────────────────────────"
	@echo "  make install         Install dependencies in a venv"
	@echo "  make run             Start the dev server (SQLite)"
	@echo "  make test            Run the full test suite (49 tests)"
	@echo "  make test-cov        Run tests with HTML coverage report"
	@echo "  make docker-up       Start app + PostgreSQL + Redis via Docker"
	@echo "  make docker-down     Stop all Docker services"
	@echo "  make demo-approve    Send an approvable loan request to localhost"
	@echo "  make demo-reject     Send a rejectable loan request to localhost"
	@echo "  make demo-idempotency Demonstrate idempotency (201 then 200)"
	@echo "  make clean           Remove build artefacts and temp files"
	@echo ""
