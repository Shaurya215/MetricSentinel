# Project Vision — MetricSentinel

> Status: Phase 0 planning document. Nothing described here is implemented.

## 1. Problem statement

Organisations track daily KPIs, but they handle KPI changes badly in two ways.

**Detection problem.** Most KPI alerts use fixed thresholds such as "alert if orders drop 20%". These ignore weekly seasonality, holidays, trend and segment size. Small segments trigger false alarms, and real problems hidden inside one segment go unnoticed. When many series are checked each day, some will look unusual purely by chance. Without statistical control, the alert channel fills with noise.

**Diagnosis problem.** After an alert, an analyst must answer "why?". They slice the KPI by region, category, seller and payment method, compare against the expected level, and check whether the change is a data bug or a real business event. This is repetitive SQL work, often taking hours. The final explanation is usually a message with numbers nobody can trace back.

**Why a plain LLM chatbot is not the answer.** A chatbot waits for a question, does not know what is "normal", can state numbers it never computed, and should not publish anything on its own.

## 2. Real-world use case

The system is modelled on a marketplace operations team, using the Olist Brazilian e-commerce dataset (orders, items, payments, reviews, sellers, customers, products).

A typical planned scenario:

1. The scheduler advances the simulated clock one day and computes KPIs.
2. The detector finds that the **late-delivery rate** is well above its forecast interval, and that the result survives false-discovery-rate control.
3. The investigation agent reads the metric definition and similar past incidents. It then tests hypotheses such as region, seller, category or data error.
4. Using SQL and a contribution analysis, it finds that most of the increase comes from **a small group of sellers in one state**. It confirms this with a segment test.
5. The verifier checks that every number in the draft report exists in the query ledger.
6. An approver reads a one-page report with evidence links and a chart, then approves, edits or rejects it.
7. The approved report is stored as incident memory, so future investigations can retrieve it.

Because the anomaly in the evaluation setting was injected on purpose, the system's diagnosis can be scored against the known cause.

## 3. Objectives

| ID | Objective | How it will be measured |
|---|---|---|
| OBJ-1 | Detect real KPI anomalies with controlled false alarms | Precision, recall, detection delay and false alarms per 30 simulated days, measured on injected faults |
| OBJ-2 | Identify the root cause automatically | Root-cause hit@1 and hit@3 on a labelled scenario benchmark |
| OBJ-3 | Never report an unsupported number | Numeric-claim support rate from the verifier, audited on a sample by a human |
| OBJ-4 | Keep a human in control of every output | No path to publishing that bypasses approval, enforced by design and tested |
| OBJ-5 | Run at zero cost and optionally fully offline | Free tools only; a documented local-only mode |
| OBJ-6 | Show when fine-tuning helps and when it does not | Four-way comparison: base, RAG, fine-tuned, RAG + fine-tuned, with a paired significance test |
| OBJ-7 | Be reproducible | One command rebuilds data, runs evaluation and reports the versions used |

Numeric targets for these objectives are set in `REQUIREMENTS.md` as *initial targets*. They will be revised once baselines are measured.

## 4. Value

**For users (simulated):** faster time from change to explanation, fewer false alarms, and reports whose numbers can be checked.

**As a portfolio project**, it shows these skills working together, each with a real job:

| Skill area | Where it appears |
|---|---|
| Data engineering | Ingestion, validation, modelled tables, metric store |
| Data Science and statistics | EDA, forecasting backtests, interval-based anomaly tests, FDR control, contribution analysis, paired tests |
| ML engineering | Model selection, experiment tracking, evaluation harness, (extended) fine-tuning and serving |
| GenAI / LLM engineering | Text-to-SQL, hybrid RAG, structured outputs, grounded report writing |
| Agentic AI | Tool-using investigation loop, state, stopping rules, human approval |
| Production engineering | FastAPI, auth, audit log, containers, tracing, CI gates |
| AI security | Read-only data access, SQL guard, indirect prompt-injection defence, PII handling |

**What makes it different from common projects:** it is proactive rather than a chatbot, and its diagnosis quality is scored against injected ground truth rather than judged by eye. Fine-tuning is gated by measurement instead of being assumed.

## 5. Non-goals

- A general BI tool or dashboard builder.
- A chat interface over arbitrary databases.
- Autonomous actions on business systems (refunds, emails to customers, config changes).
- Real-time streaming analytics.

## 6. Long-term vision (beyond v1.0, all `[FUTURE]`)

- **Pluggable data sources:** the same pipeline running on a second public dataset, to show it generalises beyond Olist.
- **Learning from reviewers:** an alert-priority model trained on approve/reject history.
- **Richer causal analysis:** handling known events such as holidays and campaigns as covariates.
- **Interoperability:** exposing the investigator as an MCP server or A2A agent, so other assistants can request investigations.
- **Write-up:** a technical blog post on "SFT vs GRPO for schema-specific text-to-SQL", using only measured results.

## 7. Success definition for v1.0

MetricSentinel v1.0 is successful if:

1. The full loop (detect → investigate → verify → approve) runs end to end from one command on a clean machine.
2. All evaluation suites run in CI, and the README results table is generated from their output.
3. The fine-tuning go/no-go decision is documented with evidence, whichever way it goes.
4. A reviewer can trace any number in any report back to the SQL query that produced it.
