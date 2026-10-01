# ADR-2026-09-30: Lifecycle Hooks Engine

Status: Accepted
Date: 2026-09-30

## Context

The engine-native loop hardcodes every check: built-in tool policy
(`classify_tool_request`), completion guards, and the observation compiler. There is no way to
add a deterministic, externally configured check — an URL allow-list, an approval profile for
batch runs, a "the final answer must be grounded in what the page showed" rule — without
editing the loop. The [lifecycle hooks research](../research/2026-09-30-lifecycle-hooks-research.md)
compared Claude Code, Codex, Gemini CLI, ADK and the Agents SDK; the
[implementation plan](../development/2026-09-30-lifecycle-hooks-implementation-plan.md) checks
the result against the current code. This ADR records the decisions.

## Decision

- **Neutral contract** in `src/contracts.py`: `HookEventName` (`goal_start`, `pre_tool_use`,
  `permission_request`, `post_tool_use`, `post_tool_use_failure`, `stop`, `goal_end`),
  `HookDecision` (`allow`/`deny`/`ask`), frozen `HookEvent` and `HookResult` dataclasses, and
  `HookHandler = Callable[[HookEvent], Awaitable[HookResult | None]]`. Events hold only
  dicts, tuples and scalars so `json.dumps(asdict(event))` works for future out-of-process
  hooks. A hook never sees or returns `LoopState`; the loop translates the aggregated
  outcome into `LoopState.apply(...)`. `None` means "no opinion".
- **Per-event semantics.**
  - `goal_start`: `deny` blocks the task before any model call; `additional_context` is
    appended after the user request.
  - `pre_tool_use` runs after built-in policy (`approved` or `needs_human`, never after
    `blocked`) and after request normalization: `deny` takes the built-in block path, `ask`
    routes to `needs_human`, `updated_input` replaces the arguments and is normalized again
    (the tool name may not change). `allow` does **not** bypass `needs_human`.
  - `permission_request` is the only auto-approval path: `allow` runs the tool without the
    human callback, `deny` is terminal `blocked` exactly like a human refusal, no decision
    (including a timeout) falls back to the human callback.
  - `post_tool_use` / `post_tool_use_failure`: `updated_output` replaces `content`/`error`
    before the result is applied to state, so every progress detector sees one version.
  - `stop` runs only for a model `done` with status `done`, never for guard terminals
    (turn cap, replan/failure limits, unchanged snapshots, blocked/cancelled stops). `deny`
    turns the turn into a new `decision: "continue"`: the final answer, completion status and
    plan completion are not applied; the rejection reason is fed back as a `[harness]` user
    message. `hooks.max_stop_blocks` and `loop.turn_cap` bound it; `stop_blocks` is
    task-local.
  - `goal_end` is observational and runs only for a normal terminal result.
- **Context is never mixed into tool output.** `additional_context` becomes a separate
  `[harness]` user message after observation compile, because snapshot fingerprints, progress
  detection and the action journal compare tool content. A hook that rewrites a
  `browser_snapshot` must keep the `ref=` lines — the rewritten text becomes
  `browser.snapshot`, the source of refs. Built-in hooks never rewrite snapshots.
- **Aggregation**: handlers run sequentially in registry order; the first `deny`
  short-circuits; `deny > ask > allow > None`; `updated_input`/`updated_output` chain;
  `additional_context` concatenates. Handlers must be `async` (checked at load time) so
  `asyncio.wait_for` can enforce a timeout. On timeout or exception `goal_start` and
  `pre_tool_use` fail closed (`deny`), the others have no decision; an explicit
  `fail_closed` on the hook spec overrides that.
- **Telemetry through the loop's emitter.** `HookEngine.run(event, on_record=...)` reports a
  `HookDecisionRecord` per handler and the loop emits it as `hook.decided` through its own
  `EventEmitter` — the one `EngineResources.from_harness(events=...)` selected and the
  `GoalRunner` watchdog polls. Each timeout must stay below `loop.progress_timeout_seconds`.
  The payload carries no `args` or `result`; event redaction is key-based only, so a hook's
  `reason` must not quote argument values. A `pre_tool_use` deny counts toward the existing
  `policy_block_count`.
- **Placement and lifetime.** Settings `HooksSettings`/`HookSpec`/`HookMatch` are the ninth
  section of `src/config.py` (disabled by default; `registry` entries name
  `package.module:attr` handlers, `options` turn the handler into a factory). `HookEngine`
  lives in `src/harness/hooks.py`, is built once per session in `SessionContext.initialize`
  (configuration errors fail session start) and reaches the loop through
  `EngineResources.hooks` (`NullHookEngine` by default). Generic handlers live in
  `src/harness/builtin_hooks.py`, browser handlers in `src/browser/hooks.py`.
- **`ToolBroker.prepare`/`invoke`** split the broker so hooks see the normalized request
  and resolved server; `execute` stays `invoke(prepare(...))`.

## Consequences

- With hooks disabled the runtime is byte-for-byte unchanged: no `HookEvent` is built, no
  event is emitted, existing payloads do not change, the eval baseline stays put.
- Tests and evals never read hooks from settings; they build `HookEngine` from an explicit
  `HooksSettings(...)`, so a personal `config.yaml` cannot change them.
- `url_policy` is a guardrail, not a security boundary: navigation through
  `browser_evaluate` or a link click is invisible to it.
- Hooks execute arbitrary Python named in a git-ignored config file; the SHA-256 of the
  registry is recorded in `session.json`.
- Follow-up: `command`/`http`/`mcp_tool` hook types, session/compaction events,
  moving `classify_tool_request` into non-removable built-in hooks.

## Alternatives Considered

- **General event bus with subscribers**: no ordering or aggregation of decisions; a
  decision point needs a deterministic verdict, not fan-out.
- **Parallel handler execution**: non-deterministic chaining of `updated_input` and
  context; sequential runs are cheap for in-process checks.
- **A `MODIFY` verdict**: modification is orthogonal to the decision; `updated_input`/
  `updated_output` alongside `allow`/`None` is simpler.
- **Hooks inside the MCP Manager**: it only sees MCP calls and has no loop context
  (task, plan, completion); the loop owns the decision points.
- **Auto-approval through `pre_tool_use` `allow`**: would let any pre-hook silently lift
  `needs_human`; Claude Code and Codex separate the two as well.
- **`HookEngine` owned by `BrowserHarness`**: the harness is built by the session, evals and
  tests alike, so a settings default would leak a personal `config.yaml` into evals and tests.
- **`HookEngine` with its own emitter**: `from_harness(events=...)` swaps the emitter, and the
  watchdog polls that one; hook events must land in the same stream.

## Related

- [Lifecycle hooks research](../research/2026-09-30-lifecycle-hooks-research.md)
- [Lifecycle hooks implementation plan](../development/2026-09-30-lifecycle-hooks-implementation-plan.md)
- [Native Agent Loop Engine](2026-08-31-native-agent-loop-engine.md)
- [Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)
- [Universal MCP Manager](2026-09-28-universal-mcp-manager.md)
