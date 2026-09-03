# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` is the exhaustive contributor guide (structure, style, full command list, testing rules, browser rules). `docs/` holds the authoritative deep references (architecture, ADRs, diagrams, migration plans). This file is the fast orientation layer plus repo-specific working rules; when they overlap, this file wins.

## CodeGraph First Policy

Tool budget:

- Maximum 2 codegraph_search calls
- Maximum 1 codegraph_files call
- Maximum 1 file read

After finding the file:
implement immediately.

## PowerShell UTF-8 Reading

When reading files that may contain Russian text in PowerShell, set the console output encoding explicitly and read as UTF-8 to avoid mojibake:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -LiteralPath path\to\file.md -Encoding UTF8
```

## What This Is

AutoBrowser is a Python 3.12 browser automation agent that turns a
natural-language task into a plan → reason → policy → execute → observe loop.
Browser interaction is **snapshot-driven** via Playwright MCP (element `ref`s),
not CSS/XPath. It runs as a long-lived interactive `cmd2` REPL (`main.py`) over
an Ollama-compatible chat model (default `gpt-oss:20b-cloud`). Control flow is
**engine-native** — there is no compiled graph. The explicit `AgentLoopEngine`
owns the loop; see `docs/decisions/2026-08-31-native-agent-loop-engine.md` for
the ADR that made it the sole runtime.

## The Engine-Native Loop

Control flow lives in `src/agent_loop/execution/`, not in a compiled graph:

- `loop.py` — `AgentLoopEngine` (builds the initial plan, then drives a bounded
  `while` loop of `TurnController` turns and returns a terminal `AgentLoopResult`)
  and `native_task_runner` (composes `EngineResources`).
- `state.py` — the frozen `LoopState` dataclass. `LoopState.apply()` is STRICT —
  it raises `ValueError` on unknown keys. `LoopState()` constructs with all
  defaults; `to_session_state()` yields the `SESSION_STATE_KEYS` dict carried
  across tasks.
- `completion.py` — `native_latest_state_loader` (unwraps the
  `AgentLoopResult.session_state` carry-forward for the next task).
- `guards.py`, `policy.py`, `observation.py`, `tools.py`, `resources.py` — the
  loop guards (including `CompletionController`), policy fns, observation
  compiler, tool broker, and `EngineResources` bundling.

`AgentLoopResult(status, final_answer, session_state, state, turns=0)` is a frozen
dataclass exported from `src.agent_loop.execution.loop.__all__`. `status` is always
terminal (`"done"`/`"blocked"`/`"cancelled"`).

The old `src/agent/` compiled-graph runtime is **deleted**, and the transitional
`src/agent_loop/outcomes.py` is **deleted** too — its `GoalState`,
`ObservationCompiler`, and completion guards were removed once `GoalRunner`
started consuming the terminal `AgentLoopResult` directly. The legacy
`src/agent_loop/adapters/` bridge and `src/cli/task_runner.py` are gone. Neutral typed
contracts live in `src/contracts.py` (imports nothing from `src/agent_loop/`,
`src/harness/`, or `src/browser/`), including the `CompletionStatus`/`GoalStatus`
literals and `goal_status_from_completion()`; `AgentState`/`BrowserState` remain as
type-only TypedDicts in `src/state.py` for annotation; prompts are consolidated in
`src/agent_loop/prompts.py`; model defaults are in `src/llm.py`.

Model access runs over the
provider-neutral `ChatModel` contract in `src/llm.py`, implemented by thin adapters in
`src/providers/` (e.g. `ollama.py`). Tools are neutral `Tool`/`ToolDef` objects in
`src/contracts.py`. Conversation history is a provider-neutral `list[Message]`
(`src/messages.py`) carried on `LoopState.messages` — shaped by the functional helpers in
`src/harness/memory.py`, never by a checkpoint saver.

## Ownership Chain & Layering Rules

```text
SessionRuntime -> GoalRunner -> native_task_runner -> AgentLoopEngine -> TurnController
```

Each layer is hard-fenced; **respect the boundary the code is trying to keep**:

- `src/harness/session.py` — `SessionRuntime`/`SessionContext`: process & session lifecycle,
  MCP/browser resource lifetime, task history, context handoff. **Not a task solver.**
- `src/agent_loop/goals.py` — `GoalRunner`: one-task lifecycle + goal events only. Must not
  choose actions, judge completion, touch routing/counters/policy, or run a model loop.
- `src/harness/runtime.py` — `BrowserHarness`: per-task composition root. Injects
  `ContextAssembler`, `ToolRegistry`, `PolicyEngine`, `TelemetryObserver`, `EventEmitter`
  and holds `EngineResources.from_harness` sources. It does not own memory; history shaping
  is a functional helper set in `src/harness/memory.py` and the durable list lives on
  `LoopState.messages`. There is no graph to stream and no recursion-limit recovery here
  anymore.
- `src/agent_loop/execution/` — the engine owns **only** reasoning, routing, execution,
  observation. Infrastructure goes in `src/harness/`; browser schema adaptation goes in
  `src/browser/`.

Do not hardcode Playwright MCP behavior into the agent loop, and do not put browser schema
adaptation in the engine or prompts — register it through `BrowserProvider`/`ToolRegistry`.

## The Engine Loop

`AgentLoopEngine.run` (in `src/agent_loop/execution/loop.py`):

```text
build initial plan (model call #0) -> while turn <= cap:
  TurnController.run_turn(LoopState) ->
    agent step -> decision
      done    -> terminal status via CompletionController
      replan  -> rebuild plan
      tool_call -> policy -> (human_input?) -> ToolBroker.execute -> observe
```

`policy` routes to `human_input` for sensitive tools; a blocked or denied tool
short-circuits back to the loop. `DEFAULT_TURN_CAP = 50` bounds the loop.

## Browser Semantics (Hard Invariant)

The agent is **snapshot-driven, not selector-driven** (see `docs/development/browser-agent-rules.md`):

- `browser_snapshot` is the only source of truth; element identity is an ephemeral `ref=e123`
  valid **only** for the snapshot that produced it. Never guess CSS/XPath/class/DOM.
- Ref-based `click`/`type`/`hover` require a current snapshot. If the ref is absent from the
  latest snapshot, **replan from visible refs** — never reuse a historical ref.
- Don't snapshot after every action; don't re-click a search affordance after an unchanged
  snapshot; prefer typing into an editable control, then fall back to a direct search URL
  (e.g. Ozon `https://www.ozon.ru/search/?text=<query>`).

These rules are **duplicated across prompts, `PolicyEngine`, the observer, and provider
tests**. Changing one layer can reintroduce stale-ref/loop bugs — keep them aligned, and
don't remove an invariant from a prompt unless policy/observer/evals still enforce it.
`FakeBrowserProvider` (`src/browser/fake.py`) exercises this behavior deterministically
without Chrome/CDP/MCP.

## Session vs Task Boundary

All tasks in one REPL session share one session identity (from
`SessionContext.session_id`), passed into each task config as
`configurable.thread_id`. Across tasks the runtime carries forward only durable context
(messages, latest observation, current snapshot, browser state, last-action metadata) and
**resets task-local fields** (`plan`, `decision`, `final_answer`, tool request/result,
policy state, errors, retry/replan/repeat counters) before the next task. Carried state is
injected through the harness-internal state-override key
(`HARNESS_STATE_OVERRIDES_CONFIG_KEY`), which is stripped from the task config before the
engine sees it.

## Feature Flags (env vars)

- `AUTOBROWSER_AGENT_LOOP` (also `--agent-loop`) — **inert.** The engine-native path is the
  only runtime; the flag and `SessionConfig.agent_loop` parse for CLI compatibility but do
  not change routing.

## Commands

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -r requirements.txt

python -m pytest                              # full suite
python -m pytest tests\test_harness_session.py  # engine + session lifecycle
python -m pytest tests\test_prompts.py        # ALWAYS run after changing any prompt

python main.py                                # interactive REPL (browser + MCP)
python main.py --no-mcp --task "inspect page" # dry run, no browser/MCP (dev checks)
python main.py --show-state --task "..."      # debug state per step

python scripts/run_batch.py --tasks tests\golden\tasks.jsonl --no-mcp --continue-on-error
python scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v1.json
python scripts/replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

Focused test groups are grouped by area in `AGENTS.md` / `docs/development/setup.md`
(harness, browser provider, agent-loop contracts, CLI). Runtime output lands in
`.autobrowser/sessions/<session_id>/` (`session.json`, `tasks.json`, `events.jsonl`,
`workspace/`) — local and git-ignored; treat exporters/replay as read-only over it.

## When Changing Things

- Prompt change → update the prompt file, adjust `tests/test_prompts.py`, run it, and for
  browser-behavior changes inspect one `--show-state` trace for loops.
- The engine-native path is the only path — there is no compiled-graph rollback. Keep every
  behavioral change additive and covered by the native tests; keep `goal_id == task_id`.
- Redact secrets (token/password/credential/api_key/authorization) before persisting events.
  `agent_trace.jsonl` is a diagnostic sidecar, **not** the metrics source of truth.
- Update `docs/diagrams/` when engine phases, loop boundaries, session lifecycle, harness
  injection, policy routing, or MCP integration change. Add superseding ADRs; don't rewrite
  historical ones.
