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
an Ollama-compatible chat model (default `gemma4:31b-cloud`). Control flow is
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
- `guards.py`, `observation.py`, `tools.py`, `resources.py` — the
  loop guards (including `CompletionController` and the progress guard), observation
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
  `ContextAssembler`, `ToolRegistry`, `TelemetryObserver`, `EventEmitter`
  and holds `EngineResources.from_harness` sources. It does not own memory; history shaping
  is a functional helper set in `src/harness/memory.py` and the durable list lives on
  `LoopState.messages`. There is no graph to stream and no turn-cap recovery here
  anymore.
- `src/agent_loop/execution/` — the engine owns **only** reasoning, routing, execution,
  observation. Infrastructure goes in `src/harness/`; browser schema adaptation goes in
  `src/browser/`.

Do not hardcode Playwright MCP behavior into the agent loop, and do not put browser schema
adaptation in the engine or prompts — add servers through `mcp_servers` (`src/mcp/`,
`src/harness/mcp_setup.py`) and adapt calls with `ToolCallNormalizer`s. See
`docs/decisions/2026-09-28-universal-mcp-manager.md`.

## The Engine Loop

`AgentLoopEngine.run` (in `src/agent_loop/execution/loop.py`):

```text
build initial plan (model call #0) -> while turn <= cap:
  TurnController.run_turn(LoopState) ->
    agent step -> decision
      done    -> terminal status via CompletionController
      replan  -> rebuild plan
      tool_call -> progress guard -> prepare -> pre_tool_use hooks
                -> PermissionEngine (deny | ask -> permission_request/human | allow)
                -> ToolBroker.invoke -> post_tool_use -> observe
```

A progress block, hook deny or permission deny short-circuits back to the loop (the model
reads the reason); a refused approval is terminal `blocked`. `settings.loop.turn_cap`
(default 50) bounds the loop. Authorization lives in `src/harness/permissions.py`
(`docs/decisions/2026-10-01-permission-engine.md`); there is no `execution/policy.py`.

## Browser Semantics (Hard Invariant)

The agent is **snapshot-driven, not selector-driven** (see `docs/development/browser-agent-rules.md`):

- `browser_snapshot` is the only source of truth; element identity is an ephemeral `ref=e123`
  valid **only** for the snapshot that produced it. Never guess CSS/XPath/class/DOM.
- Ref-based `click`/`type`/`hover` require a current snapshot. If the ref is absent from the
  latest snapshot, **replan from visible refs** — never reuse a historical ref.
- Don't snapshot after every action; don't re-click a search affordance after an unchanged
  snapshot; prefer typing into an editable control, then fall back to a direct search URL
  (e.g. Ozon `https://www.ozon.ru/search/?text=<query>`).

These rules are **duplicated across prompts, the guards, the observer, and provider
tests**. Changing one layer can reintroduce stale-ref/loop bugs — keep them aligned, and
don't remove an invariant from a prompt unless policy/observer/evals still enforce it.
`tests/mcp_fixtures/fake_server.py` (a real MCP server) and the local `_FakeBrowserTools`
helper (`src/agent_loop/evals.py`, duplicated where individual tests need it — there is no
shared `BrowserProvider` protocol or production fake anymore) exercise this behavior
deterministically without Chrome/CDP.

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

## Configuration

Every tunable lives in `src/config.py` — a pydantic-settings root read at call time through
`get_settings()`. There are **no scattered module constants**; adding one is a regression.
`src/config.py` is a neutral leaf like `src/contracts.py` and imports nothing from
`src/agent_loop/`, `src/harness/`, or `src/browser/` (it may import `src.contracts`, which
never imports it back). See
`docs/decisions/2026-09-16-typed-settings-module.md` and the `.env.example` template.

Names are `AUTOBROWSER_<SECTION>__<FIELD>` over ten sections (`llm`, `browser`, `loop`,
`observation`, `memory`, `events`, `storage`, `flags`, `hooks`, `permissions`). Env outranks `.env` outranks the code
defaults; an empty value means "not set"; sections are `frozen` with `extra="forbid"`.
`AUTOBROWSER_LLM__API_KEY` is passed to the provider as an explicit `Authorization: Bearer`
header — the vendor `OLLAMA_API_KEY` is no longer read. Adding a setting means updating
`tests/test_config.py`, which fails when `.env.example` drifts from the code.

A YAML file can slot in above the environment as a fourth source: `AUTOBROWSER_CONFIG_FILE`
names it explicitly (mandatory once set — a missing file fails startup), and with that unset,
`config.yaml` at the repo root auto-loads if present — a fixed path, not a working-directory
scan, and silent when absent. `config.yaml`/`config.local.yaml` are git-ignored, so the root
file doubles as a personal default profile. Precedence is init kwargs > YAML > env > `.env` >
secret files, and pydantic-settings merges deeply, so the file decides exactly the fields it
names while everything else still resolves below it: a partial profile, not a full config, and
one that beats `AUTOBROWSER_*` for the fields it sets. The file source rejects unknown section
names itself (the root is `extra="ignore"`). `config.example.yaml` is the template;
`docs/decisions/2026-09-16-opt-in-yaml-settings-file.md` records the original opt-in design,
superseded on the activation question by
`docs/decisions/2026-09-26-default-yaml-settings-file.md`.

## Lifecycle Hooks

Deterministic, config-driven checks at fixed loop points — `goal_start`, `pre_tool_use`,
`permission_request`, `post_tool_use`/`post_tool_use_failure`, `stop`, `goal_end`
(`docs/decisions/2026-09-30-lifecycle-hooks-engine.md`). **Disabled by default**
(`hooks.enabled`); handlers are async Python callables named in `hooks.registry`.

- Usage and operations guide (create/change/disable/test hooks):
  `docs/development/lifecycle-hooks.md`.
- A hook is `type: python` (an async `handler` imported by path) or `type: command` (an
  external process, Claude Code/Codex protocol: event JSON on stdin, exit `2` blocks with
  stderr as reason, JSON stdout → `HookResult`; `src/harness/command_hooks.py`,
  `docs/decisions/2026-09-30-command-hooks.md`).
- Where things live: contracts (`HookEvent`, `HookResult`) in `src/contracts.py`; settings in
  `src/config.py`; `HookEngine`/`NullHookEngine` in `src/harness/hooks.py`; generic handlers
  (`approve_tools`, `grounded_final_answer`) in `src/harness/builtin_hooks.py`; browser
  handlers (`url_policy`, `prompt_injection_scan`) in `src/browser/hooks.py`; ready
  stdlib-only command hooks (one script per hook, flags as options) in `scripts/hooks/`,
  registered by the commented block in `config.example.yaml` and tested by
  `tests/test_hook_scripts.py`; the call sites and `hook.decided` emission in
  `src/agent_loop/execution/loop.py`.
- `HookEngine` is session-scoped (`SessionContext.initialize`, reaching the loop through
  `EngineResources.hooks`), not a `BrowserHarness` resource. A hook never sees `LoopState`.
- A hook cannot lift a permission deny/ask (rules run after `pre_tool_use`, on the final
  arguments); only `permission_request` can stand in for the human. `stop` only
  checks a model `done`, never guard terminals. Hook context is a separate `[harness]`
  message, never appended to tool output (progress detection compares tool content).
- Tests and evals never read hooks from `get_settings()` (the personal `config.yaml` would
  leak in): build `HookEngine` from an explicit `HooksSettings(...)` or `RegisteredHook`s.

## Permissions

Deterministic tool authorization — `allow | ask | deny` + reason + `rule_id` + `source` —
evaluated after `pre_tool_use` hooks on the final arguments
(`docs/decisions/2026-10-01-permission-engine.md`; guide: `docs/development/permissions.md`).

- Where things live: contracts (`PermissionCheck`, `PermissionVerdict`, `ApprovalAnswer`) in
  `src/contracts.py`; settings (`permissions.mode`, `permissions.rules`,
  `PermissionRule`, `normalize_domain`) in `src/config.py`;
  `PermissionEngine` in `src/harness/permissions.py`; the browser resolver (domain from a
  `url` argument or the `Page URL:`, click target from `element` + the snapshot line of the
  ref) in `src/browser/permissions.py`; the CLI prompt in `src/cli/approval.py`; the call site and
  `permission.decided`/`approval.resolved` emission in `src/agent_loop/execution/loop.py`.
- Rules resolve `deny > ask > allow` regardless of order; a rule needing an unresolved
  resource matches for deny/ask, never for allow; any evaluation error is a deny.
- **No tool names in the engine or `src/browser/`, and no shipped rules**
  (`docs/decisions/2026-10-01-name-free-permission-defaults.md`). Out of the box nothing
  asks; risky tools (page JavaScript, file upload, sensitive names) are guarded only by
  rules the user configures — opt-in examples are commented in `config.example.yaml`.
  Don't reintroduce code-level rule lists or default ask rules.
- Modes: `default`, `read_only` (only `readOnlyHint` tools), `dont_ask` (ask → deny; forced
  for a configured `default` without a TTY), `bypass` (asks granted, deny/`always_ask`
  hold). `destructiveHint` is ignored (Playwright sets it on every mutating tool).
- A permission deny is **not** terminal (the model reads the reason); a refused approval is
  terminal `blocked`. The human answers `once | session | deny`; `session` stores a
  `(server, tool, domain)` grant on the session-scoped engine.
- Opt-in model judge (`permissions.approval_judge: off | model | classifier | both`,
  `src/agent_loop/execution/approval.py`, `docs/decisions/2026-10-02-model-approval-judge.md`):
  the acting model's optional `approval_request` argument (offered on tools without
  `readOnlyHint`, stripped at parse time) or a separate classifier call can escalate a default
  `allow` to an `always_ask`. The engine still never calls a model; judge asks never lift a
  rule. The agent prompt tells the model the approval gate exists, so it must not refuse
  user-requested purchases — keep that aligned with the classifier prompt and tests.
- Session-scoped like hooks (`SessionContext.permissions`, built before Chrome/MCP, reaching
  the loop via `EngineResources.permissions`); without it `EngineResources` gets
  `PermissionEngine.from_settings()` — the code-default settings (no rules), never the
  personal config.
  Tests build engines from explicit `PermissionsSettings`.

## Feature Flags (env vars)

- `AUTOBROWSER_FLAGS__AGENT_LOOP` (also `--agent-loop`) — **inert.** The engine-native path is the
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
