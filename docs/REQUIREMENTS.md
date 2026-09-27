# Requirements — MetricSentinel

> Status: Phase 0 planning document. **All requirements are planned; none are implemented.**
> Priority uses MoSCoW: **M** = Must, **S** = Should, **C** = Could.
> Scope labels: `[MVP]`, `[EXT]`, `[FUTURE]`. The phase shown is where each requirement is planned to be delivered (see `ROADMAP.md`).

---

## 1. Functional requirements

### 1.1 Data ingestion and storage

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-001 | Download the Olist dataset with a script, and verify the files with checksums before loading. | M | MVP | 2 |
| FR-002 | Load all 9 Olist tables into a raw layer in DuckDB, without changing any values. | M | MVP | 2 |
| FR-003 | Validate each table against a declared schema: types, nullability, allowed values, key uniqueness and foreign-key coverage. The pipeline must stop on a hard failure. | M | MVP | 2 |
| FR-004 | Build cleaned staging tables. This includes timestamp parsing, category translation to English, duplicate-review handling, and pseudonymised customer identifiers. | M | MVP | 2 |
| FR-005 | Build modelled tables: `fct_orders`, `fct_order_items`, `fct_payments`, `fct_reviews`, `dim_customers`, `dim_sellers`, `dim_products`, `dim_date`. | M | MVP | 2 |
| FR-006 | Write a data-quality report for each load: row counts, null rates, failed checks and the data version hash. | M | MVP | 2 |
| FR-007 | Provide a simulated clock, so that only data up to the "current" simulated date is visible to detection and investigation. | M | MVP | 3 |

### 1.2 Metric store

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-010 | Maintain a metric catalogue (YAML). Each KPI has a name, SQL definition, unit, direction of "bad", minimum sample size and owner text. | M | MVP | 3 |
| FR-011 | Compute daily values for each KPI, both in total and for each configured dimension value (state, category, payment type, seller tier). | M | MVP | 3 |
| FR-012 | Store each daily value with its sample size (denominator), so that small segments can be filtered or down-weighted. | M | MVP | 3 |

### 1.3 Fault injection (evaluation support)

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-020 | Inject parameterised faults into a copy of the modelled data, never into the raw layer. | M | MVP | 3 |
| FR-021 | Support at least these fault types: segment level shift, seller delivery delay, payment-method failure, category price change, review-score collapse, and pipeline bug (duplicated or dropped rows). | M | MVP | 3 |
| FR-022 | Record a ground-truth label for every injected fault: type, affected metric, dimension and value, start date and magnitude. | M | MVP | 3 |
| FR-023 | Generate a scenario benchmark: 30 scenarios for MVP, extended to 100. | M / S | MVP / EXT | 3 / 7 |

### 1.4 Anomaly detection

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-030 | Produce a forecast with a prediction interval for each monitored series, using only data before the evaluation date. | M | MVP | 3 |
| FR-031 | Flag a series when the observed value falls outside the interval, and compute a p-value (or equivalent score) for it. | M | MVP | 3 |
| FR-032 | Apply Benjamini–Hochberg false-discovery-rate control across all series tested on a given day. | M | MVP | 3 |
| FR-033 | Group related flags (for example, a total KPI and its segments on the same day) into one anomaly record. | S | MVP | 3 |
| FR-034 | Store each anomaly with: metric, date, observed value, expected value, interval, score, and related segments. | M | MVP | 3 |
| FR-035 | Compare forecasting methods using a rolling-origin backtest, and log the results to MLflow. | M | MVP | 3 |

### 1.5 SQL tool and retrieval

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-040 | Translate a natural-language data question into a DuckDB SQL query, using retrieved schema context. | M | MVP | 4 |
| FR-041 | Accept only a single `SELECT` statement on allow-listed tables. Enforce this by parsing with sqlglot, not by instructions to the model. | M | MVP | 4 |
| FR-042 | Run queries over a read-only connection, with a row limit and a time limit. | M | MVP | 4 |
| FR-043 | On a SQL error, retry at most twice, passing the database error back to the model. | S | MVP | 4 |
| FR-044 | Record every executed query in a query ledger: id, SQL, parameters, row count, result hash, result preview, duration and simulated date. | M | MVP | 4 |
| FR-045 | Index schema documentation, the metric catalogue and approved incident reports for retrieval. | M | MVP | 4 |
| FR-046 | Retrieve with hybrid search (keyword and vector) and merge the results with reciprocal rank fusion. | M | MVP | 4 |
| FR-047 | Rerank retrieved results with a cross-encoder. | S | EXT | 7 |
| FR-048 | Enforce the simulated clock in SQL, so that queries cannot read data after the current simulated date. | M | MVP | 4 |

### 1.6 Investigation

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-050 | Start an investigation automatically for each new anomaly above a configurable severity, or manually through the API. | M | MVP | 5 |
| FR-051 | The agent generates hypotheses, chooses tools, and records each hypothesis as supported, rejected or inconclusive, with evidence ledger IDs. | M | MVP | 5 |
| FR-052 | Provide deterministic statistical tools: contribution (mix vs rate) decomposition, segment comparison tests, and change-point detection. | M | MVP | 5 |
| FR-053 | Stop the investigation on any of: confidence threshold reached, 12 tool calls, token budget reached, or 5 minutes wall time. | M | MVP | 5 |
| FR-054 | Output a ranked list of up to 3 root-cause candidates, each with evidence and a confidence label. | M | MVP | 5 |
| FR-055 | Check whether the anomaly is likely a data-quality issue before business explanations are considered. | S | MVP | 5 |

### 1.7 Verification and reporting

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-060 | Extract every numeric claim from a draft report, and match it to a ledger result within a stated tolerance. | M | MVP | 5 |
| FR-061 | Check that claimed directions (increase or decrease) and segment names are consistent with the evidence. | M | MVP | 5 |
| FR-062 | Return a draft that fails verification to the agent once for correction. If it fails again, mark it "unverified" and still send it to review, clearly labelled. | M | MVP | 5 |
| FR-063 | Render a Markdown report containing: summary, anomaly chart, contribution chart, root-cause candidates, evidence table with links to SQL, and limitations. | M | MVP | 5 |

### 1.8 Approval, API and UI

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-070 | An approver can approve, edit or reject a report, and must give a reason when rejecting. | M | MVP | 6 |
| FR-071 | Only approved reports can be published (MVP: exported as a file and posted to a local "outbox"). | M | MVP | 6 |
| FR-072 | Store approved reports as incident memory, and index them for retrieval. | M | MVP | 6 |
| FR-073 | Provide REST API endpoints for KPIs, anomalies, investigations, reports, decisions and admin simulation control. | M | MVP | 6 |
| FR-074 | Provide a review UI: anomaly feed, report view with evidence, and approval actions. | M | MVP | 6 |
| FR-075 | Allow users to ask ad-hoc data questions, answered through the same SQL tool, ledger and verifier. | C | EXT | 7 |
| FR-076 | Publish approved reports to a real channel (email or Slack webhook). | C | EXT | 9 |
| FR-077 | Run the daily detection → investigation cycle on a schedule, or through an "advance simulated day" admin action. | M | MVP | 6 |

### 1.9 Evaluation

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-080 | Run detection evaluation (precision, recall, detection delay, false alarms per 30 days) with one command. | M | MVP | 3 |
| FR-081 | Run text-to-SQL evaluation (execution accuracy, valid-SQL rate) on a human-written test set: 100 questions for MVP, 150 for EXT. | M | MVP | 4 |
| FR-082 | Run root-cause evaluation (hit@1, hit@3, steps, tokens, cost, wall time) on the scenario benchmark. | M | MVP | 5 |
| FR-083 | Run retrieval evaluation (recall@k, MRR) on questions with gold tables and definitions. | M | MVP | 4 |
| FR-084 | Run report faithfulness evaluation: support rate, plus an LLM-judge score checked against a human-labelled sample. | S | MVP / EXT | 5 / 7 |
| FR-085 | Run a security test suite: prompt injection, indirect injection in data, forbidden SQL and exfiltration attempts. | M | EXT | 7 |
| FR-086 | Log every evaluation result with its git SHA, prompt version, model version and data version. | M | MVP | 3 |
| FR-087 | Run CI regression gates: a fast suite on each pull request, and the full suite nightly or on demand. | M | EXT | 7 |
| FR-088 | Compare configurations with a paired significance test (McNemar's test or paired bootstrap). | S | EXT | 7 |

### 1.10 Fine-tuning (gated)

| ID | Requirement | Priority | Scope | Phase |
|---|---|---|---|---|
| FR-090 | Record a written go/no-go decision for fine-tuning, based on Phase 4 and 5 measurements against the criteria in `AI_ML_PLAN.md`. | M | EXT | 8 |
| FR-091 | If go: build a training set (synthetic data verified by execution) with splits by template family. | M | EXT | 8 |
| FR-092 | If go: train with SFT (QLoRA), then GRPO with an execution-match reward, and track all runs in MLflow. | M | EXT | 8 |
| FR-093 | Report the four-way comparison: base, RAG, fine-tuned, and RAG + fine-tuned, plus a hosted-model reference. | M | EXT | 8 |
| FR-094 | Serve the selected model locally through the same model interface, so it can be swapped by configuration. | M | EXT | 8 |

---

## 2. Non-functional requirements

Targets are **initial targets**. They are not results, and they may be revised after baselines are measured. Every revision will be recorded in `ROADMAP.md`.

| ID | Category | Requirement | Scope |
|---|---|---|---|
| NFR-001 | Cost | The full system must run with zero spend: free tiers and open-source software only. Any paid option must be optional and documented. | MVP |
| NFR-002 | Portability | Everything except model training starts with `docker compose up` on a laptop with 16 GB RAM and no GPU. | MVP |
| NFR-003 | Offline mode | A documented local-only mode (Ollama models) runs the full loop with no external API calls. Quality may be lower, and this must be measured. | MVP |
| NFR-004 | Performance | Daily detection over all monitored series completes in under 60 s on a laptop. | MVP |
| NFR-005 | Performance | One investigation completes in under 5 minutes wall time (hard stop, see FR-053). | MVP |
| NFR-006 | Performance | API read endpoints respond in under 300 ms p95, excluding LLM work. | MVP |
| NFR-007 | Reliability | Investigations are checkpointed after each tool call, so a crashed run can be resumed or cleanly failed. | MVP |
| NFR-008 | Reliability | LLM calls use timeouts, bounded retries with backoff, and a configured fallback model. | MVP |
| NFR-009 | Security | Agent tools can only read. The analytics database is opened read-only, and no tool can write, send or delete. | MVP |
| NFR-010 | Security | SQL is validated by parsing (single `SELECT`, allow-listed tables, no file or extension functions). | MVP |
| NFR-011 | Security | Text returned from data (for example, review comments) is marked as untrusted and is never executed as instructions. | MVP |
| NFR-012 | Security | Role-based access: `viewer`, `analyst`, `approver`, `admin`. Only `approver` can approve or publish. | MVP |
| NFR-013 | Security | Secrets are loaded from environment variables. They never appear in prompts, logs or the repository. | MVP |
| NFR-014 | Privacy | Row-level customer data is never sent to hosted LLM APIs. Only schema, aggregates and query previews that pass PII checks may be sent. | MVP |
| NFR-015 | Auditability | Every tool call, report version and approval decision is written to an append-only audit table. | MVP |
| NFR-016 | Reproducibility | Every result records the git SHA, data hash, prompt version, model ID and random seed. | MVP |
| NFR-017 | Observability | Structured JSON logs with a run ID on every line. | MVP |
| NFR-018 | Observability | Full traces for LLM calls and tool calls (OpenTelemetry → Phoenix). | EXT |
| NFR-019 | Maintainability | Type hints throughout, ruff clean, unit-test coverage ≥ 70% on non-LLM modules. | MVP |
| NFR-020 | Maintainability | Prompts are stored as versioned files, never as inline strings in code. | MVP |
| NFR-021 | Quality target | Detection recall ≥ 0.80 on injected faults with relative magnitude ≥ 20%, with ≤ 2 false alarms per 30 simulated days. | MVP (initial) |
| NFR-022 | Quality target | Root-cause hit@3 ≥ 0.70 on the MVP scenario set. | MVP (initial) |
| NFR-023 | Quality target | Numeric-claim support rate = 100% for reports marked "verified" (enforced by the verifier). | MVP |
| NFR-024 | Quality target | Zero successful side-effect attacks on the security suite before any public deployment. | EXT |
| NFR-025 | Honesty | The README results section shows only numbers produced by the evaluation harness. | MVP |

---

## 3. Traceability

| Objective (see `PROJECT_VISION.md`) | Requirements |
|---|---|
| OBJ-1 Detect anomalies | FR-030–035, FR-080, NFR-021 |
| OBJ-2 Root cause | FR-050–055, FR-082, NFR-022 |
| OBJ-3 No unsupported numbers | FR-044, FR-060–062, NFR-023 |
| OBJ-4 Human control | FR-070–072, NFR-009, NFR-012, NFR-015 |
| OBJ-5 Zero cost / offline | NFR-001–003 |
| OBJ-6 Fine-tuning evidence | FR-090–094 |
| OBJ-7 Reproducibility | FR-001, FR-006, FR-086, NFR-016 |
