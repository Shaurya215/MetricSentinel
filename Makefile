# MetricSentinel — developer commands.
# Every recipe runs through `uv run`, so it behaves the same on Windows,
# macOS and the Ubuntu CI runner. Run `make` or `make help` for the list.

.DEFAULT_GOAL := help
.PHONY: help setup lint format test test-all golden data eda data-sample backtest detect scenarios scenarios-dev2017 backtest-dev eval-dev2017 eval-detection eval up down logs health clean

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
	uv run pytest -m "not integration and not eval and not golden"

test-all: ## Every test, including integration (needs `make up`)
	uv run pytest

golden: ## Reproduce the real-data Phase 3 baseline exactly (slow, needs the warehouse)
	uv run pytest -m golden tests/golden/test_real_baseline.py

data: ## Download/verify Olist and build the DuckDB warehouse + quality report
	uv run python -m pipelines.build

eda: ## Run the EDA on the built warehouse (writes data/reports/eda_findings.*)
	uv run python -m pipelines.eda

data-sample: ## Build a warehouse from FAKE data (no Kaggle needed) into data/synthetic
	uv run python -m pipelines.synthetic --out data/synthetic --orders 30000
	uv run python -m pipelines.build --raw-dir data/synthetic --warehouse data/synthetic/warehouse.duckdb --reports-dir data/synthetic/reports
	uv run python -m pipelines.eda --warehouse data/synthetic/warehouse.duckdb --out data/synthetic/reports

backtest: ## Rolling-origin forecast backtest on 2017; writes config/detector.json (Phase 3)
	uv run python -m ds.backtest

detect: ## Detect anomalies for one simulated day: make detect DATE=2018-03-05
	uv run python -m ds.detect --date $(DATE)

scenarios: ## Regenerate the 30-scenario fault benchmark (config/scenarios/mvp.yaml)
	uv run python -m pipelines.faults generate

scenarios-dev2017: ## Regenerate the 54-scenario 2017 development benchmark (seed 3101)
	uv run python -m pipelines.dev_benchmark

backtest-dev: ## Refit the baseline on the 2017 fit period only (config/detector_dev2017_baseline.json)
	uv run python -m ds.backtest --dev baseline

eval-dev2017: ## Detection evaluation on the 2017 development protocol
	uv run python -m evaluation.detection --protocol dev2017

eval-detection: ## Detection evaluation on the fault benchmark + clean 2018 days
	uv run python -m evaluation.detection

eval: eval-detection ## Run every evaluation suite that exists so far

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
