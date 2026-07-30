# 2026-07-30 Batch And Export Data Contracts

Status: Draft for implementation

This note defines the first stable data contracts for batch runs, feedback, and
session exports. It is intentionally read-only with respect to the current
runtime: the exporter must derive rows from files that AutoBrowser already
writes, while the batch runner adds only batch-owned metadata.

## Current Observability Sources

AutoBrowser already persists session and trace data under:

```text
.autobrowser/sessions/<session_id>/
```

### `session.json`

Owned by `SessionContext.persist()` in `src/harness/session.py`.

Available fields:

- `session_id`: stable session identifier.
- `initialized`: whether process-scoped resources were active at last persist.
- `current_task`: current task text while a task is running, otherwise null.
- `config`: serialized `SessionConfig`, including model, MCP, CDP, recursion,
  and display options.
- `metadata`: `started_at`, `last_activity`, `task_count`, and optional
  `runtime_version`.
- `workspace`: session-local workspace paths.
- `artifacts`: registered session artifacts.
- `tasks`: embedded copy of task records.

Exporter use:

- session-level metadata;
- runtime and model configuration;
- fallback task list if `tasks.json` is missing.

### `tasks.json`

Owned by `SessionContext.persist()` in `src/harness/session.py`.

Each task record currently contains:

- `task_id`: stable task identifier such as `task-...`.
- `task`: user task text.
- `started_at`: ISO timestamp.
- `finished_at`: ISO timestamp or null.
- `result`: JSON-safe task result or serialized exception object.

Exporter use:

- task text and timestamps;
- duration fallback when terminal events are incomplete;
- task result fallback for `final_answer` and failure details.

### `events.jsonl`

Owned by `JsonlEventSink` in `src/agent_loop/events.py`.

Each line is an `EventRecord` JSON object:

```json
{
  "event_id": "event-...",
  "type": "goal.completed",
  "timestamp": "2026-07-30T00:00:00+00:00",
  "session_id": "session-id",
  "goal_id": "task-id",
  "task_id": "task-id",
  "parent_id": null,
  "sequence": 42,
  "source": "harness.session",
  "payload": {}
}
```

Current event types include session lifecycle, graph lifecycle, model response,
action, policy, approval, tool, observation, and terminal goal events. Payloads
are redacted and JSON-safe before persistence.

Exporter use:

- primary source for derived metrics;
- grouping by `task_id`;
- terminal status from the last terminal `goal.*` event;
- final answer from `goal.completed.payload.result`;
- failure details from `goal.failed.payload.error`;
- duration from first and last task event timestamps when available.

### `agent_trace.jsonl`

Owned by `AgentTraceSink` in `src/agent_loop/events.py`.

This is a compact projection of selected `events.jsonl` records. It keeps
human-readable action, policy, tool, observation, and terminal summaries, with
long text capped for diagnostics.

Exporter use:

- diagnostic sidecar only;
- not the source of truth for metrics, because it omits IDs such as
  `session_id`, `task_id`, `goal_id`, and `sequence`.

### `scripts/run_evals.py` And `tests/evals`

`scripts/run_evals.py` runs deterministic scenario fixtures from
`tests/evals/scenarios/*.yaml` through the current LangGraph loop with fake
model and fake browser providers. Results are compared with
`tests/evals/baselines/langgraph_v1.json`.

These evals are not real batch runs:

- eval scenario YAML uses fake model responses and fake browser snapshots;
- eval output is a baseline metrics comparison for tests;
- eval sessions are in-memory and do not define production batch metadata;
- real batch runs execute user-authored JSONL tasks through `SessionRuntime`.

## Data Contracts

All contracts are newline-delimited JSON unless noted otherwise. Unknown fields
must be preserved by readers where practical and ignored by current logic.

### Batch Task

Input file consumed by the batch runner.

Required fields:

- `task`: user task text.

Optional fields:

- `scenario_id`: caller-owned scenario identifier.
- `batch_item_id`: caller-owned row identifier. If omitted, the runner creates
  a stable item ID for the run index.
- `metadata`: arbitrary JSON object copied to the run index.

Example:

```json
{
  "scenario_id": "search-basic",
  "batch_item_id": "item-001",
  "task": "Search for jackets and report the first visible result.",
  "metadata": {
    "suite": "smoke"
  }
}
```

### Batch Run Index

Output file written after each attempted task:

```text
.autobrowser/batches/<batch_id>/run_index.jsonl
```

Required fields:

- `batch_id`: batch run identifier.
- `batch_item_id`: input row identifier or generated item ID.
- `session_id`: AutoBrowser session identifier used for the run.
- `task_id`: `TaskRecord.task_id` for the executed task when available.
- `task`: user task text.
- `status`: `completed`, `failed`, or `skipped`.
- `started_at`: ISO timestamp.
- `finished_at`: ISO timestamp.

Optional fields:

- `scenario_id`: copied from the batch task.
- `metadata`: copied from the batch task.
- `error`: JSON object with `type` and `message` for failed runner attempts.

Example:

```json
{
  "batch_id": "batch-20260730-001",
  "batch_item_id": "item-001",
  "scenario_id": "search-basic",
  "session_id": "7ad0...",
  "task_id": "task-...",
  "task": "Search for jackets and report the first visible result.",
  "status": "completed",
  "started_at": "2026-07-30T00:00:00+00:00",
  "finished_at": "2026-07-30T00:00:15+00:00",
  "metadata": {
    "suite": "smoke"
  }
}
```

### Feedback Record

Input file joined by the exporter:

```text
.autobrowser/feedback.jsonl
```

Required fields:

- `session_id`: AutoBrowser session identifier.
- `task_id`: task identifier.
- `rating`: caller-defined numeric or string rating.

Optional fields:

- `feedback_id`: caller-owned feedback record identifier.
- `created_at`: ISO timestamp.
- `label`: short categorical outcome such as `pass`, `fail`, or `needs_review`.
- `comment`: free-form reviewer note.
- `metadata`: arbitrary JSON object.

If multiple feedback records exist for the same `session_id` and `task_id`, the
exporter should attach the last record in file order and may expose the count in
a later contract version.

Example:

```json
{
  "feedback_id": "fb-001",
  "session_id": "7ad0...",
  "task_id": "task-...",
  "rating": 1,
  "label": "pass",
  "comment": "Correct first result.",
  "created_at": "2026-07-30T00:05:00+00:00"
}
```

### Metrics

Derived from one task's `events.jsonl` records by a pure extractor.

Fields:

- `duration_ms`: integer duration from first to last task event timestamp, or
  null when timestamps are unavailable.
- `terminal_status`: `completed`, `failed`, `blocked`, `cancelled`, or
  `missing`.
- `model_turn_count`: count of `model.responded`.
- `tool_call_count`: count of `tool.finished`.
- `policy_block_count`: count of `policy.decided` with blocked decision.
- `approval_request_count`: count of `approval.requested`.
- `observation_count`: count of `observation.compiled`.
- `error_count`: count of failed graph events, failed terminal events, and tool
  results carrying an error.
- `final_answer`: final answer text when present, otherwise empty string or
  null.

Example:

```json
{
  "duration_ms": 15000,
  "terminal_status": "completed",
  "model_turn_count": 3,
  "tool_call_count": 2,
  "policy_block_count": 0,
  "approval_request_count": 0,
  "observation_count": 2,
  "error_count": 0,
  "final_answer": "The first visible result is Jacket A."
}
```

### Export Row

Output file produced by `scripts/export_sessions.py`.

Required fields:

- `session_id`
- `task_id`
- `task`
- `started_at`
- `finished_at`
- `metrics`
- `feedback`

Optional fields:

- `batch_id`
- `batch_item_id`
- `scenario_id`
- `batch_metadata`
- `session_config`
- `session_metadata`
- `result`
- `error`

`feedback` is either the joined feedback record or null.

Example:

```json
{
  "session_id": "7ad0...",
  "task_id": "task-...",
  "batch_id": "batch-20260730-001",
  "batch_item_id": "item-001",
  "scenario_id": "search-basic",
  "task": "Search for jackets and report the first visible result.",
  "started_at": "2026-07-30T00:00:00+00:00",
  "finished_at": "2026-07-30T00:00:15+00:00",
  "metrics": {
    "duration_ms": 15000,
    "terminal_status": "completed",
    "model_turn_count": 3,
    "tool_call_count": 2,
    "policy_block_count": 0,
    "approval_request_count": 0,
    "observation_count": 2,
    "error_count": 0,
    "final_answer": "The first visible result is Jacket A."
  },
  "feedback": null,
  "session_config": {
    "model": "..."
  },
  "session_metadata": {
    "task_count": 1
  }
}
```

## Implementation Notes

- The exporter core must be read-only and tolerate missing `tasks.json`,
  missing `events.jsonl`, empty JSONL files, and missing feedback.
- Metrics extraction should accept already-loaded event dictionaries or
  `EventRecord` objects for one task and should not read files directly.
- Batch metadata is joined by scanning
  `.autobrowser/batches/*/run_index.jsonl` for matching `session_id` and
  `task_id`.
- The batch runner owns `.autobrowser/batches/<batch_id>/`; session runtime
  continues to own `.autobrowser/sessions/<session_id>/`.
- The contracts above are separate from `tests/evals`, which remain
  deterministic regression fixtures rather than production batch artifacts.
