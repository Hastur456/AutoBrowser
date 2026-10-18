# ADR-2026-10-19: Name-Free Tool Handling

Status: Accepted
Date: 2026-10-19

## Context

The [injected tool roles ADR](2026-10-17-injected-tool-roles.md) moved browser knowledge out of
the engine, but behind the `ToolRoles` abstraction the decision was still made by name:
`src/browser/names.py` treated every tool starting with `browser_` as a browser tool and the
tool named `browser_snapshot` as the one that returns the page. The same name logic sat in the
schema normalizer (`BrowserToolNormalizer`), in the prompt assembler (the `browser_` prefix and
task keywords switched the "Browser Rules" block on), in memory compression (a separate path for
`browser_snapshot`) and in the CLI (`/url` and `/snapshot` called tools by name). A prefix is the
convention of one MCP server, not a property of the protocol: a prefixed or different browser
server, or a session with no browser at all, fell outside that logic silently.

The goal is a harness whose behaviour is determined for any set of tools, browser or not.

## Decision

Nothing decides by a tool name. Each former name rule is replaced by protocol metadata, the
tool's schema or the session's configuration:

- **Kept snapshots per server, from `readOnlyHint`.** `BrowserState.snapshots` maps a tool server
  to the output of its latest successful read-only call; a successful call of that server that is
  not read-only drops the entry; failures change nothing; other servers are never touched. The
  engine gets the server (`PreparedToolCall.server`) and `tool_is_read_only(tool)` for every call.
  A server without annotations keeps no snapshot. `ToolRoles`, `NullToolRoles`,
  `BrowserToolRoles` and `EngineResources.tool_roles` are removed. The neutral `Tool` contract
  gains `annotations`, so local tools can declare `readOnlyHint` like `MCPTool` does.
- **Per server, not "latest read-only output".** A single kept output would let any read-only
  tool of another server (e.g. `memory_view`) replace the page and blind the permission
  resolver; keying by server keeps environments apart.
- **The browser layer reads text formats, not names.** `current_page_url` takes the
  `- Page URL:` line of the latest tool output, then of any kept snapshot; the permission
  resolver looks the referenced element up in whichever snapshot lists the ref.
- **Argument filtering by schema only.** `BrowserToolNormalizer` (a name-gated copy of
  `SchemaArgsNormalizer`) and `src/browser/normalization.py` are deleted.
- **Prompt resources by configuration.** `ContextAssembler(resources=...)` renders the resources
  the session chose; `SessionContext` passes the browser rules exactly when a browser server is
  configured (`browser_mcp_server`). Tool-name, state-key and task-keyword triggers are deleted.
- **Memory treats every tool alike.** With `--compress-tools` every result is cut to
  `memory.compressed_tool_result_chars`; the `compressed_snapshot_*` settings are removed
  (accepted and ignored with a `FutureWarning`). Compaction of superseded outputs was already
  name-free; persistent memory scopes by the page URL text, so non-browser sessions simply have
  no site scope.
- **CLI without tool lookups.** `/url` reads the page URL from the session state; `/snapshot`
  (a call to a screenshot tool by name) is removed; the dead `print_step` redaction and the inert
  `--hide-snapshot` flag go.
- **Fenced.** `tests/test_engine_boundaries.py` forbids browser-tool-name literals and the
  `browser_` prefix in every module under `src/` except the user's `prompts.py`.

## Consequences

- Any MCP server works the same way: what is kept, filtered and rendered follows from
  annotations, schemas and configuration. A browser server other than Playwright needs no code.
- For Playwright the behaviour is unchanged: `browser_snapshot` is read-only, actions are not;
  other read-only Playwright tools (console, network) now also refresh the browser server's
  snapshot, which only matters if their output lacks the page lines.
- Carried session state uses the key `snapshots` (`snapshot`/`browser` are gone); carry-over is
  in memory only, so no stored session breaks.
- Tools without `readOnlyHint` never have a snapshot kept; that is the safe default.
- The prompts still name Playwright tools; they are the user's to revise.
- Removed user-facing pieces: the `/snapshot` REPL command and `--hide-snapshot`.

## Alternatives Considered

- Keep a configurable snapshot tool name (`browser.snapshot_tool`): still name logic, only
  moved into configuration, and it does not cover other servers.
- Drop kept snapshots and let the browser layer scan the message history: changes the
  invalidation semantics and couples the browser layer to history compaction.
- Keep only the latest read-only output across servers: breaks with several servers (see above).

## Related

- Supersedes [2026-10-17 Injected Tool Roles](2026-10-17-injected-tool-roles.md) on how roles are
  decided (the engine boundary it introduced stays).
- [2026-09-28 Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)
- [2026-10-01 Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
- [Explicit agent objects plan §8](../development/2026-10-17-explicit-agent-objects-plan.md)
- Code: `src/agent_loop/execution/observation.py`, `src/agent_loop/execution/state.py`,
  `src/browser/pages.py`, `src/browser/permissions.py`, `src/agent_loop/context.py`,
  `src/harness/session.py`, `src/harness/memory.py`, `src/cli/agent_cli.py`,
  `tests/test_engine_boundaries.py`, `tests/test_observation_snapshots.py`.
