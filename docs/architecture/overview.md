# Architecture Overview

AutoBrowser is a Python 3.12 browser automation agent built around LangGraph.
The command-line entry point is `main.py`; the core agent graph lives under
`src/agent/`; session and runtime infrastructure are managed from
`src/harness/`.

## Goals

AutoBrowser turns a natural-language browser task into a controlled loop:

1. Plan a small set of task steps.
2. Select the next browser or non-browser tool action.
3. Apply policy checks before execution.
4. Execute the selected tool.
5. Observe the result and update compact state.
6. Repeat until a final answer is available.

The browser agent is snapshot-driven. It should use Playwright MCP refs from
`browser_snapshot`, not CSS selectors, XPath, class names, or assumed DOM
structure.

## Source Layout

| Path | Responsibility |
| --- | --- |
| `main.py` | CLI parsing and wiring the process into `SessionRuntime`. |
| `src/agent/` | LangGraph graph assembly, shared state, prompts, reasoning node, routers. |
| `src/agent/subgraphs/planner/` | One-shot planning graph and planning prompts. |
| `src/agent/subgraphs/executor/` | Tool execution graph and Playwright MCP argument normalization. |
| `src/agent/subgraphs/observer/` | Tool-result observation, snapshot handling, compact result summaries. |
| `src/harness/` | Session runtime, graph harness, context, memory, tools, policy, and telemetry boundaries. |
| `src/mcp/` | MCP session and Playwright MCP integration helpers. |
| `tests/` | Pytest coverage for graph behavior, harness boundaries, CLI, prompts, and tools. |
| `scripts/` | Utility scripts, including graph visualization helpers. |

## Graph Shape

The compiled graph is assembled by `build_agent_graph` in `src/agent/agent.py`.
Its verified node flow is:

```text
START -> plan -> agent -> policy -> executor -> observe -> agent
```

The `agent` node can also route back to `plan` for replanning or to `END` when
the task is done. `policy` can route to `human_input` when a tool needs human
approval. After approved execution, `executor` always routes into `observe`,
and `observe` routes back to `agent`.

## Runtime Boundary

`SessionRuntime` in `src/harness/session.py` owns the long-lived application
lifecycle and delegates session-owned state to `SessionContext`. The context is
the root object for one process session: it holds `SessionConfig`, `session_id`,
task history, current task, workspace, artifact registry, event bus, session
state, metadata, telemetry, tool registry, memory, LLM, and `BrowserHarness`.
Each user request is tracked as a `TaskRecord` and delegated as an independent
task. When the agent reaches a terminal state, only that task ends; the session
returns to the input prompt and keeps the same runtime resources alive until the
process exits.

The session layer is intentionally not a task solver. It manages interaction
lifecycle and resource ownership, while `BrowserHarness` and the compiled
LangGraph agent continue to handle one task execution at a time. AutoBrowser
models session activity as tasks, not chat turns.

Session workspace files live under
`.autobrowser/sessions/<session_id>/workspace/`, with standard subdirectories
for `downloads/`, `screenshots/`, `temp/`, and `artifacts/`. This directory is
runtime-local and ignored by git.

Session metadata is persisted next to the workspace under
`.autobrowser/sessions/<session_id>/`. `session.json` records the current
session view, including config, metadata, workspace paths, artifacts, and task
history. `tasks.json` stores the task records directly for simpler inspection.
These files are runtime artifacts and are ignored by git.

`BrowserHarness` in `src/harness/runtime.py` is the composition root for runtime
infrastructure used by one task execution. It receives a graph builder and
injects:

- `ContextBuilder`: builds initial graph state and owns system prompt injection.
- `MemoryManager`: owns checkpoint saver and durable message history helpers.
- `ToolRegistry`: lazily loads static tools, generic providers, and MCP clients.
- `PolicyEngine`: classifies tool requests before execution.
- `TelemetryObserver`: logs local trace metadata and errors.

This keeps the graph focused on reasoning/control flow, keeps task lifecycle
separate from session lifecycle, and keeps runtime concerns replaceable in
tests.

Each task receives a generated task-specific `thread_id` stored on its
`TaskRecord` and passed into LangGraph config. After a task succeeds or fails,
`SessionRuntime` asks `MemoryManager` to delete checkpoints for that thread when
the configured saver supports thread deletion. This keeps the process-long
session alive without accumulating graph checkpoints or durable message history
from completed tasks inside the active in-memory saver.

## Planner

The planner is intentionally compact. It should create practical plans rather
than long procedural scripts. Current prompt constraints prefer one to three
steps and explicitly avoid splitting a search task into separate locate,
inspect, type, and submit microsteps unless fallback handling is needed.

For search tasks, the plan must preserve this contract:

- locate an editable search input;
- inspect whether it already contains the requested query;
- type or replace the query only if needed;
- submit the search;
- verify and extract visible results.

## Agent Reasoning

The reasoning node uses the current task, plan, latest observation, latest
snapshot, available refs, retry counters, and durable message history. It must
return either a native tool call, a JSON replan decision, or a JSON done
decision.

The system prompt emphasizes:

- the fewest useful browser actions;
- no fresh `browser_snapshot` after every successful action;
- direct use of editable `textbox`/`searchbox` refs for `browser_type`;
- early fallback to direct search URLs when repeated search affordance clicks
  do not expose an input;
- immediate completion once extracted data satisfies the user request.

## Executor

The executor resolves the requested tool through `ToolRegistry`. For browser
tools, it adapts ref-based requests to the loaded Playwright MCP tool schema:

- maps `ref` to `target` when the tool expects `target`;
- maps `target` back to `ref` when the tool expects `ref`;
- fills an `element` argument from the latest snapshot line when required;
- removes unsupported extra arguments when the tool schema disallows them.

Tool success and failure are normalized into `ToolResult` state.

## Observer

The observer translates a single tool result into compact graph state. It stores
successful snapshots as the current source of truth, clears stale snapshots
after browser actions, tracks invalid-ref recovery, and detects ineffective
browser actions by comparing snapshot fingerprints.

When compression is enabled, an observer LLM can summarize tool output, but it
must use only the latest `ToolResult` JSON.

## Policy

`PolicyEngine` currently blocks missing tool requests, routes sensitive tool
names containing markers such as `payment`, `purchase`, `delete_account`, or
`credential` to human approval, and blocks redundant identical
`browser_snapshot` requests when the current snapshot is still usable.

## Browser Semantics

The project follows Playwright MCP semantics:

- `browser_snapshot` is the source of truth.
- Element identity is the snapshot ref, such as `ref=e123`.
- Refs are ephemeral and valid only for the snapshot that produced them.
- Preferred interactions are `browser_click(ref)`, `browser_type(ref)`, and
  `browser_hover(ref)`.
- If a snapshot does not expose the required control, the agent should request
  a fresh/deeper snapshot or use `browser_evaluate` only when snapshots cannot
  answer the question.

## Known Operational Risks

- Dynamic commerce pages can expose a search button before the editable input.
  Prompt rules now limit repeated search-button clicks and allow direct search
  URL fallback, especially for Ozon.
- Recursion limits can be reached when the agent repeats tool calls without
  progress. Retry counters, observer hints, policy checks, and prompt rules are
  the current controls.
- Tool-output compression must preserve enough snapshot/ref detail for safe
  follow-up actions.
