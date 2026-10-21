# MetricSentinel — developer commands.
# Every recipe runs through `uv run`, so it behaves the same on Windows,
# macOS and the Ubuntu CI runner. Run `make` or `make help` for the list.

.DEFAULT_GOAL := help
.PHONY: help setup lint format test test-all data eda data-sample eval up down logs health clean

PY := uv run python

help: ## Show this help
	@$(PY) -c "import re; [print(f\"  {m[0]:<10} {m[1]}\") for m in re.findall(r\"^([a-z-]+):.*?## (.*)$$\", open(\"Makefile\").read(), re.M)]"

setup: ## Install locked dependencies, git hooks and a local .env
	uv sync --frozen
	uv run pre-commit install
	@$(PY) -c "import pathlib, shutil; e = pathlib.Path(\".env\"); e.exists() or shutil.copy(\".env.example\", e); print(\".env ready\")"

lint: ## Ruff lint + format check, then mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-fix lint issues and format the code
	uv run ruff check --fix .
	uv run ruff format .

test: ## Fast test suite (no Postgres, no warehouse, no evals)
	uv run pytest -m "not integration and not eval"

test-all: ## Every test, including integration (needs `make up`)
	uv run pytest

data: ## Download/verify Olist and build the DuckDB warehouse + quality report
	uv run python -m pipelines.build

eda: ## Run the EDA on the built warehouse (writes data/reports/eda_findings.*)
	uv run python -m pipelines.eda

data-sample: ## Build a warehouse from FAKE data (no Kaggle needed) into data/synthetic
	uv run python -m pipelines.synthetic --out data/synthetic --orders 30000
	uv run python -m pipelines.build --raw-dir data/synthetic --warehouse data/synthetic/warehouse.duckdb --reports-dir data/synthetic/reports
	uv run python -m pipelines.eda --warehouse data/synthetic/warehouse.duckdb --out data/synthetic/reports

eval: ## Run the evaluation suites (Phase 3+)
	@echo "make eval: not implemented yet - the first suite (detection) lands in Phase 3." && exit 1

up: ## Build images and start Postgres, API and UI
	docker compose up -d --build --wait
	@echo "API: http://localhost:8000/health   Docs: http://localhost:8000/docs   UI: http://localhost:8501"

down: ## Stop the stack (data volume is kept)
	docker compose down

logs: ## Follow logs from all services
	docker compose logs -f

health: ## Call GET /health on the running API
	@$(PY) -c "import urllib.request as u; r = u.urlopen(\"http://localhost:8000/health\", timeout=5); print(r.status, r.read().decode())"

clean: ## Remove caches and coverage output
	@$(PY) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for n in (\".pytest_cache\", \".mypy_cache\", \".ruff_cache\", \"htmlcov\", \"__pycache__\") for p in pathlib.Path(\".\").rglob(n) if \".venv\" not in p.parts]; pathlib.Path(\".coverage\").unlink(missing_ok=True)"
