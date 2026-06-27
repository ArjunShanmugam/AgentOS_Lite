# AgentOS Lite — Architecture Specification

**Document Version:** 1.0  
**Status:** Design Review Draft  
**Classification:** Internal Engineering Reference  
**Last Updated:** May 2026

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Goals](#2-system-goals)
3. [Architecture Overview](#3-architecture-overview)
4. [System Context](#4-system-context)
5. [Component Architecture](#5-component-architecture)
6. [Data Architecture](#6-data-architecture)
7. [API Architecture](#7-api-architecture)
8. [Security Architecture](#8-security-architecture)
9. [Reliability & Fault Tolerance](#9-reliability--fault-tolerance)
10. [Scalability Strategy](#10-scalability-strategy)
11. [Observability Architecture](#11-observability-architecture)
12. [System Invariants](#12-system-invariants)
13. [Deployment Architecture](#13-deployment-architecture)
14. [Technology Decisions](#14-technology-decisions)
15. [Architecture Decision Records](#15-architecture-decision-records)
16. [Future Evolution](#16-future-evolution)

---

## 1. Executive Summary

### Problem

Modern AI systems fail silently. When an LLM-powered agent encounters a rate limit, a malformed response, or an unavailable service, the standard failure mode is a stack trace and a dead task. There is no observation, no diagnosis, and no recovery — only silence.

This is not a model problem. It is a systems design problem.

### What AgentOS Lite Solves

AgentOS Lite is a self-healing agent infrastructure platform. It provides structured runtime supervision for AI agents: detecting failures as they occur, diagnosing their root cause with confidence scoring, selecting a bounded recovery action from a validated strategy set, and completing the original task without human intervention.

The system is designed around a single architectural conviction: **failures in AI pipelines are not exceptions to be caught — they are events to be observed, classified, and recovered from**.

### High-Level Approach

Three agents form the core system:

- **Executor** — executes tasks using a constrained set of strategies
- **Monitor** — observes execution, detects anomalies, and produces structured root cause analysis
- **Supervisor** — receives RCA reports, selects a recovery strategy from a predefined action space, rewrites the Executor's YAML config, and relaunches

All agent activity is captured as structured logs, scraped by Prometheus, and visualised in Grafana. Every recovery event is recorded in an append-only intervention history that provides full auditability.

### Expected Impact

A B.Tech team of two to three engineers can build and demonstrate this system in four weeks. The result is a portfolio artefact that demonstrates production observability thinking, bounded AI decision-making, fault-tolerant distributed systems design, and quantifiable engineering outcomes — all simultaneously.

The system's benchmark evaluation (20–30 tasks across four categories, with self-healing enabled and disabled) produces concrete metrics: task completion rate, mean recovery time, and Supervisor intervention frequency. These numbers are resume-ready and interview-defensible.

---

## 2. System Goals

### 2.1 Functional Goals

| ID | Goal | Priority |
|----|------|----------|
| F-01 | Accept task submissions via REST API | Must |
| F-02 | Execute tasks using the Executor agent with one of three strategies | Must |
| F-03 | Monitor execution in real time and detect failure events | Must |
| F-04 | Produce structured Root Cause Analysis with failure type, evidence, and confidence score | Must |
| F-05 | Rewrite Executor YAML config using a bounded strategy enum | Must |
| F-06 | Record every recovery attempt in an append-only intervention history | Must |
| F-07 | Expose agent health score as a computed metric | Must |
| F-08 | Replay any task's full event timeline in the UI | Must |
| F-09 | Expose all metrics via Prometheus scrape endpoint | Must |
| F-10 | Render live dashboard in Grafana | Must |
| F-11 | Run automated benchmark harness across 20–30 tasks | Should |
| F-12 | Support YAML config rollback to a previous version | Should |

### 2.2 Non-Functional Goals

**Reliability**
- Task completion rate must exceed 90% across benchmark suite when self-healing is enabled
- No task should fail permanently due to a transient failure if a valid recovery strategy exists
- The intervention history must be append-only; no entry may be deleted or modified

**Performance**
- Mean time from failure detection to recovery action must be under 15 seconds
- Prometheus scrape latency must not exceed 200ms
- Streamlit dashboard must render within 3 seconds under normal load

**Observability**
- Every agent action must emit a structured log entry with task_id, agent_id, strategy, status, and timestamp
- Health score must update within one scrape interval (15 seconds default)

**Security**
- No task executes without passing schema validation
- Supervisor may only select strategies from a predefined, versioned enum
- Executor performs web scraping, RSS parsing, API retrieval, and cache retrieval directly through approved tool bindings. No arbitrary user code execution is supported in v1.

**Maintainability**
- Each agent must be independently restartable without affecting others
- YAML config schema must be versioned; breaking changes require a schema migration
- All benchmark tasks must be reproducible from a JSON ground-truth file

**Cost Efficiency**
- LLM API calls are batched where possible; RCA generation uses a single call per failure event
- E2B sandbox is spun up per task and torn down on completion
- Prometheus retention is set to 15 days by default; configurable per deployment

---

## 3. Architecture Overview

### 3.1 High-Level Description

AgentOS Lite is a three-agent system with a shared observability plane. The agents communicate through a central event bus (Redis Pub/Sub) and share state through a SQLite database (development) or PostgreSQL (production). The Supervisor reads and writes agent configuration from a versioned YAML store on the filesystem.

The system is intentionally flat. There is no dynamic agent spawning, no complex memory graph, and no multi-level planning hierarchy. Every architectural decision prioritises **debuggability** over expressiveness: a system you can fully observe and explain is more valuable than a system that claims to be intelligent.

### 3.2 Component Interaction Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / API Client                        │
└─────────────────────────┬───────────────────────────────────────┘
                          │ POST /tasks
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Gateway                            │
│              Task validation · Auth · Rate limiting             │
└───────────┬────────────────────────────────┬────────────────────┘
            │ Enqueue task                   │ Stream events (SSE)
            ▼                               ▼
┌───────────────────┐            ┌──────────────────────┐
│   Supervisor LLM  │◄──────────►│   Streamlit UI       │
│(Gemini 2.5 Flash) │  config    │  Timeline · Metrics  │
└─────┬──────┬──────┘  rewrite  └──────────────────────┘
      │      │
  spawn    receive
  task     RCA
      │      │
      ▼      │
┌─────────────────┐   structured logs    ┌────────────────────┐
│  Executor Agent │─────────────────────►│   Monitor Agent    │
│  Strategy exec  │                      │  RCA · Health score│
└─────────────────┘                      └────────────────────┘
      │                                          │
      ▼                                          ▼
┌─────────────────────────────────────────────────────────────────┐
│               Observability Layer                               │
│    Prometheus /metrics   ·   Grafana Dashboard   ·   Logs      │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 Request Lifecycle

```
1. Client submits task → FastAPI validates schema → task persisted to DB
2. Supervisor receives task → selects initial strategy → writes Executor config
3. Executor reads config → executes task using specified strategy
4. Executor emits structured log events throughout execution
5. Monitor consumes log stream → computes health score → detects anomalies
6. [On failure] Monitor generates RCA → publishes to Supervisor via Redis
7. Supervisor receives RCA → selects recovery strategy → rewrites config → logs intervention
8. Executor relaunches with updated config → proceeds with recovery strategy
9. [On success] task marked complete → intervention history finalised
10. All events available via SSE stream → Streamlit timeline renders replay
```

---

## 4. System Context

### 4.1 Users

| Actor | Interaction | Trust Level |
|-------|-------------|-------------|
| Developer / student | Submits tasks, views dashboard, runs benchmarks | High (local dev) |
| Evaluator / recruiter | Views demo replay, reads metrics | Read-only |
| CI system | Runs benchmark harness, asserts completion rates | Service account |

### 4.2 External Services

| Service | Purpose | Failure Impact |
|---------|---------|----------------|
| Google AI Studio API (Gemini 2.5 Flash) | Supervisor LLM reasoning and RCA generation | Critical — Supervisor cannot function without it; system falls back to rule-based strategy selection |
| Target web sources | HTML scraping targets | Low — triggers recovery to RSS or API fallback |
| RSS feeds | Fallback data source for scraping tasks | Medium — triggers recovery to API fallback |
| Public REST APIs | Final fallback data source | Medium — task fails permanently if all three strategies exhausted |

### 4.3 Trust Boundaries

```
[Public Internet]
       │
  [FastAPI boundary] ← API key validation here
       │
  [Internal agents] ← No external network access; all retrieval via approved tool bindings
```

No agent has direct internet access except through explicitly configured tool bindings. The Executor may only perform approved retrieval operations (web scraping, RSS parsing, API calls, cache retrieval) defined in `StrategyEnum`. No arbitrary code execution is supported in v1.

---

## 5. Component Architecture

### 5.1 FastAPI Gateway

**Purpose:** Single ingress point for all task submissions and event streaming.

**Responsibilities:**
- Validate incoming task payloads against a Pydantic schema
- Persist validated tasks to the database with status `PENDING`
- Publish task events to the Redis Pub/Sub channel
- Expose a Server-Sent Events (SSE) endpoint for real-time event streaming to the UI
- Expose the Prometheus `/metrics` scrape endpoint

**Inputs:** HTTP POST requests from clients; Prometheus scrape requests

**Outputs:** Task IDs; SSE event stream; Prometheus metrics; HTTP error responses

**Failure Modes:**

| Failure | Behaviour |
|---------|-----------|
| Database unavailable | Return 503; do not enqueue task |
| Redis unavailable | Accept task into DB; queue processing will retry |
| Schema validation failure | Return 422 with detailed error |
| LLM API timeout on health check | Return 200; log degraded status |

**Security Considerations:**
- All inputs validated against strict Pydantic models with field-level constraints
- API key required for task submission (Bearer token in Authorization header)
- Rate limiting applied per API key: 60 requests/minute default
- SSE endpoint is read-only; no authentication required in development mode

---

### 5.2 Supervisor Agent

**Purpose:** The decision-maker. Receives task requests and RCA reports; selects strategies; rewrites Executor configs.

**Responsibilities:**
- Decompose task goals into an initial strategy selection
- Receive structured RCA from the Monitor
- Select a recovery strategy from the bounded `StrategyEnum`
- Write a new versioned YAML config to the config store
- Record the intervention in the intervention history
- Determine when to abandon a task (all strategies exhausted, max retries reached)

**Inputs:**
- Task payload from FastAPI
- RCA report from Monitor (via Redis Pub/Sub)
- Current agent health score from Monitor

**Outputs:**
- YAML config file (versioned, filesystem-backed)
- Intervention history record (appended to SQLite/PostgreSQL)
- Task status updates

**The Bounded Action Space**

The Supervisor may only select from the following strategy enum:

```yaml
allowed_strategies:
  - html_scraping      # Direct HTTP fetch + BeautifulSoup parse
  - rss_fallback       # RSS/Atom feed parse via feedparser
  - api_fallback       # Structured REST API call
  - cached_response    # Return last known good cached result
```

The mapping from failure type to allowed recovery strategies is defined in a static decision table:

```yaml
recovery_map:
  rate_limit:      [rss_fallback, api_fallback, cached_response]
  timeout:         [api_fallback, cached_response]
  parse_error:     [rss_fallback, api_fallback]
  empty_response:  [rss_fallback, api_fallback, cached_response]
  network_error:   [cached_response]
```

**The LLM's role** is selecting *which* valid strategy to try next, given the RCA evidence and the task's history. It does not generate arbitrary config values. All config fields are typed and validated against a JSON Schema before being written to disk.

**Failure Modes:**

| Failure | Behaviour |
|---------|-----------|
| LLM API unavailable | Fall back to rule-based strategy selection (first valid strategy in recovery_map) |
| All strategies exhausted | Mark task FAILED_PERMANENT; emit alert |
| Config schema validation fails | Reject config write; log error; retry with rule-based selection |
| Max retries reached (default: 3) | Mark task FAILED_MAX_RETRIES |

**Security Considerations:**
- LLM output is never executed directly; it is parsed as structured JSON and validated against a schema
- Config writes are atomic (write to temp file, rename); no partial config is ever active

---

### 5.3 Executor Agent

**Purpose:** Executes the actual task work using the strategy specified in its YAML config.

**Responsibilities:**
- Read its YAML config at task start (and on relaunch after recovery)
- Execute the specified strategy using the appropriate tool binding
- Emit structured log events at defined checkpoints: START, FETCH, PARSE, COMPLETE, ERROR
- Write results to the task result store on success
- Cache last successful result for `cached_response` fallback

**Inputs:**
- YAML config (filesystem)
- Task payload (from Supervisor via Redis)

**Outputs:**
- Structured log events (to structlog → file + stdout)
- Task result (to database)
- Cached result (to Redis key with 1-hour TTL)

**Execution Checkpoints and Log Schema:**

```json
{
  "task_id": "uuid",
  "agent_id": "executor-01",
  "strategy": "html_scraping",
  "checkpoint": "FETCH",
  "status": "ERROR",
  "error_type": "rate_limit",
  "http_status": 429,
  "latency_ms": 312,
  "timestamp": "2026-05-26T14:32:04Z"
}
```

**Failure Modes:**

| Failure | Behaviour |
|---------|-----------|
| HTTP 429 | Emit ERROR log with error_type=rate_limit; do not retry |
| Request timeout (>10s) | Emit ERROR log with error_type=timeout |
| Parse failure | Emit ERROR log with error_type=parse_error |
| Tool binding failure | Emit ERROR log with relevant error_type; Monitor generates RCA; Supervisor selects alternative retrieval strategy per recovery_map |

**Security Considerations:**
- Executor performs only approved retrieval operations through tool bindings defined in StrategyEnum; no arbitrary code execution is supported in v1
- No secrets or credentials are passed to external web sources beyond necessary HTTP headers
- Strategy selection is controlled by the Supervisor; the Executor never selects its own strategy

---

### 5.4 Monitor Agent

**Purpose:** Observes the Executor's log stream, detects failure events, computes health scores, and produces structured Root Cause Analysis.

**Responsibilities:**
- Tail the Executor's structured log stream in real time
- Detect failure signatures using a rule-based classifier
- Compute a rolling health score per agent
- Generate a structured RCA report for each detected failure
- Publish RCA to the Supervisor via Redis Pub/Sub

**The Health Score Formula:**

```
Health Score = (0.40 × success_rate) + (0.30 × (1 - error_rate)) + (0.30 × latency_score)

Where:
  success_rate  = successful_tasks / total_tasks (rolling 10-task window)
  error_rate    = failed_tasks / total_tasks (rolling 10-task window)
  latency_score = 1.0 if p95_latency < 5s, 0.5 if < 15s, 0.0 if >= 15s
```

Health score is a value in [0.0, 1.0]. The Supervisor triggers review at score < 0.6; triggers mandatory recovery at score < 0.4.

**The RCA Report Schema:**

```json
{
  "task_id": "uuid",
  "failure_type": "rate_limit",
  "evidence": "HTTP 429 at checkpoint FETCH after 312ms",
  "confidence": 0.92,
  "suggested_strategies": ["rss_fallback", "api_fallback"],
  "health_score": 0.38,
  "timestamp": "2026-05-26T14:32:05Z"
}
```

Confidence is computed from the specificity of the evidence:
- Known HTTP status codes (429, 408, 503): 0.85–0.95
- Timeout with known threshold breach: 0.80–0.90
- Parse failure with partial content: 0.70–0.80
- Unknown / ambiguous errors: 0.40–0.60

**Failure Modes:**

| Failure | Behaviour |
|---------|-----------|
| Log stream interrupted | Emit degraded health warning; last health score remains valid for 30 seconds |
| Redis unavailable | Buffer RCA in memory; retry publish every 5 seconds |
| Unable to classify failure | Emit RCA with confidence < 0.5; Supervisor uses rule-based fallback |

**Security Considerations:**
- Monitor is read-only with respect to task state; it never modifies configs or task records directly
- All RCA output is validated against JSON Schema before publishing

---

## 6. Data Architecture

### 6.1 Core Data Entities

**Task**

```
task_id         UUID (PK)
payload         JSONB          # Original task request
status          ENUM(PENDING, RUNNING, RECOVERING, COMPLETE, FAILED_PERMANENT, FAILED_MAX_RETRIES)
created_at      TIMESTAMP
updated_at      TIMESTAMP
attempt_count   INTEGER        # Default 0; max 3
result          JSONB          # Populated on success
```

**InterventionRecord**

```
intervention_id UUID (PK)
task_id         UUID (FK → Task)
attempt_number  INTEGER
strategy_before VARCHAR
strategy_after  VARCHAR
failure_type    VARCHAR
rca_confidence  FLOAT
supervisor_action VARCHAR
created_at      TIMESTAMP      # Append-only; never updated
```

**AgentConfigVersion**

```
config_id       UUID (PK)
agent_id        VARCHAR        # e.g. "executor-01"
version         INTEGER        # Monotonically increasing
config_yaml     TEXT
is_active       BOOLEAN
created_at      TIMESTAMP
```

**EventLog**

```
event_id        UUID (PK)
task_id         UUID (FK → Task)
agent_id        VARCHAR
checkpoint      VARCHAR
status          VARCHAR
payload         JSONB          # Full structured log entry
created_at      TIMESTAMP
```

### 6.2 Storage Strategy

| Data Type | Storage | Rationale |
|-----------|---------|-----------|
| Task state | SQLite (dev) / PostgreSQL (prod) | ACID guarantees required for status transitions |
| Intervention history | Same DB as tasks | Append-only; transactional with task status update |
| Agent config (active) | Filesystem YAML | Human-readable; inspectable in demo; git-diffable |
| Agent config versions | DB (AgentConfigVersion table) | Enables rollback; version history queryable |
| Cached task results | Redis (1-hour TTL) | Low-latency read; acceptable to lose on restart |
| Event log | DB (EventLog table) | Required for replay; queryable by task_id |
| Prometheus metrics | Prometheus TSDB | Native format; 15-day retention |

### 6.3 Data Flow

```
Executor emits log event
    → structlog writes to stdout + rotating file
    → Monitor tails file (via watchdog) OR reads from structured log queue
    → Monitor writes to EventLog table
    → Prometheus scrapes /metrics (aggregated from EventLog)
    → Grafana reads from Prometheus

Supervisor writes config
    → New AgentConfigVersion row inserted
    → YAML written to filesystem (atomic rename)
    → Previous version marked is_active=False
```

### 6.4 Consistency Requirements

The following operations must be atomic:

- Task status update + InterventionRecord insert (single transaction)
- AgentConfigVersion insert + filesystem YAML write (two-phase: DB first, then filesystem; rollback DB on filesystem failure)
- Task marked COMPLETE + result stored (single transaction)

No distributed transaction protocol is required in the college-scope implementation; SQLite/PostgreSQL local transactions are sufficient.

### 6.5 Retention Strategy

| Data | Retention | Reason |
|------|-----------|--------|
| Task records | 30 days | Demo and benchmark needs |
| Intervention history | 30 days | Audit; never deleted during retention window |
| Event logs | 7 days | High volume; replay only needed for recent tasks |
| Prometheus metrics | 15 days | Trend analysis for benchmark report |
| Config versions | Indefinite | Rollback capability; small data volume |
| Cached results | 1 hour (TTL) | Freshness requirement |

---

## 7. API Architecture

### 7.1 Task Submission

```
POST /api/v1/tasks
Authorization: Bearer <api_key>
Content-Type: application/json

Request:
{
  "task_type": "web_scrape",
  "target": "https://news.ycombinator.com",
  "output_format": "summary_list",
  "max_items": 10
}

Response 202 Accepted:
{
  "task_id": "uuid",
  "status": "PENDING",
  "estimated_duration_s": 30
}

Response 422 Unprocessable:
{
  "error": "schema_validation_failed",
  "detail": [{"field": "max_items", "msg": "must be <= 50"}]
}
```

### 7.2 Task Status

```
GET /api/v1/tasks/{task_id}
Authorization: Bearer <api_key>

Response 200:
{
  "task_id": "uuid",
  "status": "RECOVERING",
  "attempt_count": 2,
  "current_strategy": "rss_fallback",
  "health_score": 0.41,
  "interventions": [
    {
      "attempt": 1,
      "failure_type": "rate_limit",
      "strategy_before": "html_scraping",
      "strategy_after": "rss_fallback",
      "rca_confidence": 0.92,
      "timestamp": "2026-05-26T14:32:05Z"
    }
  ]
}
```

### 7.3 Metrics Endpoint

```
GET /metrics
(No authentication — standard Prometheus convention)

Returns: Prometheus text format
# HELP agentos_task_completion_rate Rolling task completion rate
# TYPE agentos_task_completion_rate gauge
agentos_task_completion_rate 0.94

# HELP agentos_recovery_time_seconds Time from failure detection to recovery
# TYPE agentos_recovery_time_seconds histogram
agentos_recovery_time_seconds_bucket{le="5"} 12
agentos_recovery_time_seconds_bucket{le="15"} 28
```

### 7.4 Event Stream

```
GET /api/v1/tasks/{task_id}/events
(SSE — Server-Sent Events)

data: {"event": "TASK_STARTED", "strategy": "html_scraping", "timestamp": "..."}
data: {"event": "FETCH_FAILED", "error_type": "rate_limit", "http_status": 429, "timestamp": "..."}
data: {"event": "RCA_GENERATED", "failure_type": "rate_limit", "confidence": 0.92, "timestamp": "..."}
data: {"event": "CONFIG_REWRITTEN", "strategy_before": "html_scraping", "strategy_after": "rss_fallback", "timestamp": "..."}
data: {"event": "TASK_COMPLETE", "result_preview": "10 items retrieved", "timestamp": "..."}
```

### 7.5 Error Handling

All errors follow RFC 7807 (Problem Details):

```json
{
  "type": "https://agentos.local/errors/rate_limit_exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "60 requests per minute limit reached for this API key",
  "instance": "/api/v1/tasks/uuid"
}
```

---

## 8. Security Architecture

### 8.1 Threat Model

**Assets:**
- Task payloads (may contain sensitive URLs or query parameters)
- API keys
- Agent config files (manipulation could redirect execution)
- Intervention history (tamper would undermine auditability)
- LLM API credentials

**Entry Points:**
- FastAPI HTTP endpoints (public-facing)
- Redis Pub/Sub (internal only)
- Filesystem config store (local only)
- External web sources (via approved tool bindings only)

**Trust Boundaries:**

```
[Internet] ──► [FastAPI] ──► [Internal agents] ──► [Tool adapters]
 Untrusted      Validated      Semi-trusted         StrategyEnum-constrained

[LLM API] ──► [Supervisor] ──► [Config validator] ──► [Filesystem]
 External       Trusted          Must validate          Protected
```

**Primary Threats:**

| Threat | Likelihood | Impact | Mitigation |
|--------|-----------|--------|------------|
| Prompt injection via task payload | Medium | High | Task payload never inserted into LLM prompt verbatim; structured extraction only |
| Executor performing unauthorised actions | Low | High | Executor constrained to StrategyEnum; no arbitrary code execution in v1 |
| Config tampering to redirect Executor | Low | High | Config schema validation on every read; atomic writes |
| API key leakage | Medium | High | Keys in environment variables; never logged |
| LLM hallucinating invalid strategies | Medium | Medium | Output validated against StrategyEnum; invalid values rejected |

### 8.2 Authentication and Authorization

- API key authentication via Bearer token for all write endpoints
- Keys stored as bcrypt hashes in the database; never stored in plaintext
- No multi-tenant isolation required in v1 (single-user local deployment)

### 8.3 Secrets Management

- All secrets (API keys, LLM credentials) loaded from environment variables via `python-dotenv`
- A `.env.example` file is committed to the repository; `.env` is gitignored
- No secrets appear in logs, config files, or database records

### 8.4 Data Protection

- Task payloads stored in the database; no PII should be included in demo tasks
- Structured logs redact any field matching common PII patterns (email, phone, credit card) before writing
- No secrets or credentials are passed to external retrieval targets beyond necessary HTTP headers

### 8.5 LLM Output Validation

The Supervisor never executes LLM output directly. All LLM responses are:

1. Parsed as JSON (reject on parse failure)
2. Validated against a Pydantic schema (reject on schema failure)
3. Checked against the StrategyEnum (reject if strategy not in allowed list)
4. Checked against the recovery_map for the detected failure type (reject if not a valid recovery for this failure)

Only after passing all four checks is the strategy applied. This makes the LLM a *classifier*, not an *executor*.

### 8.6 Logging and Auditability

- Every Supervisor decision is logged with: task_id, failure_type, strategy_selected, confidence, timestamp
- Intervention history is append-only at the database constraint level (`INSERT` only; no `UPDATE` or `DELETE` permitted on this table)
- All log entries include a correlation ID (task_id) enabling full trace reconstruction

---

## 9. Reliability & Fault Tolerance

### 9.1 Failure Scenarios and Recovery

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| Executor: HTTP 429 | Monitor detects error_type=rate_limit in log | Supervisor switches to rss_fallback or api_fallback |
| Executor: timeout | Monitor detects latency > 10s threshold | Supervisor switches to api_fallback or cached_response |
| Executor: parse failure | Monitor detects error_type=parse_error | Supervisor switches to rss_fallback |
| Tool binding failure | Monitor detects exception in retrieval tool | Monitor generates RCA; Supervisor selects alternative retrieval strategy according to recovery_map |
| LLM API unavailable | Supervisor catches API exception | Rule-based strategy selection from recovery_map |
| Redis unavailable | FastAPI catches connection error | Tasks buffered in DB; Monitor buffers RCA in memory |
| Database unavailable | All agents detect DB error | Halt task; emit alert; do not attempt recovery without state |

### 9.2 Retry Strategy

```
Per-task retry policy:
  max_attempts: 3
  strategies_tried: tracked per task in InterventionHistory
  backoff: none (strategies differ; backoff not useful here)

Per-LLM-call retry policy:
  max_retries: 2
  backoff: exponential (1s, 2s)
  timeout_per_call: 30s

Per-Redis-publish retry policy:
  max_retries: 5
  backoff: linear (5s intervals)
  on_exhaustion: log warning; Supervisor polls DB for pending RCA
```

### 9.3 Circuit Breaker

A lightweight circuit breaker protects LLM API calls:

```
States: CLOSED → OPEN → HALF_OPEN → CLOSED
Threshold: 3 consecutive failures within 60s → OPEN
Open duration: 30s
Half-open: 1 probe call; success → CLOSED, failure → OPEN
```

When the circuit is OPEN, the Supervisor falls back to rule-based strategy selection immediately without waiting for a timeout.

### 9.4 Graceful Degradation

| Component Unavailable | Degraded Behaviour |
|-----------------------|--------------------|
| LLM API | Rule-based recovery; reduced RCA quality; system continues |
| Prometheus | Metrics unavailable; task processing continues |
| Grafana | Dashboard unavailable; system continues |
| Redis | Inter-agent communication degrades; Supervisor polls DB |

No single component failure (except the database) causes total system failure.

### 9.5 Backup Strategy

For the college-scope deployment:
- SQLite database is backed up to a timestamped file every hour via a cron job
- Config YAML files are committed to a git repository; every config rewrite produces a commit
- Prometheus TSDB data is ephemeral in development; 15-day retention in production

---

## 10. Scalability Strategy

### 10.1 Current Scale Assumptions

AgentOS Lite v1 is designed for a single-node deployment with the following expected load:

- 1–5 concurrent tasks
- 20–30 tasks per benchmark run
- 1 Supervisor, 1 Executor, 1 Monitor instance
- Single SQLite database (dev) or PostgreSQL instance (prod)

This is appropriate for a demo and benchmark evaluation environment.

### 10.2 Known Bottlenecks

| Bottleneck | Threshold | Mitigation (v2) |
|-----------|-----------|-----------------|
| Single Executor instance | >5 concurrent tasks | Multiple Executor instances with task queue |
| SQLite write lock | >10 writes/second | PostgreSQL with connection pooling |
| LLM API rate limits | >60 RPM (Google AI Studio free tier) | Request batching; tier upgrade |

### 10.3 Horizontal Scaling Path (v2)

The system is designed to scale horizontally without architectural changes:

- Multiple Executor instances subscribe to a Redis task queue (FIFO)
- Supervisor remains a single instance (Supervisor decisions are sequential by design)
- Monitor scales with Executor instances (one Monitor per Executor, or a single Monitor consuming a multiplexed log stream)
- PostgreSQL with PgBouncer connection pooling handles increased DB load

### 10.4 Caching Strategy

| Cache | Backend | TTL | Purpose |
|-------|---------|-----|---------|
| Task results | Redis | 1 hour | `cached_response` strategy |
| LLM RCA responses | In-memory (LRU, 100 entries) | Session | Identical failure patterns reuse cached RCA |
| Health score | In-memory | 15 seconds | Avoids recompute on every Prometheus scrape |

---

## 11. Observability Architecture

### 11.1 Metrics (Prometheus)

All metrics are exposed at `GET /metrics` in Prometheus text format.

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `agentos_tasks_total` | Counter | status, strategy | Track task outcomes by strategy |
| `agentos_task_completion_rate` | Gauge | — | Rolling completion rate (benchmark key metric) |
| `agentos_recovery_time_seconds` | Histogram | failure_type | Time from failure detection to recovery start |
| `agentos_agent_health_score` | Gauge | agent_id | Current health score per agent |
| `agentos_supervisor_interventions_total` | Counter | strategy_before, strategy_after | Recovery action frequency |
| `agentos_llm_api_latency_seconds` | Histogram | — | LLM call latency |
| `agentos_circuit_breaker_state` | Gauge | component | 0=CLOSED, 1=OPEN, 2=HALF_OPEN |

### 11.2 Structured Logging

All agents use `structlog` configured to emit JSON to stdout and a rotating file (`logs/agentos.jsonl`). Every log entry includes:

```json
{
  "timestamp": "ISO 8601",
  "level": "INFO|WARNING|ERROR",
  "agent_id": "executor-01",
  "task_id": "uuid",
  "event": "human-readable event name",
  "...": "additional context fields"
}
```

Log levels by event type:
- `INFO`: normal checkpoints (START, FETCH, PARSE, COMPLETE)
- `WARNING`: degraded state (circuit breaker HALF_OPEN, health score < 0.6)
- `ERROR`: failure events (rate_limit, timeout, parse_error)
- `CRITICAL`: unrecoverable failures (all strategies exhausted, DB unavailable)

### 11.3 Distributed Tracing

In v1, correlation is achieved via `task_id` propagated through all log entries, DB records, and Redis messages. A full trace for any task can be reconstructed by querying `EventLog WHERE task_id = ?` ordered by `created_at`.

Full OpenTelemetry tracing is deferred to v2 (see Section 16).

### 11.4 Grafana Dashboard

The dashboard contains four panels:

**Panel 1: Agent Health Score (Gauge)**
- Metric: `agentos_agent_health_score`
- Thresholds: red < 0.4, amber < 0.6, green >= 0.6
- Update: every 15 seconds

**Panel 2: Task Completion Rate (Time Series)**
- Metric: `agentos_task_completion_rate`
- Time range: last 1 hour
- Annotation: Supervisor interventions marked as vertical lines

**Panel 3: Recovery Time Distribution (Histogram)**
- Metric: `agentos_recovery_time_seconds`
- Buckets: 2s, 5s, 10s, 15s, 30s

**Panel 4: Intervention History (Table)**
- Source: `/api/v1/interventions` (custom data source via JSON API plugin)
- Columns: task_id, failure_type, strategy_before, strategy_after, confidence, timestamp

### 11.5 Alerting

In development, alerts are logged to the structured log file. In production:

| Alert | Condition | Severity |
|-------|-----------|----------|
| High failure rate | completion_rate < 0.7 for 5 minutes | WARNING |
| Agent health critical | health_score < 0.4 | CRITICAL |
| All strategies exhausted | task status = FAILED_PERMANENT | ERROR |
| LLM circuit breaker open | circuit_breaker_state = 1 | WARNING |

---

## 12. System Invariants

These rules must always be true. Any code change that could violate them requires explicit review.

| ID | Invariant |
|----|-----------|
| INV-01 | A task's `attempt_count` never exceeds `max_attempts` (default: 3) |
| INV-02 | The Supervisor never selects a strategy that is not in `StrategyEnum` |
| INV-03 | The Supervisor never selects a strategy that is not valid for the detected failure type per `recovery_map` |
| INV-04 | LLM output is never written to the filesystem or database without passing Pydantic schema validation |
| INV-05 | Executor may only execute approved retrieval strategies defined in StrategyEnum and may not perform arbitrary code execution |
| INV-06 | InterventionRecord rows are never updated or deleted; only inserted |
| INV-07 | A config YAML file is never written without a corresponding AgentConfigVersion row in the database |
| INV-08 | A task cannot transition from COMPLETE or FAILED_PERMANENT back to RUNNING |
| INV-09 | The health score is always in the range [0.0, 1.0] |
| INV-10 | Every structured log entry must contain task_id, agent_id, checkpoint, and timestamp |

Invariants INV-02 and INV-03 are enforced by the config validator, not by convention. The validator runs as the final gate before any config is written, regardless of how the strategy was selected.

---

## 13. Deployment Architecture

### 13.1 Development Environment

```
Developer machine (local)
├── Python 3.11 virtualenv
├── FastAPI (uvicorn, port 8000)
├── Streamlit (port 8501)
├── SQLite (./data/agentos.db)
├── Redis (Docker, port 6379)
├── Prometheus (Docker, port 9090)
└── Grafana (Docker, port 3000)
```

Docker Compose manages the Redis, Prometheus, and Grafana services. Python processes run natively for easier debugging.

```yaml
# docker-compose.yml (abbreviated)
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  prometheus:
    image: prom/prometheus:latest
    volumes: ["./prometheus.yml:/etc/prometheus/prometheus.yml"]
    ports: ["9090:9090"]
  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
```

### 13.2 Staging / Demo Environment

A single Linux VM (Ubuntu 24.04, 4 vCPU, 8 GB RAM) running all components:

```
/opt/agentos/
├── app/          # FastAPI + agents
├── data/         # SQLite DB + config YAML
├── logs/         # Structured JSON logs
└── configs/      # Versioned agent YAML configs
```

All Python processes managed by `systemd` units. Docker Compose runs infrastructure services.

### 13.3 CI/CD Flow

```
Developer pushes to main
    → GitHub Actions triggers
    → Install dependencies
    → Run unit tests (pytest)
    → Run benchmark harness (20 tasks)
    → Assert: completion_rate >= 0.90
    → Assert: mean_recovery_time <= 15s
    → Build Docker images
    → Deploy to staging VM (rsync + systemd restart)
```

The benchmark harness is the primary quality gate. A deployment that degrades task completion rate below 90% is rejected automatically.

### 13.4 Infrastructure Layout

```
┌──────────────────────────────────────────────┐
│              Single VM / Dev Machine          │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ FastAPI  │  │Supervisor│  │ Executor  │  │
│  │  :8000   │  │          │  │           │  │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│       │              │              │        │
│       └──────────────┼──────────────┘        │
│                      │ Redis :6379            │
│  ┌──────────┐  ┌─────┴──────┐               │
│  │Streamlit │  │  Monitor   │               │
│  │  :8501   │  │            │               │
│  └──────────┘  └────────────┘               │
│                                              │
│  ┌──────────┐  ┌────────────┐               │
│  │Prometheus│  │  Grafana   │               │
│  │  :9090   │  │   :3000    │               │
│  └──────────┘  └────────────┘               │
└──────────────────────────────────────────────┘
```

---

## 14. Technology Decisions

| Layer | Technology | Justification |
|-------|-----------|---------------|
| Orchestration | LangGraph | Stateful agent graph with explicit node transitions; better debuggability than CrewAI's implicit task routing |
| Supervisor LLM | Gemini 2.5 Flash (Google AI Studio API) | Lower operating cost; fast structured JSON generation; excellent response latency; generous developer-tier usage; well suited for classification and bounded decision-making tasks |
| Event bus | Redis Pub/Sub | Low-latency inter-agent messaging; also provides caching layer; single dependency |
| Database (dev) | SQLite | Zero-configuration; sufficient for single-node; easy to inspect with standard tools |
| Database (prod) | PostgreSQL | ACID guarantees; connection pooling; JSON column support for structured payloads |
| Task execution layer | Native Python Tool Adapters | Simpler implementation, fewer dependencies, sufficient for web retrieval workflows in v1. Executor strategies implemented as pure Python modules using httpx, BeautifulSoup, and feedparser |
| Structured logging | structlog | JSON output natively; context binding per agent; performance overhead negligible |
| Metrics | Prometheus | Industry standard; native Grafana integration; pull model simplifies agent implementation |
| Dashboards | Grafana | First-class Prometheus integration; pre-built panel types; free and open source |
| API framework | FastAPI | Async-native; automatic OpenAPI docs; Pydantic integration for request validation |
| UI | Streamlit | Rapid development; Python-native; sufficient for demo; event stream rendering via `st.empty()` |
| Config format | YAML | Human-readable; diff-friendly; widely understood; validates cleanly against JSON Schema |
| Config validation | Pydantic + JSON Schema | Two-layer validation: Pydantic for Python objects, JSON Schema for YAML files |

**Tradeoff: LangGraph vs CrewAI**

CrewAI provides a higher-level abstraction (roles, goals, tools) that reduces boilerplate. LangGraph provides an explicit state machine (nodes, edges, conditional routing) that makes the system's behaviour fully inspectable. For a project whose primary value is *demonstrable* self-healing, inspectability wins. Every state transition in LangGraph can be logged, replayed, and explained. CrewAI's implicit routing cannot.

**Tradeoff: SQLite vs PostgreSQL in development**

SQLite removes all database infrastructure overhead in development. The schema is identical to PostgreSQL (via SQLAlchemy models); migration to PostgreSQL requires only a connection string change. The write concurrency limitation of SQLite is not a constraint at the development scale (single-node, 1–5 concurrent tasks).

---

## 15. Architecture Decision Records

### ADR-001: Bounded Strategy Enum Over Free-Form LLM Config Generation

**Context:** The Supervisor must select a recovery strategy. Two approaches were considered: allow the LLM to generate arbitrary config values, or constrain it to a predefined enum.

**Decision:** The Supervisor selects from a bounded `StrategyEnum`. The LLM's output is parsed as structured JSON and validated before any config is written.

**Alternatives Considered:**
- *Free-form LLM config generation:* The LLM writes any YAML it chooses. Rejected because: (1) untestable — you cannot enumerate all possible outputs; (2) unsafe — a hallucinated strategy could redirect execution unpredictably; (3) indefensible in interviews — "the LLM decided" is not an architecture.

**Tradeoffs:**
- Pro: Every valid state transition is enumerable and testable
- Pro: Supervisor decisions are auditable and explainable
- Pro: Config schema validation is straightforward
- Con: New strategies require code changes (not just prompting)

**Risk:** The bounded action space may fail to handle unanticipated failure types. Mitigated by the `confidence` field in RCA: low-confidence RCA triggers human notification rather than automatic recovery.

---

### ADR-002: Filesystem YAML for Active Config With Database Version History

**Context:** Agent configs need to be readable by agents, writable by the Supervisor, and versionable for rollback.

**Decision:** Active config lives on the filesystem as a human-readable YAML file. Every config write creates a new `AgentConfigVersion` row in the database. The Supervisor can roll back by marking a previous version active and rewriting the filesystem file.

**Alternatives Considered:**
- *Redis only:* Fast, but opaque — configs are not inspectable without a Redis client. Rejected because the visible YAML diff is a core demo feature.
- *Database only:* Requires agents to query the DB on startup. Adds a runtime dependency; harder to inspect. Rejected.
- *Redis with version history:* Possible, but Redis is not the right tool for relational version queries. Rejected.

**Tradeoffs:**
- Pro: Active config is immediately inspectable (open the file)
- Pro: Version history is queryable (SQL)
- Pro: Rollback is a single DB query + atomic file write
- Con: Two writes must stay consistent (DB + filesystem); mitigated by DB-first write order

---

### ADR-003: Append-Only Intervention History

**Context:** The system needs an audit trail of every recovery decision the Supervisor has made.

**Decision:** The `InterventionRecord` table is append-only at the database constraint level. No `UPDATE` or `DELETE` statement is ever issued against this table. Application-level code enforces this; a database-level row security policy enforces it in production.

**Alternatives Considered:**
- *Mutable records with updated_at:* Simpler to implement but loses the ability to distinguish a corrected record from a tampered one. Rejected because auditability is a first-class requirement.
- *File-based append log:* Possible, but not queryable without parsing. Rejected in favour of SQL queryability.

**Tradeoffs:**
- Pro: Full causal history of every recovery decision
- Pro: Tamper-evident (no row can be silently modified)
- Con: Table grows unboundedly (mitigated by 30-day retention via archiving, not deletion)

---

### ADR-004: Rule-Based Fallback for Supervisor When LLM Is Unavailable

**Context:** The Supervisor depends on the LLM API for strategy selection. The LLM API can be unavailable.

**Decision:** When the LLM API is unavailable (circuit breaker OPEN), the Supervisor selects the first valid strategy from `recovery_map[failure_type]` deterministically. This is implemented as a pure Python function with no external dependencies.

**Alternatives Considered:**
- *Fail the task immediately:* Simple but means all tasks fail during any LLM API outage. Rejected because the system's value is resilience.
- *Queue for later:* Adds significant complexity. Rejected for v1.

**Tradeoffs:**
- Pro: System continues to function during LLM outages
- Pro: Rule-based selection is 100% testable
- Con: Recovery quality degrades (no RCA confidence scoring; always picks first valid strategy)

---

## 16. Future Evolution

### 16.1 Intentionally Deferred Features

| Feature | Reason Deferred | Target Version |
|---------|----------------|----------------|
| Planner agent | Adds planning complexity without improving the self-healing story | v2 |
| Dynamic agent spawning | Significant orchestration complexity; not required for v1 demo | v2 |
| Multi-tenant isolation | Single-user scope is sufficient for portfolio and demo | v2 |
| OpenTelemetry tracing | Full distributed tracing valuable at scale; overkill for single-node | v2 |
| Kubernetes deployment | Operational overhead unjustified for college-scope | v3 |
| Multiple simultaneous Executors | Requires task queue and result aggregation; v1 is single-task | v2 |
| E2B sandbox-based task execution | Isolated execution environment for code-execution tasks; adds API key management and integration overhead not required for v1 web retrieval workflows | v2 |

### 16.2 Scaling Roadmap

**v2 (Post-project, 1–2 months):**
- Add Planner agent to handle multi-step task decomposition
- Multiple Executor instances with Redis task queue (FIFO)
- PostgreSQL as default storage
- OpenTelemetry tracing with Jaeger
- **E2B sandbox-based task execution:** Integrate E2B as an isolated execution environment for Python code-execution tasks. Provides stronger security boundaries and support for workflows beyond web retrieval. Deferred from v1 because current functionality only requires the four web retrieval strategies (`html_scraping`, `rss_fallback`, `api_fallback`, `cached_response`).

**v3 (Productionisation, 3–6 months):**
- Kubernetes deployment with Helm charts
- Horizontal Executor autoscaling based on queue depth
- Multi-tenant isolation with per-tenant task namespacing
- LLM fine-tuning on intervention history to improve RCA accuracy

### 16.3 Research Opportunities

The intervention history accumulated by this system is a dataset. With 1,000+ task executions, it becomes possible to:

- Train a lightweight classifier to replace the LLM for RCA generation (lower latency, lower cost)
- Analyse which failure types correlate with which recovery strategies succeeding
- Study whether health score is predictive of imminent failure (anomaly detection)
- Evaluate whether confidence scores from the Monitor are calibrated (reliability diagrams)

These are graduate-level research directions that emerge naturally from running the system at scale. Document them in the README as future work.

### 16.4 Architecture Improvements

- **Saga pattern for distributed recovery:** As the system scales to multiple nodes, the two-phase config write (DB + filesystem) should be replaced with a proper saga with compensating transactions.
- **Event sourcing for task state:** Replacing the mutable `task.status` field with an append-only event log (in addition to the existing EventLog) would make the task state fully reconstructable from events — a more robust model at scale.
- **Strategy performance learning:** Track which strategies succeed for which failure types over time, and bias the Supervisor's selection toward historically successful strategies. This closes the loop between observability data and decision quality.

---

*End of Architecture Specification*

*This document is a living specification. Any architectural change that touches Section 12 (System Invariants) or Section 15 (ADRs) requires a new ADR entry before implementation.*
