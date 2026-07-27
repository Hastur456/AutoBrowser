# 2026-07-27 Agent Loop Observability Branch Plan

Branch: `feat/agent-loop-observability`
Source plan: [Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md)
Status: Planned

## Goal

Make the current LangGraph loop observable, measurable, replayable, and
baseline-tested without changing agent behavior by default.

This branch implements the migration plan's Phase 1 and Phase 2 as one
observability foundation:

- typed event stream;
- durable JSONL traces;
- trace export and replay helper;
- scenario eval harness;
- first eval scenarios;
- baseline comparison for the current LangGraph loop.

## Non-Goals

Do not change these in this branch:

- model prompts;
- graph node routing;
- policy semantics;
- browser provider behavior;
- executor request/result normalization;
- memory carryover semantics;
- default user-visible CLI output.

If an observability change requires behavior changes to pass tests, split that
behavior change into a later branch.

## Current Integration Points

The branch should wrap the existing runtime boundaries:

| Area | Current file | Use in this branch |
| --- | --- | --- |
| Session lifecycle | `src/harness/session.py` | Emit session and goal/task lifecycle events, provide session trace directory. |
| Graph task execution | `src/harness/runtime.py` | Emit graph invocation, graph node, terminal, and error events around `run()` and `stream_updates()`. |
| CLI stream adapter | `src/cli/task_runner.py` | Keep existing printing, optionally route streamed updates through events. |
| Existing telemetry | `src/harness/telemetry.py` | Keep as logging boundary; do not overload it with durable trace ownership. |
| Browser scenarios | `src/browser/fake.py` | Use fake browser provider for deterministic scenario evals. |
| Existing graph tests | `tests/test_agent_graph.py` | Reuse current graph fixtures as the first baseline behavior source. |
| Harness tests | `tests/test_harness_runtime.py`, `tests/test_harness_session.py` | Extend with event ordering, JSONL persistence, and failure traces. |

## Proposed Files

New source files:

- `src/agent_loop/__init__.py`
- `src/agent_loop/events.py`
- `src/agent_loop/tracing.py`
- `src/agent_loop/replay.py`
- `src/agent_loop/evals.py`

New test files:

- `tests/test_agent_loop_events.py`
- `tests/test_agent_loop_tracing.py`
- `tests/test_agent_loop_replay.py`
- `tests/test_agent_loop_evals.py`
- `tests/evals/runner.py`
- `tests/evals/scenarios/search_basic.yaml`
- `tests/evals/scenarios/stale_ref_recovery.yaml`
- `tests/evals/scenarios/unchanged_search_click.yaml`
- `tests/evals/scenarios/dynamic_search_input.yaml`
- `tests/evals/scenarios/filter_requires_apply.yaml`

Optional script entry points:

- `scripts/export_trace.py`
- `scripts/replay_trace.py`
- `scripts/run_evals.py`

Prefer small Python scripts over new CLI flags unless a workflow must be part
of the production CLI.

## Event Model

Add typed records in `src/agent_loop/events.py`.

Minimum shape:

```python
EventRecord
  event_id: str
  type: EventType
  timestamp: datetime
  session_id: str | None
  goal_id: str | None
  task_id: str | None
  parent_id: str | None
  sequence: int
  source: str
  payload: dict[str, Any]
```

Initial event types:

- `session.started`
- `session.closed`
- `goal.started`
- `graph.started`
- `graph.node_started`
- `graph.node_finished`
- `model.requested`
- `model.responded`
- `action.proposed`
- `policy.decided`
- `approval.requested`
- `tool.started`
- `tool.finished`
- `observation.compiled`
- `goal.completed`
- `goal.blocked`
- `goal.failed`
- `goal.cancelled`

Use `goal.*` names externally even though the current code still calls each
user request a task. Store the current `TaskRecord.task_id` as `task_id` and use
the same value as `goal_id` until an explicit goal model exists.

## Event Sinks

Implement sinks in `src/agent_loop/events.py` or `src/agent_loop/tracing.py`:

- `EventSink` protocol with `emit(record: EventRecord) -> None`;
- `InMemoryEventSink` for tests;
- `JsonlEventSink` for durable traces;
- `CompositeEventSink` for fan-out;
- `NullEventSink` for rollback and disabled tracing.

JSONL path:

```text
.autobrowser/sessions/<session_id>/events.jsonl
```

Persistence rules:

- one JSON object per line;
- append-only during a session;
- flush after each event;
- write JSON-serializable payloads only;
- redact secrets before persistence;
- keep payloads compact enough for replay and eval diagnostics.

## Redaction And JSON Safety

Move reusable JSON-safe conversion out of `src/harness/session.py` only if it
stays purely additive and does not churn existing persistence behavior.
Otherwise duplicate a small private helper in tracing for this branch and
deduplicate later.

Required redaction coverage:

- keys containing `token`, `secret`, `password`, `credential`, `api_key`, or
  `authorization`;
- long tool outputs should be capped or summarized in event payloads;
- raw snapshots may be persisted, but eval trace summaries should have compact
  action sequences by default.

Tests must prove redacted JSONL can be loaded after process exit.

## Instrumentation Plan

### 1. SessionRuntime

In `SessionContext.initialize()`:

- create the session directory before the JSONL sink is opened;
- initialize an event sink owned by the session context;
- emit `session.started` to the typed stream after session metadata is ready.

In `SessionRuntime.run_task()`:

- emit `goal.started` after `reset_task()`;
- pass trace metadata into `BrowserHarness` through config or explicit
  arguments;
- emit `goal.completed` after `finish_task()`;
- emit `goal.failed` before re-raising exceptions;
- keep the existing `SessionEventBus` in place for compatibility.

### 2. BrowserHarness

In `BrowserHarness.run()`:

- emit `graph.started` before `graph.ainvoke()`;
- emit terminal `goal.completed` evidence only through session runtime, not
  directly, to avoid duplicate terminal ownership;
- emit `goal.failed` details only when the exception escapes the harness.

In `BrowserHarness.stream_updates()`:

- emit `graph.started`;
- for each streamed chunk, emit `graph.node_started` and
  `graph.node_finished` events derived from chunk keys;
- derive node-specific events from state updates when available:
  - `agent` update with `decision=tool_call` -> `action.proposed`;
  - `policy` update with `policy_event` -> `policy.decided`;
  - `executor` update with `tool_result` -> `tool.finished`;
  - `observe` update with `observation` or `snapshot` -> `observation.compiled`;
  - `agent` update with `decision=done` -> completion evidence.

Do not require `show_state=True` to create traces. Non-streaming `run()` should
still produce lifecycle and terminal events; streaming mode should produce
node-level detail. If node-level detail for non-streaming mode is not available
without changing execution mode, document that as a v1 trace limitation.

### 3. CLI Task Runner

Keep `src/cli/task_runner.py` output stable:

- current `--show-state` printing stays based on graph chunks;
- trace writing happens below the CLI layer;
- helper scripts read JSONL traces after the run instead of changing normal CLI
  output.

## Trace Export And Replay

Add replay helpers that consume JSONL, not live graph state.

`src/agent_loop/replay.py` should expose:

- `load_events(path: Path) -> list[EventRecord]`;
- `iter_action_sequence(events) -> Iterator[TraceAction]`;
- `summarize_trace(events) -> TraceSummary`;
- `print_action_sequence(events) -> str`.

`scripts/replay_trace.py` should support:

```powershell
python scripts\replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

Minimum printed output:

```text
goal.started: inspect page
1. browser_snapshot {}
2. browser_type {"ref": "e8", "text": "jackets"}
3. browser_click {"ref": "e9"}
goal.completed: Found ...
```

`scripts/export_trace.py` should support compact JSON export for eval fixtures:

```powershell
python scripts\export_trace.py .autobrowser\sessions\<session_id>\events.jsonl --format compact-json
```

## Scenario Eval Harness

Add a pytest-friendly harness before adding standalone commands.

Scenario schema:

```yaml
name: search_basic
task: Search for jackets and report the first visible result.
model:
  responses:
    - '{"steps":[...]}'
    - '{"decision":"tool_call","tool_request":{...}}'
browser:
  snapshots:
    - '- textbox "Search" ref=e8'
    - '- link "Jacket A" ref=e10'
assertions:
  expected_terminal_status: completed
  final_answer_contains:
    - Jacket
  max_tool_calls: 5
  forbidden_repeated_actions:
    - name: browser_click
      args:
        ref: e7
      max_consecutive: 1
```

Metrics:

- terminal status;
- final answer present;
- model turn count;
- tool call count;
- repeated action count;
- policy block count;
- invalid-ref recovery count;
- trace completeness.

Runner responsibilities:

- build `BrowserHarness` with `FakeListLLM` or scripted fake model;
- register `FakeBrowserProvider` when browser steps are needed;
- run the current LangGraph loop only;
- capture the typed event stream through `InMemoryEventSink`;
- optionally write a JSONL trace fixture;
- return `EvalResult` with metrics and compact action sequence.

## First Scenarios

### `search_basic.yaml`

Purpose: prove a normal search path emits complete traces and completes.

Expected flow:

```text
plan -> browser.snapshot -> browser.type -> browser.snapshot -> done
```

Assertions:

- terminal status is `completed`;
- final answer contains the visible result name;
- trace has `goal.started`, at least one `tool.finished`, and `goal.completed`;
- no repeated identical tool call above 1.

### `stale_ref_recovery.yaml`

Purpose: baseline current invalid-ref recovery behavior.

Expected flow:

```text
browser.click stale ref -> tool error invalid_ref -> fresh browser.snapshot -> replan or valid click
```

Assertions:

- invalid-ref error appears in trace;
- fresh snapshot appears after invalid ref;
- terminal status is `completed` or documented `blocked` baseline;
- repeated stale click does not exceed the current guard threshold.

### `unchanged_search_click.yaml`

Purpose: protect against the known search-affordance loop.

Assertions:

- repeated same search-button click is counted;
- policy or agent guard prevents unlimited repeats;
- compact failure output includes action sequence if the baseline currently
  fails.

### `dynamic_search_input.yaml`

Purpose: cover pages where search input appears only after one interaction.

Assertions:

- one search affordance click is allowed;
- subsequent visible textbox is typed into directly;
- final answer is produced.

### `filter_requires_apply.yaml`

Purpose: cover filter UIs where typing does not apply until a visible apply
control is clicked.

Assertions:

- typing filter value and applying filter are distinct actions;
- repeated type into unchanged input is detected;
- final answer references filtered result or baseline failure is recorded.

## Baseline Comparison

Create a baseline command that compares eval results from the current
LangGraph loop against stored expected metrics.

Initial baseline file:

```text
tests/evals/baselines/langgraph_v1.json
```

Suggested command:

```powershell
python -m pytest tests\test_agent_loop_evals.py
```

Optional script:

```powershell
python scripts\run_evals.py --engine langgraph-v1 --baseline tests\evals\baselines\langgraph_v1.json
```

Baseline policy:

- the first branch may record known failures, but it must make them explicit;
- future branches must not silently increase tool calls, repeats, or missing
  terminal events;
- failed scenarios should print compact action sequences and trace path.

## Implementation Slices

### Slice 1: Event Core

Files:

- `src/agent_loop/events.py`
- `tests/test_agent_loop_events.py`

Deliverables:

- event type definitions;
- `EventRecord`;
- JSON-safe serialization;
- redaction helper;
- in-memory, JSONL, composite, and null sinks.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_events.py
```

### Slice 2: Session And Harness Wiring

Files:

- `src/harness/session.py`
- `src/harness/runtime.py`
- `tests/test_harness_session.py`
- `tests/test_harness_runtime.py`

Deliverables:

- session-owned sink under `.autobrowser/sessions/<session_id>/events.jsonl`;
- lifecycle events from session runtime;
- graph and node events from streaming execution;
- failure events for graph errors and task errors.

Exit gate:

```powershell
python -m pytest tests\test_harness_session.py tests\test_harness_runtime.py
```

### Slice 3: Derived Agent Events

Files:

- `src/agent_loop/tracing.py`
- `src/harness/runtime.py`
- `tests/test_agent_loop_tracing.py`

Deliverables:

- functions that convert graph chunks into typed events;
- action, policy, tool, and observation event derivation;
- tests for chunk-to-event mapping using fake graph chunks.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_tracing.py tests\test_harness_runtime.py
```

### Slice 4: Trace Replay Helpers

Files:

- `src/agent_loop/replay.py`
- `scripts/replay_trace.py`
- `scripts/export_trace.py`
- `tests/test_agent_loop_replay.py`

Deliverables:

- load JSONL events;
- compact action sequence;
- trace summary;
- script-level smoke tests or direct module tests.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_replay.py
```

### Slice 5: Eval Harness

Files:

- `src/agent_loop/evals.py`
- `tests/evals/runner.py`
- `tests/test_agent_loop_evals.py`

Deliverables:

- scenario dataclasses or typed dictionaries;
- YAML loader;
- fake-model/fake-browser graph runner;
- metrics collection from event traces;
- failure output with compact action sequence.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_evals.py
```

### Slice 6: First Scenarios And Baseline

Files:

- `tests/evals/scenarios/*.yaml`
- `tests/evals/baselines/langgraph_v1.json`
- `tests/test_agent_loop_evals.py`

Deliverables:

- five baseline scenarios;
- stored current-loop metrics;
- test that compares current metrics to baseline tolerances.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_evals.py
```

### Slice 7: Full Branch Verification

Commands:

```powershell
python -m pytest tests\test_agent_loop_events.py tests\test_agent_loop_tracing.py tests\test_agent_loop_replay.py tests\test_agent_loop_evals.py
python -m pytest tests\test_harness_session.py tests\test_harness_runtime.py tests\test_agent_graph.py
python -m pytest
```

Deliverables:

- no default behavior changes;
- current CLI output remains compatible;
- JSONL trace can be replayed after process exit;
- baseline eval results are explicit and reproducible.

## Feature Flag

Use a config name that aligns with the migration plan:

```text
AUTOBROWSER_AGENT_LOOP=v1
```

For this branch, `v1` remains the only engine. The flag can be parsed or only
documented, but it must not introduce a `v2` execution path yet.

If event tracing needs a separate operational switch, prefer:

```text
AUTOBROWSER_TRACE_EVENTS=1
```

Default recommendation for this branch: JSONL tracing is enabled for local
sessions because it writes into `.autobrowser/`, which is already runtime-local.
Use `NullEventSink` in tests or explicit config where traces should be disabled.

## Acceptance Criteria

The branch is complete when:

- every task has `goal.started` and exactly one terminal goal event;
- streamed runs include graph node events;
- policy, tool, and observation updates are represented when the graph exposes
  them;
- `.autobrowser/sessions/<session_id>/events.jsonl` is valid JSONL;
- persisted events are redacted and JSON-serializable;
- trace replay prints a compact action sequence;
- at least five scenario evals exist;
- current LangGraph loop has a stored baseline;
- full test suite passes;
- prompts, policy, graph routing, and browser provider behavior are unchanged.

## Rollback

Rollback should not require reverting behavior:

- replace active sink with `NullEventSink`;
- leave `SessionEventBus` unchanged;
- keep `BrowserHarness.run()` and `stream_updates()` return values identical;
- remove eval baseline enforcement from CI if it proves flaky, but keep the
  harness and scenarios for local diagnostics.

## Follow-Up Branches

After this branch:

1. `feat/context-assembler-prompt-split`
2. `feat/proposed-action-contract`
3. `feat/tool-broker-permissions`
4. `feat/agent-loop-hooks`
5. `feat/agent-loop-skills`

Do not start those until this branch gives reliable traces and scenario
baseline data.
