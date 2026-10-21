# pipelines

The Phase 2 data pipeline: Olist CSVs -> verified raw files -> DuckDB warehouse -> quality report.

| Module | Job |
|---|---|
| `config.py` | Source files, published row counts, allowed values, thresholds (PROVISIONAL ones are marked) |
| `download.py` | Find the data (CSV, zip, or Kaggle API), verify checksums (trust on first use) |
| `validate.py` | Hard checks: Pandera schemas, foreign keys, row counts |
| `sql_models/staging/`, `sql_models/marts/` | The transformations, as plain SQL files run in filename order |
| `build.py` | Orchestrates everything; atomic; `python -m pipelines.build` is `make data` |
| `quality.py` | Soft checks, statistics, mart checks, hashes, HTML/JSON report |
| `eda.py` | Monitoring window, seasonality, KPI distributions, sample-size rule |
| `synthetic.py` | Generates FAKE Olist-shaped data for tests and `make data-sample`. Never use for results |

Layers: `raw` (text, unchanged) -> `staging` (typed, cleaned, pseudonymised) -> `marts` (facts and dimensions).
Design and decisions: `docs/DATA_PLAN.md` sections 4-8 and 14.
