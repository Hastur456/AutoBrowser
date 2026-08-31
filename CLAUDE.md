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

AutoBrowser is a Python 3.12 LangGraph agent that turns a natural-language task into a
plan → reason → policy → execute → observe loop. Browser interaction is **snapshot-driven**
via Playwright MCP (element `ref`s), not CSS/XPath. It runs as a long-lived interactive
`cmd2` REPL (`main.py`) over an Ollama-compatible chat model (default `gpt-oss:20b-cloud`).

## Two "Agent Loops" — Read This First

The single biggest source of confusion: **two directories both look like "the agent loop."**

- `src/agent/` — the **active** engine. The compiled LangGraph graph, its `AgentState`,
  nodes, routers, prompts, and the `planner`/`executor`/`observer` subgraphs. This is what
  actually runs today.
- `src/agent_loop/` — **provider-neutral contracts + observability + the future engine
  shell** being built alongside the graph: `ProposedAction`/`ModelDriver` (`actions.py`,
  `model.py`), events/tracing/replay/metrics, batch/export/evals, `ContextAssembler`
  (`context.py`), `GoalRunner` (`goals.py`), and the `AgentLoopEngine` shell (`engine.py`).

An in-progress migration (branch `feat/agent-loop-engine`) is moving control flow from the
LangGraph graph to an explicit `AgentLoopEngine`. It proceeds **incrementally behind flags**;
LangGraph stays the default and the rollback path until scenario-eval parity is proven.
Before assuming a module is "the final architecture," check
`docs/development/2026-08-08-agent-loop-engine-migration-touchpoints.md` — much of
`src/harness/` and `src/browser/` currently exists to feed the *legacy* graph shape and is
slated for rewrite. `src/agent_loop/outcomes.py` and `src/agent_loop/adapters/langgraph.py`
are explicit transitional debt — **do not grow them**; port behavior into engine-native
contracts instead.

## Ownership Chain & Layering Rules

```text
SessionRuntime -> GoalRunner -> task_runner -> BrowserHarness -> LangGraph graph
```

Each layer is hard-fenced; **respect the boundary the code is trying to keep**:

- `src/harness/session.py` — `SessionRuntime`/`SessionContext`: process & session lifecycle,
  MCP/browser resource lifetime, task history, context handoff. **Not a task solver.**
- `src/agent_loop/goals.py` — `GoalRunner`: one-task lifecycle + goal events only. Must not
  choose actions, judge completion, touch routing/counters/policy, or run a model loop.
- `src/harness/runtime.py` — `BrowserHarness`: per-task composition root. Injects
  `ContextBuilder`, `MemoryManager`, `ToolRegistry`, `PolicyEngine`, `TelemetryObserver`
  and invokes/streams the graph. Recursion-limit recovery lives here.
- `src/agent/` — the graph owns **only** reasoning, routing, execution, observation.
  Infrastructure goes in `src/harness/`; browser schema adaptation goes in `src/browser/`.

Do not hardcode Playwright MCP behavior into the agent loop, and do not put browser schema
adaptation in the executor or prompts — register it through `BrowserProvider`/`ToolRegistry`.

## The Compiled Graph

Assembled by `build_agent_graph` in `src/agent/agent.py`:

```text
START -> plan -> agent -> policy -> executor -> observe -> agent
```

`agent` also routes to `plan` (replan) or `END` (done); `policy` routes to `human_input`
(sensitive tool) or back to `agent` (blocked); `executor` always flows into `observe`.

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

All tasks in one REPL session share **one** LangGraph `thread_id` (from
`SessionContext.session_id`). Across tasks the runtime carries forward only durable context
(messages, latest observation, current snapshot, browser state, last-action metadata) and
**resets task-local fields** (`plan`, `decision`, `final_answer`, tool request/result,
policy state, errors, retry/replan/repeat counters) before the next task. `BrowserHarness`
injects carried state through an internal override key that is stripped before LangGraph
sees the config.

## Feature Flags (env vars)

- `AUTOBROWSER_AGENT_LOOP` — boolean (also `--agent-loop`). Routes execution through the
  explicit `AgentLoopEngine` shell (`src/agent_loop/engine.py`), which currently wraps
  `GoalRunner` with no behavior change. Unset → the LangGraph path. This is the seam the
  engine migration is being built on.
- `AUTOBROWSER_CONTEXT_MODE` — `legacy` (default) | `assembled`. `assembled` renders per-turn
  prompts through `ContextAssembler` (`src/agent_loop/context.py`); `legacy` is the rollback.

## Commands

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -r requirements.txt

python -m pytest                              # full suite
python -m pytest tests\test_agent_graph.py    # single file
python -m pytest tests\test_prompts.py        # ALWAYS run after changing any prompt

python main.py                                # interactive REPL (browser + MCP)
python main.py --no-mcp --task "inspect page" # dry run, no browser/MCP (dev checks)
python main.py --show-state --task "..."      # debug graph state per step

python scripts/run_batch.py --tasks tests\golden\tasks.jsonl --no-mcp --continue-on-error
python scripts/run_evals.py --baseline tests\evals\baselines\langgraph_v1.json
python scripts/replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

Focused test groups are grouped by area in `AGENTS.md` / `docs/development/setup.md`
(harness, browser provider, agent-loop contracts, CLI). Runtime output lands in
`.autobrowser/sessions/<session_id>/` (`session.json`, `tasks.json`, `events.jsonl`,
`workspace/`) — local and git-ignored; treat exporters/replay as read-only over it.

## When Changing Things

- Prompt change → update the prompt file, adjust `tests/test_prompts.py`, run it, and for
  browser-behavior changes inspect one `--show-state` trace for loops.
- Keep every behavior change behind a flag and keep the LangGraph path working until v2 wins
  on evals. Prefer additive files over rewrites during the migration; keep `goal_id == task_id`.
- Redact secrets (token/password/credential/api_key/authorization) before persisting events.
  `agent_trace.jsonl` is a diagnostic sidecar, **not** the metrics source of truth.
- Update `docs/diagrams/` when graph nodes, subgraph boundaries, session lifecycle, harness
  injection, policy routing, or MCP integration change. Add superseding ADRs; don't rewrite
  historical ones.
