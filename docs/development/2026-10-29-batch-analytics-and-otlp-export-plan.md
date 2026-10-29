# Batch Analytics and OTLP Trace Export: Implementation Plan

Status: Ready to implement
Date: 2026-10-29
Parent design: [Autonomous Test Pipeline Design](2026-10-19-autonomous-test-pipeline-design.md)
(§4.5 benchmarks, §4.6.4 artifacts, §4.9 Langfuse, §7 steps 6, 10 and 15)
Related ADRs:
[ADR-2026-10-19: Autonomous Test Pipeline](../decisions/2026-10-19-autonomous-test-pipeline.md),
[ADR-2026-10-19: Langfuse Observability](../decisions/2026-10-19-langfuse-observability.md)
Guide to update: [Testing Pipeline Guide](testing-pipeline.md)

This document is self-contained: an implementer (human or agent) can build both parts from it
without the conversation that produced it. Read §0 first, then work through §7 step by step.

---

## 0. How to Use This Plan

- **Two parts, one link.**
  - **Part A**, local batch analytics: a machine-readable, self-contained result of every
    Golden Set run, a run history, and a regression verdict between runs. No external service.
  - **Part B**, OTLP trace export: the same runs as OpenTelemetry spans, viewable in
    Arize Phoenix, Langfuse, Jaeger or Grafana Tempo.
  - Both carry the same identifiers (§6), so a run found in a backend UI can be matched to its
    local files and verdict.
- **Order.** Prerequisite P0, then Part A (steps A1–A7), then Part B (steps B1–B6). Part B
  reuses A1 (`stop_reason`) and A3 (session labels).
- **Branches.** Strictly sequential: each branch is cut from `main` after the previous one is
  merged (a pull request per branch; the `gate` check must be green before merging).

  | # | Branch | Content | Starts after |
  |---|---|---|---|
  | 1 | `feat/golden-graders` | step 7 of the parent plan (graders, `run_batch --grade`), the 18-scenario Golden Set, batch metrics and `report.md`, batch profiles in the batch folder, UTF-8 config reading, this plan | already exists: push, PR, merge (P0) |
  | 2 | `feat/batch-analytics` | Part A (A1–A7) | branch 1 merged |
  | 3 | `feat/otlp-export` | Part B (B1–B6); needs A1 `stop_reason` and A3 session labels | branch 2 merged |
  | 4 | `feat/cassettes` | parent plan steps 8–9: cassette replay and recording | branch 3 merged |
  | 5 | `feat/ci-orchestrator` | parent plan step 10 (`scripts/ci.py`, stages, report) together with the rest of step 6 (statistics, A/B, history verdict) | branch 4 merged |
  | 6 | `feat/live-stages` | parent plan step 11: fixture site, `live_local`/`live_web`/`bench_live` | branch 5 merged |
  | 7 | `ci/nightly` | parent plan step 13: `nightly.yml`, required checks, `ci-history` | branch 6 merged; needs an answer to Q2 of the parent design (where nightly runs) |

  Branches 4–7 follow the parent design, not this plan: it is a design document with open
  questions (Q1–Q9), so stop and ask at each of them instead of guessing. Starting branch 3
  from branch 2 before it is merged is possible after A3, but then every change to branch 2
  needs a rebase of branch 3 — avoid it.
- **Project rules that apply to every step** (from `CLAUDE.md`):
  - Every tunable is a field of `src/config.py`, mirrored in `.env.example` and
    `config.example.yaml`, and checked by `tests/test_config.py`. No module constants for
    limits, sizes, thresholds or timeouts.
  - No logic keyed on tool, site or page names.
  - `src/agent_loop/execution/` imports nothing from the analytics modules; the fence is
    `tests/test_engine_boundaries.py::PIPELINE_MODULES` — add every new analytics module to it.
  - Secrets are redacted before anything is persisted or exported (`redact_json_safe`).
  - One commit per step (Conventional Commits, English, `.claude/conventions/commit.md`).
    Run the full suite once at the end of each part, then one `code-reviewer` pass over the
    whole part.
- **Verification commands** used throughout:

  ```powershell
  uv run pytest                                   # full suite (~2–4 min)
  uv run pytest tests\test_<area>.py              # focused
  uv run scripts/run_evals.py --memory-seed tests/evals/memory_seed   # evals must stay green
  ```

---

## 1. Context and Goal

The Golden Set (`tests/golden/tasks.jsonl`, 18 scenarios / 61 tasks) is run live with:

```powershell
uv run scripts/run_batch.py --tasks tests\golden\tasks.jsonl --continue-on-error --grade
```

The first full run (batch `batch-cecc32cde63745cba2a50ba5714355b6`, 2026-10-29) showed what is
missing:

- **"failed" mixes unrelated causes.** 11 of 13 failures were model provider errors
  (`ollama 502`, `timed out after 60 s`), two were blocked by the user's personal permission
  rules and approval classifier, one hit an anti-bot page. Nothing machine-readable tells them
  apart, so any automatic analysis would call a provider outage an agent regression.
- **Runs are not comparable.** The batch does not record the commit, the settings in force or
  the version of the Golden Set.
- **The batch folder is not self-contained.** Traces live in `<home>/sessions/<id>/`; the
  batch folder only links to them.
- **No history and no verdict.** Nothing answers "is this run worse than the previous one".
- **No trace UI.** The user wants the runs in Phoenix or Langfuse as well.

Goal: both local, automatable analytics (Part A) and an export to standard trace backends
(Part B), sharing identifiers.

---

## 2. Current State (what exists, with references)

| Piece | Where | Notes |
|---|---|---|
| Batch runner | `src/agent_loop/batch.py::run_batch` | one fresh `SessionRuntime` per scenario; writes `batch.json`, `run_index.jsonl`, `report.md` into `<storage.batches_dir>/<batch_id>/` |
| Batch CLI | `scripts/run_batch.py` | picks `batch_id`, gives scenario N a Chrome profile in `<batch_dir>/profiles/scenario-N` (deleted at the end), `--grade` |
| Row of `run_index.jsonl` | `batch.py::_run_index_row` + `run_batch` | keys today: `batch_id`, `scenario_id`, `session_id`, `status` (`completed`/`failed`), `session_dir`, `tasks`, `expected`, `tags`, `started_at`, `finished_at`, `error`, `task_statuses`, `final_answer`, `grade` (with `grade=True`), `metrics` |
| Graders | `src/agent_loop/graders.py::grade_answer` | `Grade.status` ∈ `passed`/`failed`/`ungraded`; a task not ending `done` grades as failed with a `run` check (`batch.py::_grade`) |
| Timings | `src/agent_loop/benchmarks.py` | `TaskTimings`, `extract_task_timings`, `split_by_task`, `RunMetrics`, `summarize_run` |
| Report | `src/agent_loop/batch_report.py` | `render_batch_report`, `total_metrics`, `REPORT_FILE_NAME` |
| Events | `src/agent_loop/events.py` | `EventRecord` (`type`, `source`, `payload`, `timestamp`, `session_id`, `goal_id`, `task_id`, `sequence`), `EventType` literal, `redact_json_safe` (integers under `*_tokens` keys survive redaction) |
| Event sinks | `EventSink` protocol (only `emit`), `JsonlEventSink`, `CompositeEventSink` in `events.py`; `src/harness/session.py::_build_session_event_sink` composes `events.jsonl`, `agent_trace.jsonl` and `SessionConfig.event_sinks` | there is no `close()` on sinks; `SessionContext.close()` emits `session.closed` last |
| Session events | `session.started` (`session_id`, `resumed`, `skills`), `session.closed`; goal events from `src/agent_loop/goals.py` (`goal.started` → `goal.completed`/`blocked`/`failed`/`cancelled`, payload `task`, `status`, `final_answer`, `turns`; `goal.failed` has `error` = `{type, message}`) | |
| Engine terminals | `src/agent_loop/execution/guards.py::_terminal_guard` (max replans, consecutive failures, no plan advance), `loop.py` (turn cap ~l.1002, model `stop` ~l.528–560, approval hook denied ~l.717, human approval denied ~l.730, no actionable step ~l.408, unexpected decision ~l.363) | the reason exists only as text in `final_answer` (`"Blocked: …"`) |
| Permission decisions | `permission.decided` payload: `tool`, `server`, `decision` (`allow`/`ask`/`deny`), `source` (`PermissionSource` = `rule`/`hook`/`model`/`classifier`/`mode`/`annotation`/`grant`/`error`), `rule_id`, `mode`, `reason` | |
| Model usage | `model.responded` payload `phase` (`plan`/`agent`/`approval`), `usage` (`input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`, `provider_duration_ms`, `load_duration_ms`) | |
| Settings | `src/config.py`: 13 sections incl. `pipeline` (`PipelineSettings.request_chars_tolerance`) | |
| OTel packages | `pyproject.toml`: `opentelemetry-api==1.39.1`, `opentelemetry-sdk==1.39.1`, `opentelemetry-semantic-conventions==0.60b1` (no OTLP exporter yet) | |

---

## 3. Requirements

### 3.1 Functional

- **R1.** Every scenario of a batch run gets one `failure_kind` from a fixed vocabulary,
  derived from its trace and grade, never from site or page knowledge.
- **R2.** `batch.json` records provenance: commit, branch, dirty flag, model, provider, the
  Golden Set file hash, a fingerprint of the effective settings and the settings sections that
  differ from code defaults.
- **R3.** The batch folder holds each scenario's full trace and a readable version of it.
- **R4.** Each run appends one row per scenario to a history file.
- **R5.** A comparison command gives a verdict between two runs (default: the latest against
  the previous comparable one), with exit codes for automation.
- **R6.** With `telemetry.otlp_enabled`, every session exports its tasks as OpenTelemetry
  spans over OTLP/HTTP to any compatible backend.
- **R7.** Spans and local files share identifiers (`batch.id`, `scenario.id`,
  `session.id`, `task.id`, commit, `stop_reason`).

### 3.2 Non-functional

- **N1.** Off by default and hermetic: tests and the CI gate never need a network or a backend.
- **N2.** No content leaves the machine unless asked: `telemetry.capture_content="none"` by
  default; whatever is exported passes `redact_json_safe`.
- **N3.** The exporter can never break or slow a session: errors are swallowed and logged
  once; spans are batched in the background.
- **N4.** Additive: the engine change is a telemetry field only; existing events, rows and
  baselines keep their meaning (`run_evals.py` stays green without a baseline rewrite).
- **N5.** Windows first (UTF-8 files, no POSIX-only tools), Linux works too.

### 3.3 Out of scope

- An LLM judge for ungraded keys (open question Q7 of the parent design).
- Latency percentiles, A/B bootstrap statistics, `ci.py` orchestration, nightly CI (parent
  plan steps 6, 10, 13) — this plan prepares their data, it does not build them.
- A Langfuse SDK dependency, Langfuse scores or datasets API: only standard OTLP.

---

## 4. Part A — Local Batch Analytics

### A1. `stop_reason`: a structured terminal reason from the engine

Today a blocked task explains itself only in prose. Add a machine-readable reason.

- `src/agent_loop/execution/state.py::LoopState`: new field `stop_reason: str = ""`
  (task-local; reset like `completion_status`; not in `SESSION_STATE_KEYS`).
- `AgentLoopResult` (`loop.py`): new field `stop_reason: str = ""`, filled from the terminal
  state.
- `src/agent_loop/goals.py`: the terminal goal event payload gets `"stop_reason"` (empty string
  omitted). `goal.failed` keeps its `error` and gets no stop reason.
- Vocabulary (a `StopReason` `Literal` in `src/contracts.py`, exported):

  | Value | Set at |
  |---|---|
  | `answered` | the model's `done`/answer (`loop.py`, `_done_response` path for a model answer) |
  | `model_stop` | the model's `stop` action with any status (`loop.py` ~l.528–560) |
  | `turn_cap` | turn cap reached (`loop.py` ~l.1002) |
  | `max_replans` | `_terminal_guard`, replanning limit |
  | `consecutive_failures` | `_terminal_guard`, failure limit |
  | `no_plan_advance` | `_terminal_guard`, plan step did not advance |
  | `approval_denied` | a human or `dont_ask` refused an approval (`loop.py` ~l.730) |
  | `approval_hook_denied` | a `permission_request` hook refused (`loop.py` ~l.717) |
  | `no_action` | the model returned no actionable step (`loop.py` ~l.408) |
  | `invalid_decision` | unexpected loop decision (`loop.py` ~l.363) |
  | `cancelled` | the run was cancelled |

  Implementation: `_blocked_response(state, reason, *, stop_reason)` and
  `_done_response(..., stop_reason=...)` put `"stop_reason"` into the update; every call site
  passes its value. Grep `blocked_response(` and `done_response(` to find them all.
- Tests: `tests/test_agent_loop_hooks.py`/`tests/test_engine_events.py` style — one test per
  value asserting `AgentLoopResult.stop_reason` and the goal event payload.
  `tests/test_engine_boundaries.py` must still pass (no new imports in `execution/`).
- Evals: `stop_reason` is not part of `EvalResult.baseline_metrics()`; the v2 baseline does not
  change.

### A2. `failure_kind`: classify each task and scenario

New module `src/agent_loop/failures.py` (pure, no I/O):

```python
FailureKind = Literal[
    "passed", "provider_error", "timeout", "permission_denied", "guard_limit",
    "agent_stopped", "grade_failed", "error", "ungraded",
]

@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    status: str            # goal status: completed / blocked / failed / cancelled / missing
    stop_reason: str       # from the goal event, "" when absent
    error_type: str        # goal.failed error.type, "" otherwise
    permission_denies: int # permission.decided with decision "deny" in this task
    failure_kind: FailureKind

def classify_task(events: Sequence[EventRecord]) -> TaskOutcome: ...
def classify_scenario(outcomes: Sequence[TaskOutcome], grade_status: str | None) -> FailureKind: ...
```

Task rules, first match wins:

1. `goal.failed` with `error.type == "ModelProviderError"` → `provider_error`.
2. `goal.failed` with `error.type` in (`TimeoutError`, `asyncio.TimeoutError`) → `timeout`.
3. any other `goal.failed`, or no terminal goal event → `error`.
4. `stop_reason` in (`approval_denied`, `approval_hook_denied`) → `permission_denied`.
5. not `completed` and `permission_denies > 0` → `permission_denied` (a deny made the task
   fail even when the final reason is a guard or a model stop).
6. `stop_reason` in (`turn_cap`, `max_replans`, `consecutive_failures`, `no_plan_advance`,
   `no_action`, `invalid_decision`) → `guard_limit`.
7. `blocked`/`cancelled` with `stop_reason == "model_stop"` (or no stop reason) →
   `agent_stopped` (the model gave up: an anti-bot page, a login wall; the reason is in its
   answer — deliberately not parsed).
8. `completed` → `passed`.

Scenario rule: the first task whose kind is not `passed` decides; if every task passed, the
grade decides: `failed` → `grade_failed`, `ungraded` or no grading → `ungraded`, `passed` →
`passed`. A scenario that raised before any goal event (`status: failed`, no trace) uses the
row's `error.type` with rules 1–3.

Infra kinds (`provider_error`, `timeout`) are not the agent's fault; every other non-passed
kind is. `failures.py` exports `INFRA_KINDS = frozenset({"provider_error", "timeout"})`.

Tests `tests/test_failures.py`: one synthetic trace per rule (build `EventRecord`s as in
`tests/test_benchmarks.py`), precedence cases (deny + guard → `permission_denied`; provider
error in task 2 after task 1 passed), the scenario rule.

### A3. Data contract v1: rows, `batch.json`, session labels

**Session labels** (shared with Part B):

- `SessionConfig` gets `labels: Mapping[str, str] = {}` (`src/harness/session.py`); the
  session puts them into the `session.started` payload as `"labels"`.
- `scripts/run_batch.py::_session_factory` sets, per scenario:
  `{"autobrowser.batch.id": batch_id, "autobrowser.scenario.id": scenario_id}` — the factory
  needs the scenario id, so `run_batch` passes it: change `BatchSessionFactory` to
  `Callable[[BatchScenario], BatchSessionRuntime]` and call `session_factory(scenario)`.
  Update `tests/test_agent_loop_batch.py` fakes accordingly.

**`run_index.jsonl` row v1** — existing keys plus:

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int `1` | row format version |
| `failure_kind` | `FailureKind` | §A2 scenario kind |
| `task_outcomes` | list of `{task_id, status, stop_reason, error_type, permission_denies, failure_kind}` | §A2 per task |
| `trace_path` | str | `traces/<file>.events.jsonl`, relative to the batch dir (A4) |
| `trace_text_path` | str | `traces/<file>.txt` |

**`batch.json` v1** — existing keys plus `schema_version: 1` and `provenance`:

| Key | How it is obtained |
|---|---|
| `git.commit`, `git.branch`, `git.dirty` | `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git status --porcelain` (non-empty → dirty), run by `scripts/run_batch.py` (subprocess, `ROOT` as cwd, failures → `null`) |
| `model`, `provider` | `get_settings().llm.model`, `.provider` (after CLI overrides) |
| `tasks_sha256` | sha256 of the tasks file with `\r\n` normalized to `\n` |
| `settings_sha256` | sha256 of `json.dumps(redact_json_safe(get_settings().model_dump(mode="json")), sort_keys=True)` |
| `non_default_sections` | sorted names of the `Settings` sections whose value differs from the section class's default instance (e.g. `["llm", "permissions"]`) |
| `python`, `platform` | `platform.python_version()`, `sys.platform` |

`run_batch` gets `provenance: Mapping[str, Any] | None = None` and writes it into the metadata;
the script builds it (`scripts/run_batch.py::_provenance`), keeping subprocess calls out of
`src/`. A helper for the two settings fields goes into `src/agent_loop/batch.py` (pure, takes
`Settings`), so tests can call it.

### A4. Self-contained batch folder: `traces/`

After a scenario's session closes (where `run_batch` already computes `metrics`):

- copy `<session_dir>/events.jsonl` to `<batch_dir>/traces/<name>.events.jsonl`;
- write `<batch_dir>/traces/<name>.txt`: for each task, a heading with the task text and its
  outcome (`status`, `stop_reason`, `failure_kind`), then
  `src/agent_loop/replay.py::print_action_sequence` of that task's events;
- `<name>` = `scenario_id` with every character outside `[A-Za-z0-9._-]` replaced by `_`; on a
  collision append `-2`, `-3`.

Missing trace (the scenario raised before the session wrote one): no files, both path keys
`null`. `report.md` links `traces/<name>.txt` in its "Traces" section instead of the session
directory (keep the session path as a second link).

### A5. History

- File: `<storage.batches_dir>/history.jsonl`, one row per scenario per run, appended at the
  end of `run_batch` (also when the batch failed part-way).
- Row: `schema_version`, `batch_id`, `started_at`, `git.commit`, `git.dirty`, `model`,
  `tasks_sha256`, `settings_sha256`, `scenario_id`, `grade_status`, `failure_kind`, and the
  `metrics` keys (`duration_ms`, `tasks`, `model_calls`, `input_tokens`, `output_tokens`,
  `model_ms`, `tool_calls`, `tool_ms`, `tool_errors`, `policy_blocks`).
- Rotation: new setting `pipeline.history_max_rows` (int, default `50000`, bounds
  `100..10_000_000`, env `AUTOBROWSER_PIPELINE__HISTORY_MAX_ROWS`; parent design §4.8 already
  names it). When the file exceeds it, keep the newest rows (rewrite via a temp file and
  `os.replace`).
- `scripts/run_batch.py --no-history` skips the append (for experiments).
- Module: `src/agent_loop/batch_history.py` (`history_rows(metadata, rows)`,
  `append_history(path, rows, max_rows)`, `load_history(path)`).

### A6. Verdict between runs: `scripts/compare_batches.py`

```powershell
uv run scripts/compare_batches.py                      # latest run vs the previous comparable one
uv run scripts/compare_batches.py <batch_a> <batch_b>  # ids or directories; b is the newer
uv run scripts/compare_batches.py --json               # machine output
```

- Comparable: same `tasks_sha256`; a differing `settings_sha256` or `model` is reported as a
  warning line, not an error.
- Per scenario present in both, the change of `failure_kind`:

  | Before → after | Finding |
  |---|---|
  | `passed` → agent kind (not infra, not `ungraded`) | `regression` |
  | agent kind → `passed` | `fixed` |
  | anything → infra kind | `inconclusive` (rerun it) |
  | other changes | `changed` |
  | only in one run | `added` / `removed` |

- Metrics, for scenarios `passed` in both: relative change of `duration_ms`, `model_calls`,
  `input_tokens`; above `pipeline.batch_metric_tolerance` (new setting, float, default `0.25`,
  bounds `0.0..10.0`, env `AUTOBROWSER_PIPELINE__BATCH_METRIC_TOLERANCE`) → `slower` /
  `costlier`; a single live run is noisy, so these never fail the command unless `--perf-gate`.
- Output: one line per finding (same style as `run_evals.py`), plus `compare.md` written into
  the newer batch dir.
- Exit codes: `0` no regression; `1` at least one `regression`; `2` unreadable input or
  incomparable sets (`tasks_sha256` differs); `3` only perf findings and `--perf-gate`.
- Logic in `src/agent_loop/batch_compare.py` (pure: `compare_batches(meta_a, rows_a, meta_b,
  rows_b, settings) -> BatchComparison`), the script only loads files and prints.

### A7. Report and docs

- `report.md`: a provenance block under the header (commit + dirty mark, model, settings
  fingerprint short hash, non-default sections); a `Kind` column after `Grade`; a summary line
  "Agent failures: N · Infra failures: M · Passed: K"; the "Failed scenarios" section groups by
  `failure_kind`, infra kinds last with "rerun" advice.
- `docs/development/testing-pipeline.md`: the row/`batch.json` contract tables (A3), `traces/`,
  history, `compare_batches.py`, how to query with DuckDB:

  ```sql
  SELECT scenario_id, failure_kind, metrics.duration_ms
  FROM read_json_auto('~/.autobrowser/batches/*/run_index.jsonl')
  WHERE failure_kind NOT IN ('passed', 'provider_error', 'timeout');
  ```

- `docs/glossary.md`: Failure kind, Stop reason, Batch history.

---

## 5. Part B — OTLP Trace Export

### B1. ADR

New `docs/decisions/2026-10-29-otlp-trace-export.md` (template
`docs/decisions/adr-template.md`, register on top of `docs/decisions/index.md`), extending the
Langfuse ADR: the user needs a trace UI (Phoenix and Langfuse) — the trigger the parent design
names (§4.9.4). Decision: the deferred neutral OTLP exporter is introduced; still no Langfuse SDK,
service or dataset API. `Related:` the Langfuse ADR (extends its deferred option), the pipeline
ADR. In the Langfuse ADR add only a line "Deferred option implemented by ADR-2026-10-29".

### B2. Settings: new section `telemetry`

`TelemetrySettings(_Section)` in `src/config.py`, field `Settings.telemetry`; update the
"thirteen sections" sentence in `CLAUDE.md` (→ fourteen), `.env.example`, `config.example.yaml`,
`tests/test_config.py`.

| Field | Type | Default | Bounds | Env | Meaning |
|---|---|---|---|---|---|
| `otlp_enabled` | bool | `False` | — | `AUTOBROWSER_TELEMETRY__OTLP_ENABLED` | export spans |
| `otlp_endpoint` | str | `"http://localhost:6006/v1/traces"` | non-empty | `AUTOBROWSER_TELEMETRY__OTLP_ENDPOINT` | OTLP/HTTP traces URL (Phoenix default) |
| `otlp_headers` | `dict[str, SecretStr]` | `{}` | — | `AUTOBROWSER_TELEMETRY__OTLP_HEADERS` (JSON) | e.g. `Authorization` for Langfuse |
| `otlp_timeout_seconds` | float | `10.0` | `1.0..120.0` | `AUTOBROWSER_TELEMETRY__OTLP_TIMEOUT_SECONDS` | export request timeout |
| `capture_content` | `Literal["none", "redacted"]` | `"none"` | — | `AUTOBROWSER_TELEMETRY__CAPTURE_CONTENT` | `redacted`: task text, final answers, tool args/results (already redacted and truncated by `events.max_string_chars`) become span attributes |
| `service_name` | str | `"autobrowser"` | non-empty | `AUTOBROWSER_TELEMETRY__SERVICE_NAME` | `service.name` resource attribute |

### B3. Dependency

- New optional group in `pyproject.toml`:
  `[dependency-groups] otel = ["opentelemetry-exporter-otlp-proto-http==1.39.1"]` (pinned to the
  SDK version); `uv lock`; `requirements.txt` is not changed (optional).
- Import the exporter lazily inside the sink factory. Enabled without the package → a clear
  `RuntimeError` at session start ("install with `uv sync --group otel`"), and
  `main.py doctor` shows a `telemetry` line (enabled/endpoint/exporter importable).
- CI gate installs only the default groups: tests use the SDK's `InMemorySpanExporter` (in
  `opentelemetry-sdk`), never the OTLP exporter.

### B4. `OtlpEventSink` (`src/harness/otel_sink.py`)

An `EventSink` that turns event pairs into spans with explicit timestamps
(`tracer.start_span(name, start_time=ns, ...)`, `span.end(end_time=ns)`).

| Events | Span | Name | Key attributes |
|---|---|---|---|
| `goal.started` → terminal goal event | root of a trace per task | `invoke_agent` | `gen_ai.operation.name=invoke_agent`, `gen_ai.conversation.id`=session id, `autobrowser.task.id`, `autobrowser.goal.status`, `autobrowser.stop_reason`, `error.type` (failed); status ERROR unless `completed` |
| `model.requested` → `model.responded` (same `phase`) | child | `chat {model}` | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.provider.name` = `llm.provider`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `autobrowser.model.phase`, `autobrowser.model.load_ms` |
| `tool.started` → `tool.finished` | child | `execute_tool {tool}` | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `autobrowser.tool.server`, status ERROR on an error result with `error.type` |
| `permission.decided` (not `allow`), `approval.*`, `hook.decided` (deny/ask), `policy.decided` (blocked), `plan.updated` | span events on the task span | event type | `decision`, `source`, `rule_id`, `tool` (+ `reason` only with `capture_content="redacted"`) |
| `session.started` | — | — | stores labels (A3) and session id; every span gets `session.id` and the labels as attributes |
| `session.closed` | — | — | `force_flush()` the provider (bounded by `otlp_timeout_seconds`) |

Rules:

- Pairing: model spans by `phase` (as `benchmarks.extract_task_timings` does), tool spans in
  FIFO order; an unclosed span at the terminal event ends at the terminal timestamp with
  `autobrowser.incomplete=true`.
- Content: with `capture_content="none"` no task text, answer, reason, args or results leave
  the machine — only ids, names, counts, statuses and durations. With `redacted`, values come
  from `record.to_json_dict()` (already redacted) — never from the raw payload.
- Tool and model names are data (span names/attributes), not branches: no logic keyed on them.
- Isolation: `emit` catches every exception, logs the first one per sink at WARNING and drops
  the rest; the SDK's `BatchSpanProcessor` exports in the background.
- One `TracerProvider` per sink, with `Resource({"service.name": service_name,
  "service.version": <package version>})`, built by `build_otlp_sink(settings, exporter=None)`;
  tests pass an `InMemorySpanExporter`.

Wiring: `src/harness/session.py::_build_session_event_sink` appends the sink when
`get_settings().telemetry.otlp_enabled` (session-scoped, like the other sinks). Engine and
`src/agent_loop/` never import it (`tests/test_engine_boundaries.py`: add
`src.harness.otel_sink` to a "harness-only" fence or to `PIPELINE_MODULES`).

### B5. Backends (documented, not code)

| Backend | Endpoint | Headers | Run |
|---|---|---|---|
| Arize Phoenix (local) | `http://localhost:6006/v1/traces` | — | `uvx arize-phoenix serve` or `docker run -p 6006:6006 arizephoenix/phoenix` |
| Langfuse Cloud / self-hosted ≥ 3.22 | `https://cloud.langfuse.com/api/public/otel/v1/traces` (or `<host>/api/public/otel/v1/traces`) | `Authorization: Basic base64(<public_key>:<secret_key>)` | project keys from the Langfuse UI; OTLP/HTTP only, no gRPC |
| Jaeger | `http://localhost:4318/v1/traces` | — | `docker run -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one` |

Verify the Phoenix and Langfuse endpoint paths against their current docs when writing the
guide (they were current on 2026-10-19 per the parent design §4.9.1).

### B6. Docs

- New guide section "Trace export (OTLP)" in `docs/development/testing-pipeline.md` (or a new
  `docs/development/telemetry.md` linked from `docs/README.md`): enabling, content capture and
  privacy, each backend from B5, how to find a batch scenario by `autobrowser.batch.id` /
  `autobrowser.scenario.id`.
- `docs/diagrams/`: a Mermaid diagram of events → sinks (`events.jsonl`, `agent_trace.jsonl`,
  OTLP) and the span tree; register it in `docs/diagrams/index.md` (session lifecycle and
  harness injection changed — a `CLAUDE.md` rule).

---

## 6. The Link Between A and B

| Identifier | Local (Part A) | Span attribute (Part B) |
|---|---|---|
| batch | `batch.json.batch_id`, row `batch_id` | `autobrowser.batch.id` (session label) |
| scenario | row `scenario_id` | `autobrowser.scenario.id` (session label) |
| session | row `session_id`, `traces/<name>.events.jsonl` | `session.id`, `gen_ai.conversation.id` |
| task | `task_outcomes[].task_id`, event `task_id` | `autobrowser.task.id` |
| terminal reason | `task_outcomes[].stop_reason` | `autobrowser.stop_reason` |
| commit | `batch.json.provenance.git.commit` | `vcs.ref.head.revision` (session label added by `run_batch`) |
| model | `provenance.model` | `gen_ai.request.model` |

`failure_kind` and the grade are computed after the run, so they stay local; a backend user
reaches them through the batch and scenario ids.

---

## 7. Implementation Steps

Each step is one commit with its tests. Run focused tests per step, the full suite and evals at
the end of each part.

**P0. Land the current branch.** `feat/golden-graders` (graders, 18-scenario Golden Set, batch
metrics and report, UTF-8 config fix) is unmerged: push it, open a PR, merge after the CI gate
is green. Branch Part A from the updated `main`.

**Part A (`feat/batch-analytics`):**

1. **A1 `stop_reason`** — `src/contracts.py` (`StopReason`), `execution/state.py`,
   `execution/guards.py`, `execution/loop.py`, `goals.py`; tests per value.
   Verify: `uv run pytest tests\test_engine_events.py tests\test_agent_loop_hooks.py tests\test_goal_runner.py`
   and `run_evals.py` (no baseline change).
2. **A2 `failure_kind`** — `src/agent_loop/failures.py`, `tests/test_failures.py`, fence entry.
3. **A3 contract v1** — `SessionConfig.labels` + `session.started` payload, factory signature
   change, `schema_version`, `failure_kind`, `task_outcomes`, `provenance` (script + settings
   helper); tests in `tests/test_agent_loop_batch.py`, `tests/test_run_batch_script.py`,
   `tests/test_harness_session.py` (labels in `session.started`).
4. **A4 `traces/`** — copy + text rendering, name sanitizing and collisions, report links;
   tests with a real trace file (see `test_run_batch_records_metrics_from_the_session_trace`).
5. **A5 history** — `src/agent_loop/batch_history.py`, setting `pipeline.history_max_rows`
   (+ `.env.example`, `config.example.yaml`, `tests/test_config.py`), `--no-history`; tests for
   append, rotation, load.
6. **A6 compare** — `src/agent_loop/batch_compare.py`, `scripts/compare_batches.py`, setting
   `pipeline.batch_metric_tolerance`; tests for every finding kind, exit codes, comparability
   checks, `compare.md`.
7. **A7 report and docs** — `batch_report.py` (provenance block, `Kind` column, grouping),
   guide, glossary. Then: full suite, evals, one `code-reviewer` pass over the branch, fix,
   PR.

**Part B (`feat/otlp-export`):**

1. **B1 ADR** — the new ADR and the index entry; one line in the Langfuse ADR.
2. **B2 settings** — `TelemetrySettings`, templates, `tests/test_config.py`, `CLAUDE.md`
   section count.
3. **B3 dependency** — `otel` group, `uv lock`, `doctor` line, lazy import error.
4. **B4 sink** — `src/harness/otel_sink.py`, wiring in `_build_session_event_sink`, fence;
   `tests/test_otel_sink.py` with `InMemorySpanExporter`: span tree and timings from a
   synthetic trace, usage attributes, error statuses, labels on every span,
   `capture_content="none"` exports no text attributes (assert no attribute value contains the
   task text), `redacted` exports redacted values only, an exporter exception does not
   propagate, `session.closed` flushes, disabled by default (no sink built).
5. **B5/B6 docs** — guide section, diagram + index, `docs/README.md` link. Then: full suite,
   evals, one `code-reviewer` pass, a live check (below), PR.

**Live checks (manual, not in CI):**

- Part A: run two batches of the sandbox scenarios (`books-*`, `quotes-*`) and
  `uv run scripts/compare_batches.py`; break one scenario's `expected` on purpose and confirm a
  `regression` with exit `1`; confirm a provider outage shows as `inconclusive`.
- Part B: `uvx arize-phoenix serve`, run one scenario with
  `AUTOBROWSER_TELEMETRY__OTLP_ENABLED=true`, find it in Phoenix by `autobrowser.scenario.id`,
  check the span tree, token attributes and that no page text was exported.

---

## 8. Testing Strategy

- Everything pure is tested on synthetic `EventRecord` lists (timestamps set explicitly), the
  way `tests/test_benchmarks.py` does.
- Batch-level behavior uses the fake sessions of `tests/test_agent_loop_batch.py` with a real
  `events.jsonl` written under `tmp_path`.
- Scripts are tested in process (`main(argv)`), plus one subprocess test where the environment
  matters (as `tests/test_run_evals_script.py` does).
- OTLP uses `opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter` and a
  `SimpleSpanProcessor` in tests (synchronous), `BatchSpanProcessor` in production.
- No test reads `get_settings()` for telemetry or pipeline values it asserts on: build
  `TelemetrySettings(...)` / `PipelineSettings(...)` explicitly (the personal profile must not
  leak in).

---

## 9. Risks and Open Questions

| Risk | Mitigation |
|---|---|
| `stop_reason` misses a terminal site | the per-value tests plus a test that every `goal.blocked` produced by the eval suite carries a non-empty `stop_reason` |
| `permission_denies > 0` over-attributes a failure to permissions | the rule applies only to tasks that did not complete; the per-task list keeps the raw counts for review |
| Large traces copied into every batch | traces are the main artifact; history rows stay small. Add `--no-traces` only if disk use becomes a problem |
| Exporter slows or breaks sessions | background batch processor, swallowed errors, bounded flush; disabled by default |
| Content leaking to a cloud backend | `capture_content="none"` default; tests assert no text attributes; docs warn before enabling `redacted` with a cloud endpoint |
| OTel GenAI semantic conventions are still "development" status | attribute names in one module-level mapping, so a rename touches one place |

Open questions (answer before or during implementation; defaults in brackets):

- **Q-A.** Should batch runs ignore the personal permission rules and approval classifier
  (a dedicated `tests/golden/profile.yaml` through `AUTOBROWSER_CONFIG_FILE`)? Today
  `quotes-login` and `httpbin-form-submit` cannot pass under the user's rules. [Keep the user's
  profile; `non_default_sections` makes it visible; decide after the first compare runs.]
- **Q-B.** Should `run_batch.py` retry a scenario that ended with an infra kind? [No in this
  plan; `compare_batches.py` reports it `inconclusive`.]
- **Q-C.** Langfuse scores for the grade (needs its API, not OTLP)? [Out of scope.]

---

## 10. Definition of Done

- Part A: a Golden Set run produces `schema_version` 1 rows with `failure_kind` and
  `task_outcomes`, `batch.json.provenance`, `traces/`, a history row per scenario, and
  `compare_batches.py` returns `1` for a planted regression and `0` with `inconclusive` for a
  provider outage; the guide documents all of it; full suite and evals green; reviewed; merged.
- Part B: with `otlp_enabled` a run appears in Phoenix (and, with keys, Langfuse) as one trace
  per task with model and tool spans, tokens, statuses and the batch/scenario labels; with the
  default settings nothing is exported and no backend is needed; full suite green; reviewed;
  merged.
