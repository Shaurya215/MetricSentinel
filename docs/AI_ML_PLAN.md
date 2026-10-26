# AI / ML Plan — MetricSentinel

> Status: Phase 0 planning document. No model has been trained, evaluated or deployed.
> All numbers below are **planned settings or initial targets**, not results.

## 1. Where each technique is used, and why

| Problem | Technique | Why this and not something else |
|---|---|---|
| "What value is normal today?" | Statistical forecasting with prediction intervals | Seasonality and trend make fixed thresholds useless. Intervals give a calibrated notion of "unusual". |
| "Is this deviation real?" | Interval test + Benjamini–Hochberg FDR across all series | Hundreds of series are tested each day. Without multiple-testing control, false alarms are guaranteed. |
| "Which segment explains the change?" | Contribution (mix vs rate) decomposition, segment tests, change-points | Deterministic, explainable numbers the agent can cite |
| "Which hypothesis should be tested next?" | LLM agent | The investigation path depends on earlier results and cannot be scripted in advance |
| "Turn a data question into SQL" | LLM text-to-SQL with retrieved schema | A language-to-formal-language task |
| "Which definitions and past incidents are relevant?" | Hybrid retrieval | Exact names (keyword) plus meaning (vector) |
| "Is every number in the report true?" | Deterministic verifier | Exact matching is stronger than an LLM judge for numbers |
| "Can a small local model do the SQL?" | Fine-tuning (gated) | Only if the measurements in §6 show a real gap |

Supervised ML classifiers are **not** used for anomaly detection. There are no real labelled anomalies. Training on injected faults would teach the model the injector's patterns, not real anomalies. Injected faults are used **only for evaluation**.

## 2. Forecasting and anomaly detection `[MVP]`

### 2.1 Models

| Model | Role |
|---|---|
| Seasonal naive (weekly) | Baseline; must be beaten |
| AutoETS (statsforecast) | Main candidate for smooth series |
| AutoARIMA / AutoTheta (statsforecast) | Candidates, chosen per series family by backtest |
| TimesFM zero-shot `[OPT]` | Foundation-model comparison, in the backtest only |

Holiday indicators come from `dim_date`. Series with low volume are aggregated or excluded (rule set in Phase 3 from EDA).

### 2.2 Backtest

- **Rolling-origin backtest** over the monitoring window, with a horizon of 1 day and weekly re-fits (exact cadence tuned in Phase 3).
- **Metrics:** MASE and sMAPE for point accuracy; **interval coverage** (target: close to nominal, for example 95%) and interval width for calibration.
- **Selection:** the model family with the best coverage-adjusted accuracy per KPI family. It is chosen on backtest data only, before any evaluation on fault scenarios.
- Every backtest is logged to MLflow with its `data_hash`.

### 2.3 Detection rule

1. For each series `s` on day `t`, compute the forecast `ŷ` and interval `[L, U]` from data before `t`.
2. Compute a standardised residual `z = (y − ŷ) / σ̂`, where `σ̂` is derived from the interval width. Convert it to a one-sided p-value in the KPI's "bad" direction.
3. Apply Benjamini–Hochberg at `q = 0.05` (initial setting) across all series tested that day.
4. Keep flags with `q ≤ threshold`, observed value outside `[L, U]`, and enough sample size.
5. Group flags that share a KPI and day (total plus segments) into one anomaly. The segment list is stored as context.
6. Severity = |relative deviation| × log(sample size), used to decide which anomalies are investigated first.

### 2.4 Detection evaluation

On the fault benchmark plus clean days: precision, recall (overall and by magnitude band), detection delay in days, and false alarms per 30 simulated days. Initial target: NFR-021.

### 2.5 Phase 3 results (2026-10-23) and gap analysis

All numbers come from the harness (`make backtest`, `make eval-detection`) and are logged to MLflow.
Run: git `11147b0`, data_hash `8333919867b2`, detector `16cecd334dd9`, scenarios `3fedd09cae9d`.

**Backtest (2017 only; model choice and calibration never saw 2018).**

| Series group | Chosen model | Calibration | MASE | Calibrated coverage (2nd half) |
|---|---|---|---|---|
| volume daily | AutoETS | x1.02 | 1.05 | 92.7% |
| volume weekly | AutoTheta | x1.66 | 1.35 | 91.3% |
| ratio daily | AutoTheta | x0.96 | 0.59 | 93.1% |
| ratio weekly | AutoETS | x1.21 | 1.58 | 97.3% |
| rate daily | AutoTheta | x1.40 | 1.82 | 89.4% (below the 90% floor) |
| rate weekly | AutoTheta | x1.88 | 3.17 | 91.6% |
| mean daily | SeasonalNaive | x1.07 | 1.18 | 90.9% |
| mean weekly | AutoETS | x1.38 | 2.22 | 90.8% |

AutoARIMA (run on the total series only, about 1.3 s per fit) did not beat AutoETS or AutoTheta on any total series.
Full tables: `data/reports/backtest.md`.

**Detection on the 30-scenario benchmark plus 243 clean days of 2018.**

| Metric | Result | NFR-021 target |
|---|---|---|
| Recall, all 30 scenarios | 36.7% | - |
| Recall, faults with measured magnitude >= 20% (16) | **56.2%** | >= 80%: **not met** |
| False alarms on clean days | **9.26 per 30 days** (75 in 243 days) | <= 2: **not met** |
| Precision of fault-caused anomalies | 79.4% | - |
| Detection delay | median 5.0 days, mean 5.3 days | - |
| One day's detection, all 214 series (NFR-004) | 6.4 s | < 60 s: met |

Recall by fault type: pipeline_bug 80%, category_price_change 40%, payment_failure 40%, and 20% each for
segment_level_shift, seller_delay and review_collapse. By target size: large 55%, medium 12%, small 0%.

**History of this number.** The first evaluation run found a bug: count/sum KPIs were skipped when the
day's count was below the minimum sample, so a day with ZERO orders was never tested (scenario S28,
all orders dropped for a day, went undetected). After the fix (regression tests added) recall at >= 20%
went from 43.8% to 56.2%, and clean-day alarms rose from 4.57 to 9.26 per 30 days, because zero-count
days are now tested (see the cut-off finding below). Both runs are in MLflow.

**Gap analysis: why false alarms are high (75 clean-day alarms).**

| Cause | Alarms | Evidence |
|---|---|---|
| The dataset stops: orders fall to 14, 4 and 1 on 29-31 August 2018 | 13 | Daily counts; the Phase 2 window rule used a trailing 7-day mean, which lags, so it let these days in |
| Post-holiday surge, 1-14 January 2018 | 17 | Orders 2x the forecast after a December holiday lull that was not marked as an event |
| Truckers' strike window, 21 May - 4 June 2018 (delivery and review KPIs) | 5 | A documented national strike; these are probably real incidents, not false alarms |
| Other upward volume spikes, mostly daily GMV | 24 | GMV is heavy-tailed (a few expensive orders); the normal-error interval is too thin on the upper side |
| Other | 16 | Mixed: review-score dips, freight-ratio jumps, weekly segment swings |

Even excluding the dataset cut-off, the rate is 7.9 per 30 days, so the target is genuinely missed.

**Gap analysis: why recall is low.**

* Small segments are pooled into `__other__`, so a 50% drop in a small state (S03, PA) is diluted to a few
  percent of the pool. This is the cost of the Phase 2 sample-size finding.
* Weekly series react only at the week end and have wide intervals (volume weekly calibration x1.66), so
  medium segments are caught only when the fault is large and covers whole weeks.
* Seller delay targets the seller's state, which is not a monitored dimension; only the diluted effect on
  the total is visible (measured change 0-9%).
* Daily order volume is volatile (the calibrated interval is roughly +/-40% of the forecast), so duplicating
  30% of one day's orders (S29) stays inside it. Larger one-day bugs (S26, S27, S28, S30) are caught on the day.

**What would close the gap (proposed, not done).** To avoid tuning on the scored data, any of these must be
validated on a NEW benchmark (new seed and dates) before its numbers are reported.

1. Composition tests for segments: test each segment's *share* of the total (binomial), not its level. Shares
   are far less noisy than levels and expose payment, state and category faults inside the total.
2. Heavy-tailed volume: model GMV on a log scale, or keep GMV's alert direction to `down` and leave spikes to `orders`.
3. Calendar events known in advance: Christmas-New Year holiday season, and a data-completeness guard that stops
   testing when the feed collapses (and raises a single data-quality alert instead).
4. Add `seller_state` as a dimension for the delivery KPIs.
5. Correct the Phase 2 window rule to use a centred rolling mean (the trailing mean lags at the end of the data).

## 3. Root-cause statistics (agent tools) `[MVP]`

| Tool | Method | Output |
|---|---|---|
| `contribution_analysis` | Decomposes the KPI change between the baseline window and the anomaly day into **mix effect** (segment share changed) and **rate effect** (segment KPI value changed), for each dimension | Ranked segments with contribution share |
| `segment_test` | Two-proportion z-test (rates) or Mann–Whitney U (means, skewed data) comparing segment vs rest, anomaly window vs baseline | Effect size, p-value, sample sizes |
| `changepoint` | PELT (ruptures) on a series | Change-point dates |
| `data_quality_check` | Row-count, duplicate and null checks on the relevant tables for the anomaly date | Pass/fail with details |

Every tool result is written to the query ledger with an ID. The agent can cite only ledger IDs.

## 4. GenAI / LLM `[MVP]`

### 4.1 Model roles

| Role | MVP model | Fallback | Output contract |
|---|---|---|---|
| Planner (investigation agent) | Hosted Gemini Flash-tier model (free tier) | Groq free tier, or local model via Ollama | ADK tool calls; final `Findings` (Pydantic) |
| Text-to-SQL | Same hosted model + retrieved context | Local small model (Gemma 4 E4B class) | One SQL string, then guard + execute |
| Report prose | Hosted model | Local model | Markdown sections that reference ledger IDs |
| Embeddings | Local multilingual small model | — | 384–1024-dimension vectors (chosen in Phase 4) |

Exact model IDs are chosen in Phase 4 on the SQL and retrieval evals, and recorded in `config/models.yaml` (planned).

### 4.2 Prompting rules

- Prompts are files under `prompts/` with an ID and version. The run record stores the prompt versions used.
- Structured output (JSON schema / Pydantic) is used for every agent decision.
- Tool results are placed in delimited "untrusted data" blocks.
- The report writer receives **only verified findings and ledger values**. It does not receive raw query results.
- Temperature 0 for SQL and verification-related steps; low temperature for prose.

### 4.3 Text-to-SQL pipeline

```
question
 → retrieve: relevant tables and columns, metric definitions, 3 similar solved examples
 → generate SQL (DuckDB dialect)
 → sqlglot guard (single SELECT, allowed tables and functions, sim_date views)
 → execute read-only (row limit, 10 s timeout)
 → on error: repair with the error message (max 2 attempts)
 → ledger entry
```

## 5. Retrieval (RAG) `[MVP]`

### 5.1 Corpora

| Corpus | Chunking | Why it is needed |
|---|---|---|
| Schema docs (one card per table with column descriptions, join keys, example values) | One chunk per table, plus one chunk per column group for wide tables | Lets text-to-SQL see only the relevant schema |
| Metric catalogue | One chunk per KPI | The agent uses the official definition, not a guess |
| SQL examples (question → SQL) | One chunk per example | Few-shot examples retrieved by similarity |
| Incident memory (approved reports) | One chunk for the summary, plus one per root-cause section | Reuse of past diagnoses |

Chunks carry metadata (source, table, KPI, date, version), used for filtering.

### 5.2 Retrieval pipeline

1. **Keyword:** Postgres full-text search (`tsvector`) → top 20.
2. **Vector:** pgvector cosine similarity → top 20.
3. **Fusion:** reciprocal rank fusion → top k (k = 5 initially).
4. **Rerank** `[EXT]`: cross-encoder over the fused top 20. Kept only if recall@5 improves on the retrieval eval.
5. **Query rewriting:** not planned. The agent already writes precise queries. It is added only if the retrieval eval shows failures on the agent's own queries.

Graph RAG is **not** used. The data is tabular, and relationships are expressed by SQL joins.

### 5.3 Retrieval evaluation

For each SQL test question there are gold tables and a gold metric definition. Metrics: recall@k (k = 3, 5, 10) and MRR. Compared across: keyword only, vector only, hybrid, and hybrid + rerank.

## 6. Fine-tuning: is it genuinely necessary?

### 6.1 Honest assessment

- **For MVP accuracy: probably not.** A capable hosted model with good schema retrieval and few-shot examples may be accurate enough on one well-documented schema.
- **For the local-only goal (NFR-003) and for cost and privacy: possibly yes.** Small local models (2–8B) are often unreliable at multi-table SQL through prompting alone. RAG supplies context, but it does not teach the *skill* of writing correct joins, date logic and dialect details.
- **Why this is a good fine-tuning target:** SQL execution gives an automatic, exact reward. This makes RL with verifiable rewards (GRPO) practical on free hardware, without human preference labels.

So fine-tuning is **planned but gated**. The decision is made on measurements, not assumptions.

### 6.2 Go / no-go gate (end of Phase 7, decided in Phase 8)

Measure on the human-written SQL test set:

- **A** = best hosted model + RAG (execution accuracy)
- **B** = best *local base* model + RAG (execution accuracy)

| Condition | Decision |
|---|---|
| A − B ≥ 10 percentage points | **GO**: fine-tune the local model to close the gap |
| 5 ≤ A − B < 10 points | **GO (light)**: SFT only; GRPO only if SFT plateaus |
| A − B < 5 points | **NO-GO**: document that RAG is enough for local mode; spend Phase 8 on reranking and evaluation depth instead |
| Local latency p50 > 3× hosted, with equal accuracy | Consider fine-tuning a *smaller* model (E2B class) for speed |

The decision, the numbers and the reasoning are written to `docs/decisions/ADR-finetuning.md` (planned).

### 6.3 Fine-tuning pipeline (if GO) `[EXT]`

| Step | Plan |
|---|---|
| Base model | Gemma 4 E4B class (Apache 2.0); Qwen3 4B as fallback if tooling for Gemma 4 lags. Re-verify Unsloth and TRL support at the start of Phase 8. |
| Data | 3k–5k synthetic pairs from templates (joins, windows, date buckets, top-k by segment, cohorts), generated by a teacher model. Kept only if the SQL executes, returns non-empty deterministic results, and is not a near-duplicate. |
| Splits | By template family, 70/15/15. The test set is the human-written set only. |
| SFT | QLoRA (4-bit base), LoRA rank 16 as a starting point; Unsloth + TRL `SFTTrainer`; Kaggle or Colab T4 |
| RL | TRL `GRPOTrainer`. Reward: +1.0 if the result set matches gold (order-insensitive unless ordering is asked for), +0.1 for valid syntax, −0.5 for a forbidden statement or table. Group size 4–8. |
| Reward-hacking guards | Rewards are computed only by execution against gold; queries that return the whole table are penalised; outputs are spot-checked by a human |
| Selection | Best validation execution accuracy; the test set is used once |
| Export | Merge adapter → GGUF → Ollama; registered in MLflow with its eval report |
| Integration | Switch `sql` role in `config/models.yaml`; no code change |

### 6.4 Required comparison (if GO)

| Configuration | Metrics |
|---|---|
| Local base, zero-shot | Execution accuracy, valid-SQL %, p50/p95 latency, cost per 1k queries |
| Local base + RAG | same |
| Fine-tuned, no RAG | same |
| Fine-tuned + RAG | same |
| Hosted + RAG (reference) | same |

Differences are tested with **McNemar's test** on per-question correctness (paired). Only measured values will be reported.

## 7. Evaluation framework `[MVP]` → `[EXT]`

| Suite | Data | Metrics | Scope |
|---|---|---|---|
| Forecast backtest | Monitoring window | MASE, sMAPE, interval coverage and width | MVP |
| Detection | Fault benchmark + clean days | Precision, recall by magnitude, delay, false alarms per 30 days | MVP |
| Text-to-SQL | 100 → 150 human-written questions | Execution accuracy, valid-SQL rate, repair rate, latency, tokens | MVP |
| Retrieval | Gold tables and definitions | Recall@k, MRR | MVP |
| Root cause | 30 → 100 scenarios | hit@1, hit@3, correct "data bug" identification, steps, tokens, cost, wall time | MVP |
| Report faithfulness | All generated reports | Support rate, direction errors; LLM-judge groundedness validated against ~50 human labels (agreement reported) | MVP / EXT |
| Tool use | Traces | Guard rejections, redundant calls, invalid arguments | MVP |
| Security | Adversarial suite | Attack success rate, false-block rate | EXT |
| Local vs hosted | All suites in offline mode | Same metrics | MVP |

**Rules**

- Each eval run logs its git SHA, `data_hash`, prompt versions, model IDs and seed to MLflow (FR-086).
- LLM-dependent suites run 3 times; the mean and range are reported.
- A change counts as an improvement only if the target metric improves under a paired test **and** no gated metric regresses (faithfulness, security, cost).
- CI gates `[EXT]`: a fast suite on every pull request (about 30 SQL questions, 5 scenarios, the security suite); the full suite nightly or on demand.

## 8. Experiment tracking and versioning

| Artefact | Tool |
|---|---|
| Forecast backtests, eval runs, training runs | MLflow experiments |
| Models (forecasters as config; LLM adapters) | MLflow model registry, with eval reports attached |
| Prompts | Versioned files in git; version IDs stored in each run |
| Data | `data_hash` (see `DATA_PLAN.md`) |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Free-tier rate limits slow down evaluation | Cache LLM responses by (prompt hash, model); run large evals at night; use a local model for development |
| Forecast intervals poorly calibrated | Measure coverage in the backtest; use conformal adjustment of interval width if needed |
| Injected faults are too easy | Vary magnitudes down to 10%; add distractor changes; include "no fault" days |
| LLM judge disagrees with humans | Report agreement; rely on deterministic checks for numbers |
| Gemma 4 tooling not ready for fine-tuning | Fall back to Qwen3 4B; record the change |
