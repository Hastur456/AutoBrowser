# ADR-2026-10-17: Injected Tool Roles and a Browser-Free Engine

Status: Accepted
Date: 2026-10-17

## Context

`src/agent_loop/execution/` is meant to be server-neutral: tool names are compared exactly as
exposed, and browser schema adaptation belongs to `src/browser/` and `ToolCallNormalizer`s
([Universal MCP Manager](2026-09-28-universal-mcp-manager.md)). In practice the engine still
imported `src.browser.names`:

- `observation.py` decided that a tool acts on the page by the `browser_` prefix and that it
  returns the page snapshot by the exact name `browser_snapshot`. The snapshot-freshness rule
  of the [progress journal ADR](2026-09-28-server-neutral-progress-journal.md) (drop the kept
  snapshot after a successful non-read-only call) therefore only worked for an unprefixed
  Playwright server and would silently stop for a prefixed or different browser server.
- `guards.py` built a Playwright `browser_tabs` call (`{"action": "select", "index": N}`)
  from `pending_browser_tab_*` fields that nothing set any more — dead code carried across
  tasks in `SESSION_STATE_KEYS`.

`src/agent_loop/context.py` also imported the prefix check, to decide when the
`browser-agent-rules` prompt resource is relevant.

## Decision

- **Tool roles are a contract.** `ToolRoles` in `src/contracts.py` answers two questions about
  an exposed tool name: `is_observation` (a successful call returns the environment snapshot)
  and `is_stateful` (a successful call that is not read-only invalidates it). Read-only stays
  the MCP `readOnlyHint` annotation (`tool_is_read_only`), not a role.
- **The engine receives roles, never derives them.** `EngineResources.tool_roles` reaches the
  `ObservationCompiler` / `ToolResultNormalizer`. The default `NullToolRoles`
  (`src/harness/tools.py`) assigns none, so without a server-specific implementation no
  snapshot is kept or dropped — safe for any tool server.
- **Browser knowledge stays in `src/browser/`.** `BrowserToolRoles` (`src/browser/roles.py`)
  maps the browser names. `SessionContext.tool_roles` defaults to it and
  `SessionRuntime.run_task` passes it through `EngineResources.from_harness(...,
  tool_roles=...)`, next to hooks, permissions, approval and memory. Evals pass it explicitly.
- **The dead tab path is deleted**: `pending_tab_activation_request`, the `TABS_TOOL`
  reducer branch, `TABS_TOOL` itself and the `pending_browser_tab_index`/`_reason` fields in
  `BrowserState`, `SESSION_STATE_KEYS` and `AgentState`.
- **Prompt resources declare their tools.** `PromptResource.trigger_tool_prefixes` /
  `applies_to_tool` let the browser rules resource say which tools it is about, so
  `ContextAssembler` no longer imports `src.browser`.
- **The boundary is tested.** `tests/test_engine_boundaries.py` forbids `src.browser` imports
  (via `ast`) and words starting with `browser_` in every `execution/*.py`;
  `tests/test_harness_session.py` asserts the real session hands the engine
  `BrowserToolRoles`.

## Consequences

- The engine has no browser imports and no browser tool names; another browser server only
  needs its own `ToolRoles`.
- Behaviour is unchanged for the browser session and the eval baseline; the snapshot rule is
  the same, only its source moved.
- Risk: a composition path that forgets `tool_roles` silently keeps no snapshot. The session
  test covers the production path; anything new that builds `EngineResources` for a browser
  must pass the roles.
- Session carry-over stays compatible: it is in memory only, and
  `LoopState.from_state_overrides` ignores unknown keys.
- `BrowserState`, `BrowserStateReducer` and fields such as `last_browser_action` keep their
  names; a neutral rename is separate work. The boundary test is written so these names do
  not match.

## Alternatives Considered

- Classify by server instead of by name (`Tool.server`): the snapshot role is a single tool,
  not a whole server, and the browser server is exposed unprefixed; a server check would still
  need a name for the snapshot tool.
- Keep the name checks and only allowlist them in a test: documents the violation instead of
  removing it.
- Move `read_only_tools` into `ToolRoles`: it already comes from protocol metadata on the live
  tool list in `TurnController`; turning it into a role would replace an annotation with a
  configured judgment.

## Related

- [Explicit agent objects plan](../development/2026-10-17-explicit-agent-objects-plan.md)
- [2026-09-28 Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md):
  refined — the snapshot-freshness rule now reads injected roles.
- [2026-09-28 Universal MCP Manager](2026-09-28-universal-mcp-manager.md)
- [2026-10-01 Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
- Code: `src/contracts.py` (`ToolRoles`), `src/harness/tools.py` (`NullToolRoles`),
  `src/browser/roles.py`, `src/agent_loop/execution/observation.py`,
  `src/agent_loop/execution/resources.py`, `src/harness/session.py`,
  `tests/test_engine_boundaries.py`, `tests/test_tool_roles.py`.
