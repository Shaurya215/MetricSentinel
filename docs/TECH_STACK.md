# Technology Stack — MetricSentinel

> Status: Phase 0 planning document. These are **recommendations**; nothing is installed or implemented yet.
> Classification: `[MVP]` required for v0.1 · `[EXT]` added in Phases 7–9 · `[OPT]` optional, only if a measured need appears.
> Rule: every technology must have a job that simpler tools cannot do. If a choice cannot justify itself, it is removed.

## 1. Summary

| Layer | Choice | Class |
|---|---|---|
| Language and tooling | Python 3.12, uv, ruff, mypy (lenient), pytest, pre-commit | MVP |
| Analytics database | DuckDB | MVP |
| Transformations | SQL model files run by a small Python runner | MVP |
| Data validation | Pandera | MVP |
| Application database | PostgreSQL 16 + pgvector | MVP |
| Keyword search | PostgreSQL full-text search | MVP |
| Embeddings | sentence-transformers with a small open embedding model | MVP |
| Reranker | Open cross-encoder (bge-reranker family) | EXT |
| Forecasting | statsforecast | MVP |
| Statistics | SciPy, statsmodels, ruptures | MVP |
| Foundation-model forecasting | TimesFM | OPT |
| Agent framework | Google ADK | MVP |
| Hosted LLM | Gemini Flash-tier model (free tier) | MVP |
| Local LLM runtime | Ollama | MVP |
| SQL safety | sqlglot | MVP |
| Structured outputs | Pydantic v2 | MVP |
| Prompt optimisation | DSPy | OPT |
| Backend | FastAPI + Uvicorn | MVP |
| DB access / migrations | SQLAlchemy 2 + Alembic | MVP |
| Auth | JWT (PyJWT) + passlib | MVP |
| Scheduling | APScheduler | MVP |
| UI | Streamlit | MVP |
| Charts | Plotly (UI) and Matplotlib (static report images) | MVP |
| Experiment tracking / registry | MLflow (local file or SQLite backend) | MVP |
| Logging | structlog (JSON) | MVP |
| Tracing | OpenTelemetry + Arize Phoenix (self-hosted) | EXT |
| Metrics dashboards | Prometheus + Grafana | OPT |
| Fine-tuning | Unsloth + TRL + PEFT | EXT (gated) |
| Model serving (fine-tuned) | Ollama (GGUF); vLLM optional | EXT |
| Containers | Docker + Docker Compose | MVP |
| CI | GitHub Actions | EXT |
| Demo hosting | Free-tier VM or Hugging Face Space | EXT |

---

## 2. Decisions with reasons and alternatives

### Data layer

| Technology | Why it is needed | Alternatives considered | Why not the alternative |
|---|---|---|---|
| **DuckDB** | Fast columnar SQL over about 100k orders on a laptop, no server needed. It can be opened **read-only**, which physically enforces NFR-009. | PostgreSQL for analytics; SQLite; BigQuery sandbox | Postgres mixes app writes with analytics reads. SQLite is slow on aggregations. BigQuery adds cloud setup and quota limits. |
| **SQL files + Python runner** | Five or six model files do not need a framework. This keeps the SQL visible and easy to explain. | dbt-duckdb | dbt is worth it at 30+ models or with a team. It is kept as a documented upgrade path, not an MVP dependency. |
| **Pandera** | Declarative dataframe schemas with checks on types, ranges and uniqueness. This is how FR-003 is met. | Great Expectations; hand-written asserts | Great Expectations is heavy for 9 tables. Hand-written asserts do not produce readable reports. |
| **PostgreSQL + pgvector** | One database for app state, audit log, query ledger, vectors and full-text search. This means fewer services to run. | Qdrant, Chroma, FAISS + SQLite | A separate vector DB adds a service with no benefit at this corpus size (a few thousand chunks). |
| **Postgres full-text search** | Keyword half of hybrid retrieval. It catches exact column and table names that embeddings often miss. | Elasticsearch/OpenSearch; rank-bm25 in memory | Elasticsearch is heavy. In-memory BM25 does not persist. |
| No DVC in MVP | The data is public and re-downloadable. A script plus checksums plus a data hash gives reproducibility. | DVC | DVC becomes useful when generated training sets appear (Phase 8), and will be reconsidered then. |

### Data Science and ML

| Technology | Why it is needed | Alternatives | Why not |
|---|---|---|---|
| **statsforecast** | Fast ETS/ARIMA/Theta models with prediction intervals, suitable for hundreds of daily series. Intervals are the basis of detection. | Prophet; pmdarima; hand-rolled models | Prophet is slower and its intervals are often poorly calibrated. pmdarima is slow at scale. |
| **SciPy / statsmodels** | Two-proportion tests, Mann–Whitney, Benjamini–Hochberg (`multipletests`), decomposition. | — | Standard and explainable. |
| **ruptures** | Change-point detection, used as an investigation tool. | Custom CUSUM | ruptures is tested and small. |
| **TimesFM** `[OPT]` | Compare a foundation forecasting model against classical baselines in the backtest. This is a good DS discussion point. | Chronos | Added only if time allows. It is not needed for detection to work. |
| **MLflow** | Tracks forecasting backtests, eval runs and (later) fine-tuning runs and model versions in one place. | Weights & Biases; plain CSV | W&B is hosted (free tier, but external). CSV files do not scale to comparing runs. |

### GenAI, retrieval and agents

| Technology | Why it is needed | Alternatives | Why not |
|---|---|---|---|
| **Google ADK** | Code-first agent framework with tool definitions, session state, built-in agent evaluation and tool-confirmation (human-in-the-loop) features. It is model-agnostic (non-Gemini models via its LiteLLM integration). It also adds a framework different from ones already used in prior projects. | LangGraph; OpenAI Agents SDK; plain Python loop | **LangGraph is the documented alternative** if ADK blocks progress: it has an explicit graph, a checkpointer and interrupts. A plain loop is possible, but gives up built-in eval and tracing hooks. *Open decision for approval.* |
| **Gemini Flash-tier (free tier)** | Capable planner and report writer at zero cost. Structured output and function calling are supported. | Groq free tier (open models); frontier paid APIs | Groq is a good fallback. Paid APIs break NFR-001. The exact model ID is chosen in Phase 4 by measurement. |
| **Ollama** | Runs local models for offline mode (NFR-003) and for serving the fine-tuned model later. | llama.cpp directly; vLLM | llama.cpp is lower level. vLLM needs a GPU and is kept optional for a GPU demo box. |
| **Small open model (Gemma 4 E4B class)** | Local text-to-SQL and fallback planner. Also the fine-tuning target if the gate says go. Apache 2.0 licence. | Qwen3 4B–8B; Llama small models | Qwen3 is the fallback if tooling for Gemma 4 lags. The final choice is made on the Phase 4 SQL eval. |
| **sentence-transformers + small embedding model** | Local, free embeddings for schema docs and incidents. It must handle Portuguese category names and review text. | Hosted embedding APIs | Hosted embeddings send data out and add cost. A multilingual small model is chosen in Phase 4 on the retrieval eval. |
| **sqlglot** | Parses SQL into an AST, so the guard can check "single SELECT, allowed tables, no forbidden functions". Parsing is reliable; checking strings with regex is not. | Regex checks; database permissions only | Regex is bypassable. Database permissions are used *as well* (read-only connection); defence in depth. |
| **Pydantic v2** | Validates every tool input and agent output against a schema. | dataclasses | No validation. |
| **DSPy** `[OPT]` | Optimise the planner prompt against the RCA eval set instead of hand-tuning. | Manual prompt iteration | Adopted only if manual iteration plateaus. |

### Application and operations

| Technology | Why it is needed | Alternatives | Why not |
|---|---|---|---|
| **FastAPI** | Async REST API, automatic OpenAPI docs, Pydantic integration. It is the most common Python stack for AI backends. | Flask; Django | Flask lacks built-in async and typing. Django is too heavy for this. |
| **SQLAlchemy + Alembic** | Typed database access and versioned schema migrations for the app database. | Raw psycopg | Migrations would become ad hoc. |
| **JWT + roles** | Four roles with approval restricted to `approver` (NFR-012). | OAuth provider | Unnecessary for a portfolio deployment. |
| **APScheduler** | Runs the daily cycle inside the backend process. | Celery + Redis; cron container | Celery adds two services for one job per day. |
| **Streamlit** | A review UI built quickly in Python. The focus of the project is the AI system, not frontend work. | Next.js; Gradio | Next.js is a larger effort (possible v1.x upgrade). Gradio is less suited to multi-page review flows. |
| **structlog** | JSON logs with a run ID on every line (NFR-017). | stdlib logging | Harder to produce structured output. |
| **OpenTelemetry + Arize Phoenix** `[EXT]` | Traces of LLM and tool calls, with token and latency data. Phoenix runs as a single container and is OpenTelemetry-native. | Langfuse (self-hosted) | Current Langfuse self-hosting needs several services (Postgres, ClickHouse, Redis, object storage). That is heavy for a laptop. Langfuse remains a valid alternative. |
| **Prometheus + Grafana** `[OPT]` | Operational dashboards. | Phoenix + logs only | Added only if Phoenix + logs are not enough for the demo. |
| **Docker Compose** | One-command start (NFR-002). | Kubernetes | Out of scope for a single-node system. |
| **GitHub Actions** `[EXT]` | Lint, tests and eval gates on each pull request. | GitLab CI | The repository lives on GitHub. |

### Fine-tuning (gated, Phase 8)

| Technology | Why it is needed | Alternatives | Why not |
|---|---|---|---|
| **Unsloth** | QLoRA training of 2–4B models within free Kaggle/Colab T4 memory. | Axolotl | Axolotl is config-driven and good at scale, but heavier for single-GPU free tiers. |
| **TRL** | `SFTTrainer` and `GRPOTrainer` with custom reward functions (execution match). | Tunix (JAX) | Tunix is a good fit for TPUs. TRL fits the PyTorch/GPU free tier and is more widely used in job descriptions. |
| **PEFT** | LoRA adapters: small artefacts that can be swapped. | Full fine-tuning | Not feasible on free GPUs. |

---

## 3. Deliberately excluded

| Technology | Why excluded |
|---|---|
| LangChain (as a core dependency) | Adds abstraction layers without a clear need, given ADK and direct SDKs |
| CrewAI / AutoGen group chat | A multi-agent chat pattern is not needed (see `AGENT_WORKFLOW.md`) |
| Kafka / Spark | No streaming and no big-data volume |
| Redis | No cache or queue need that Postgres and in-process caching cannot meet |
| Separate vector database | pgvector is enough at this scale |
| Paid LLM APIs as defaults | Breaks the zero-cost rule; a paid model may appear only as an optional *reference baseline* in evaluation |
| Kubernetes, Terraform | Operational overhead with no portfolio benefit for a single-node system |

## 4. Cost classification

| Tier | Items |
|---|---|
| **FREE** | Everything in section 1, Olist data, Kaggle/Colab free GPUs, Gemini API free tier, GitHub Actions free minutes, Hugging Face Spaces (CPU) |
| **LOW-COST** | A small always-on VM for the demo; Colab Pro for faster GRPO runs; small spend on a paid API for a reference baseline only |
| **OPTIONAL PAID** | Rented 24 GB GPU hours for larger local models or longer training |
| **EXPENSIVE (avoid)** | Managed agent runtimes, managed vector DBs, always-on GPU serving, training models above ~10B |

## 5. Version pinning policy

Exact versions are pinned in `pyproject.toml` / `uv.lock` in Phase 1. Fast-moving libraries (ADK, TRL, Unsloth, Ollama models) are re-checked at the start of the phase that first uses them, and any change is recorded in the `ROADMAP.md` change log.
