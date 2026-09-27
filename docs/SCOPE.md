# Scope — MetricSentinel

> Status: Phase 0 planning document. Nothing listed here is implemented.

## 1. MVP definition (v0.1, end of Phase 6)

The MVP is the **smallest system that runs the complete loop end to end and measures it**:

> simulated day → KPIs → anomaly detection → one investigation agent → verifier → report → human approval → incident memory

The MVP uses a hosted free-tier LLM (or a local model) **without fine-tuning**. It also ships evaluation for detection, text-to-SQL, retrieval and root-cause accuracy.

### MVP feature list

| Area | Included in MVP |
|---|---|
| Data | Olist download with checksums; raw, staging and modelled layers in DuckDB; Pandera validation; data-quality report |
| Simulation | Simulated clock; fault injector with 6 fault types; 30-scenario benchmark |
| Metric store | 8 KPIs × 4 dimensions (see `DATA_PLAN.md`), with sample sizes |
| Detection | statsforecast models + seasonal-naive baseline; interval test; Benjamini–Hochberg FDR; grouping of related flags |
| Retrieval | Schema docs, metric catalogue and incident index in pgvector + Postgres full-text search; RRF fusion |
| SQL | Text-to-SQL with retrieved context; sqlglot guard; read-only execution; query ledger; 2 self-repair retries |
| Agent | One investigation agent (Google ADK) with 7 tools and a hard budget |
| Stats tools | Contribution decomposition, segment tests, change-point detection |
| Verifier | Deterministic numeric and direction checks against the ledger |
| Report | Markdown with two charts and an evidence table |
| Approval | Approve / edit / reject with reasons; audit log |
| Backend | FastAPI, JWT auth, 4 roles, REST endpoints |
| UI | Streamlit review app |
| Evaluation | Detection, SQL (100 questions), retrieval, RCA (30 scenarios); results logged to MLflow |
| Ops | Docker Compose; structured logs; README results generated from eval output |

## 2. In scope (whole project, v1.0)

Everything in the MVP, plus:

- Evaluation expanded to a 150-question SQL set and 100 RCA scenarios.
- CI regression gates on GitHub Actions.
- Security test suite and hardening.
- A human-calibrated LLM-judge for report faithfulness.
- Cross-encoder reranking, adopted only if it measurably improves recall@5.
- OpenTelemetry tracing into self-hosted Arize Phoenix.
- Fine-tuning a small local text-to-SQL model, **only if the Phase 8 go/no-go gate says go**.
- Ad-hoc question endpoint using the same tools and verifier.
- A public demo on a free host, a demo video, and final documentation.

## 3. Out of scope

| Item | Reason |
|---|---|
| Real-time streaming (Kafka, Flink) | Daily KPIs do not need it; a simulated clock gives the same learning at far lower complexity |
| Multi-agent crews or swarms | Not justified by the task; they make testing and cost control harder (see `AGENT_WORKFLOW.md`) |
| Any agent action with side effects (sending messages, writing data, changing configuration) | Safety; the approval service is the only publisher |
| Arbitrary user-uploaded databases | Changes the security model; possible future work |
| Paid managed services (Vertex Agent Engine, managed vector DBs, paid observability) | NFR-001 zero cost |
| Training models larger than about 10B parameters | Not feasible on free GPUs, and not needed |
| Kubernetes | Docker Compose is enough for a single-node portfolio system |
| Mobile app | Not relevant to the problem |
| Causal inference claims | The system reports *contribution* and *association* evidence, not proven causation |

## 4. Extended scope (v1.0, Phases 7–9)

| Item | Phase | Condition |
|---|---|---|
| CI regression gates | 7 | Always |
| Security suite and hardening | 7 | Always; must pass before public deploy |
| Phoenix tracing and cost dashboards | 7 | Always |
| Reranker | 7 | Kept only if recall@5 improves on the retrieval eval |
| Calibrated LLM judge | 7 | Always |
| Ad-hoc question endpoint | 7 | Should-have |
| SFT → GRPO fine-tuned SQL model | 8 | Only if the go/no-go gate says go |
| Prometheus + Grafana dashboards | 9 | Could-have; Phoenix + logs may be enough |
| Email or Slack publishing | 9 | Could-have; uses a webhook, approval still required |
| Public demo deployment | 9 | Only after the security gate passes |

## 5. Future scope (after v1.0, not scheduled)

- Alert-priority model trained on approve/reject history (needs enough reviewed incidents).
- A second public dataset, to prove the pipeline generalises.
- Holiday and campaign covariates in forecasting.
- Exposing the investigator as an MCP server or A2A agent.
- DPO on report writing using approved vs rejected reports.
- Multi-tenant support.

## 6. Scope-change rule

A feature moves into MVP only if it is needed for the end-to-end loop or for measuring it. Any change to scope must be recorded in `ROADMAP.md` (change log), with the reason.
