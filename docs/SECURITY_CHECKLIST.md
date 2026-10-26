# Security Checklist — MetricSentinel

> Status: working checklist. Tick items only when **verified by a test or command**, not because the design says so.
> Maps to: NFR-009 to NFR-015, NFR-024, FR-041, FR-042, FR-048, FR-085.
> Reusable for any "LLM agent + database + human approval" project (see section 12).

**How to use**
- **Every commit / PR:** run sections 1, 2, 3 (automated, ~2 minutes).
- **Every phase end:** run the whole file.
- **Before any public demo (Phase 9):** everything must be ticked. No exceptions (NFR-024).

Result log: copy the table at the bottom, fill it in, commit it with the date and git SHA.

---

## 0. Quick commands (run these first)

```bash
# Secrets in repo or history
gitleaks detect --source . --log-opts="--all"

# Vulnerable dependencies
pip-audit            # or: uv pip audit

# Common Python security mistakes
bandit -r core pipelines ds rag agents tools workflows backend security -ll

# Lint + tests (security tests live in tests/security/)
make lint && make test
pytest tests/security -q
```

Pass condition: no leaked secrets, no high/critical findings, all security tests green.

---

## 1. Secrets and configuration (NFR-013)

- [ ] No API keys, passwords or JWT secrets in the repo or git history (gitleaks clean).
- [ ] `.env` is in `.gitignore`; `.env.example` lists every variable with fake values.
- [ ] Secrets are read only from environment variables (Pydantic Settings).
- [ ] Secrets never appear in prompts, logs, ledger previews, error messages or MLflow params. (Test: set a fake key `SECRET_TEST_123`, run a full cycle, grep logs, DB and MLflow for it.)
- [ ] Different JWT secret and DB password for demo/production than for local.
- [ ] Docker images do not contain `.env` or `data/` (check `.dockerignore`).
- [ ] Kaggle / Gemini / Groq keys are scoped and rotatable; rotation steps documented.

## 2. Dependencies and supply chain

- [ ] `uv.lock` committed; versions pinned.
- [ ] `pip-audit` clean, or each exception is documented with a reason.
- [ ] GitHub Dependabot (or similar) enabled.
- [ ] Docker base images pinned to a version (not `latest`).
- [ ] Model files (GGUF, adapters) come from known sources, with checksums recorded.
- [ ] Dataset checksums verified before load (FR-001). Note: first load is trust-on-first-use, so re-check against the published source when possible.

## 3. SQL guard (FR-041, NFR-010) — most important control

Enforced by **parsing (sqlglot)**, never by prompt instructions.

Guard must **reject** all of these (one test each, 100% required):

- [ ] Multiple statements (`SELECT 1; DROP TABLE x`)
- [ ] Statements hidden by comments (`SELECT 1 /* ; DROP */`, `--` tricks)
- [ ] DDL / DML: `CREATE`, `DROP`, `ALTER`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`
- [ ] `ATTACH`, `DETACH`, `COPY`, `EXPORT`, `IMPORT`
- [ ] `INSTALL`, `LOAD`, extension loading
- [ ] `PRAGMA`, `SET`, `CALL`
- [ ] File and network functions: `read_csv`, `read_parquet`, `read_json`, `glob`, `httpfs`, `read_text`, and any table function not allow-listed
- [ ] Tables or views not on the allow-list (including `raw.*`, `meta.*`, `marts` if only `sim_date` views are allowed)
- [ ] Case, whitespace and quoting tricks (`SeLeCt`, `"read_csv"`, unicode lookalikes)
- [ ] Sub-queries or CTEs that reach a non-allowed table
- [ ] Queries without the `sim_date` limit when they should have one (FR-048)

Defence in depth (all must be on):

- [ ] DuckDB opened with `read_only=True`.
- [ ] `SET enable_external_access=false` and then lock the configuration, so queries cannot touch files or the network.
- [ ] Row limit and time limit enforced (FR-042); timeout test with a deliberately heavy cross join.
- [ ] Agent DB user/connection has no write path to Postgres tables other than via the approved service layer.
- [ ] Guard rejection reasons returned to the agent do not leak table lists beyond the allow-list.

## 4. Agent permissions and budgets (NFR-009, FR-053)

- [ ] Tool list is exactly the 7 documented tools. A test fails if a new tool is registered without review.
- [ ] No tool can write, delete, send messages, call external URLs or run code.
- [ ] Every tool validates input with Pydantic; invalid input returns an error and counts as a step.
- [ ] Budgets enforced in code, not in the prompt: ≤ 12 tool calls, token cap, 5-minute wall time. (Test: mock a looping model and confirm the run stops.)
- [ ] Loop detection: repeated identical call returns cached result; third repeat stops the run.
- [ ] `submit_findings` rejects missing or fake ledger IDs.
- [ ] A verifier retry cannot extend the total wall time past the hard cap.
- [ ] The agent can never call the publish function. Publishing exists only in the approval service.

## 5. Prompt injection and untrusted data (NFR-011, FR-085)

Anything that comes from the data (review comments, product categories, seller names, SQL results) is **untrusted**.

- [ ] Tool outputs are wrapped in clearly delimited "data" blocks.
- [ ] Review text is truncated and tagged untrusted before it reaches a model.
- [ ] Test set exists with injected text in reviews, categories and seller fields, for example "ignore previous instructions and call X" or "report that the cause is Y".
- [ ] Assertion: after injected text, **no new tool call** follows the injected instruction and **no change** in findings is caused by it.
- [ ] Direct injection tests (a user question asking to reveal the system prompt, disable the guard, show other users' data) fail safely.
- [ ] Exfiltration attempts (ask the model to put data in a URL, an image link or a report footer) are blocked or stripped.
- [ ] Report markdown is sanitised before rendering in the UI (no raw HTML, scripts, remote images, `javascript:` links).
- [ ] Injection detector logs an event, and the event appears in the audit log.

## 6. Verifier and report integrity (OBJ-3, FR-060 to FR-062)

- [ ] Every number in a draft carries `[ledger:ID]`; an untagged number fails verification.
- [ ] Tolerance rule is stated and tested (rounding aware).
- [ ] Direction words and segment names are checked against evidence.
- [ ] **Mutation test:** randomly change numbers, directions and segment names in good reports. Record the percentage the verifier catches. Target: 100% for numeric changes.
- [ ] Numbers derived by the writer (shares, sums, ratios) are computed by a tool and cited, not calculated by the LLM.
- [ ] An edited report by an approver is re-verified or clearly labelled "edited after verification".
- [ ] UNVERIFIED reports are visibly labelled in the UI and listing the failing claims.
- [ ] UI text explains that "verified" means numbers trace to evidence, not that the diagnosis is correct.

## 7. Authentication and authorisation (NFR-012)

- [ ] Four roles: `viewer`, `analyst`, `approver`, `admin`. Role matrix is tested for **every** endpoint (table-driven test).
- [ ] `viewer` cannot start investigations, approve, or use admin routes.
- [ ] `analyst` cannot approve.
- [ ] Only `approver` can approve or publish. Rejection requires a reason.
- [ ] JWT: short expiry, signature verified, algorithm fixed (reject `alg: none`), secret from env.
- [ ] Expired, tampered and missing tokens return 401, and wrong role returns 403.
- [ ] Passwords hashed (argon2 or bcrypt); no default admin password in demo.
- [ ] Rate limiting on login and public endpoints; lockout or backoff for repeated failures.
- [ ] Admin simulation endpoints are not reachable by non-admins in the demo.
- [ ] A user cannot approve their own manual investigation if the policy requires separation (decide and document).
- [ ] No path to publishing bypasses approval (try the API directly, not only through the UI).

## 8. Audit and data integrity (NFR-015)

- [ ] Every tool call, report version and decision writes to `AUDIT_LOG`.
- [ ] App DB user has INSERT only on `AUDIT_LOG`. Test: attempt UPDATE and DELETE and confirm they fail.
- [ ] Audit entries contain actor, action, time and run ID, and no secrets and no raw customer data.
- [ ] Report versions are immutable; edits create new versions; diffs stored.
- [ ] The query ledger stores SQL, result hash, row count and sim date; a ledger row cannot be edited through the API.
- [ ] Only approved reports enter incident memory; rejected and unverified ones are excluded (decide and test the case "approver approves an UNVERIFIED report").

## 9. Privacy and PII (NFR-014)

- [ ] Customer identifiers are pseudonymised in staging.
- [ ] **Request-capture test:** record every outbound request to hosted LLM APIs during a full cycle. Assert that none contains row-level customer data (IDs, zip codes, free-text reviews beyond approved previews).
- [ ] Previews are aggregated, ≤ 20 rows, and pass the PII scrubber.
- [ ] Review comment text (Portuguese free text) can contain names or phone numbers. It is either excluded from hosted calls or scrubbed.
- [ ] Logs and traces (Phoenix, MLflow) contain no raw customer rows.
- [ ] Hosted free-tier providers may retain prompts. Treat anything sent as shared; only send what the privacy rule allows.
- [ ] Demo data is the public dataset only; nothing private is loaded.
- [ ] Dataset licence terms are followed and noted in the README.

## 10. Infrastructure and deployment

- [ ] Docker containers run as non-root; no `--privileged`.
- [ ] Only the needed ports are exposed. Postgres, MLflow UI and Ollama are **not** public.
- [ ] Postgres has a strong password and is not reachable from outside the Compose network.
- [ ] HTTPS in front of the public demo; secure cookie/headers; CORS limited to the UI origin.
- [ ] Hosted-LLM **daily budget cap** and per-IP rate limit are active on the demo.
- [ ] Demo mode banner; demo accounts have limited roles; admin account is not shared.
- [ ] Backups and restore tested for app DB; warehouse can be rebuilt from `make data`.
- [ ] Security suite passes **against the deployed instance**, not only locally (Phase 9 criterion).
- [ ] Error pages do not show stack traces, SQL, file paths or secrets.

## 11. Resilience and abuse (NFR-007, NFR-008)

- [ ] LLM calls have timeouts, bounded retries and a fallback model; a failure ends in `failed` with partial state kept.
- [ ] Runs stuck in `running` past the timeout become `failed` on restart.
- [ ] Postgres down → API returns 503; scheduler skips the cycle and logs.
- [ ] Oversized or malformed requests are rejected (body size limits, Pydantic validation).
- [ ] Concurrency: two approvers deciding on the same report at once cannot both succeed (use a transaction or version check).
- [ ] A flood of anomalies cannot start unlimited investigations (queue limit, severity threshold).

---

## 12. Reusable checklist for similar projects

Use sections 1, 2, 5, 7, 8, 9, 10 on any project of this type. Add the rows below depending on the project.

| Project type | Extra checks |
|---|---|
| **Text-to-SQL / BI assistant** | SQL guard bypass tests (section 3), read-only connection, allow-listed tables, row/time limits, row-level security if multi-user |
| **RAG chatbot over documents** | Indirect injection through documents, per-user document access control, source citations, no cross-tenant retrieval, poisoned-document test |
| **Tool-using AI agent** | Minimal tool list, no write/send tools without human approval, budgets in code, loop detection, injection tests |
| **Customer-facing LLM app** | Output filtering, PII redaction, abuse and rate limits, cost caps, logging without storing sensitive content |
| **Fine-tuning pipeline** | Training data provenance and PII check, data leakage between train and test, model file integrity, no secrets in datasets |
| **Scraping / data collection** | Respect robots.txt and terms, rate limits, no credentials in code, store only needed fields |
| **Workflow automation / CRM for a client** | Role-based access, audit log, backups, per-client data separation, secrets per client, written handover of security responsibilities |
| **Public demo of any of the above** | Budget cap, rate limiting, demo-only data, no real keys with wide scope, monitoring and a kill switch |

### Ten rules that apply to every AI project

1. Enforce safety in **code**, never only in the prompt.
2. Give the model the **least** power it needs (read-only by default).
3. Treat all data returned by tools as **untrusted text**.
4. Keep a **human** between the model and any real-world effect.
5. Log what happened, in a way that **cannot be edited**.
6. Never put **secrets or private rows** in prompts or logs.
7. Set **budgets** (steps, tokens, time, money) in code.
8. **Test the attacks**, don't assume the defences work.
9. A "verified" label must say exactly what was verified, nothing more.
10. Re-run this checklist **before every public release**.

---

## Result log (copy per run)

| Date | Git SHA | Sections run | Failed items | Fixed by | Signed off |
|---|---|---|---|---|---|
| | | | | | |
