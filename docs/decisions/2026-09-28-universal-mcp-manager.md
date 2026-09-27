# ADR-2026-09-28: Universal MCP Manager Replaces Browser Provider Adapters

Status: Accepted
Date: 2026-09-28
Supersedes: [ADR-2026-07-26: Browser Provider Boundary](2026-07-26-browser-provider-boundary.md)

## Context

Browser tools used to reach the engine through a Playwright-specific path:
`src/mcp/playwright_runtime.py` (`load_browser_provider` / `close_mcp_session`) started one
Playwright MCP server, and `PlaywrightMCPBrowserProvider` (`src/browser/adapters/`) wrapped
its tools, rewrote `ref`/`target`/`element` arguments against the latest snapshot and parsed
invalid-ref error texts. Any second [MCP](../glossary.md) server would have needed its own
bespoke runtime and adapter, and the engine carried `browser_providers` it had to know about.
The design space is explored in
[research: MCP Manager system design 2.0](../research/2026-09-23-mcp-manager-system-design-2_0.md).

## Decision

- **`src/mcp/` is a server-agnostic host-side pool of MCP clients.** `MCPManager`
  (`manager.py`) owns one owner task per connection, a reconnect supervisor with
  `ReconnectPolicy` backoff, in-flight request tracking, `list_changed` rediscovery,
  liveness pings, `persistent`/`ephemeral` connection modes and bounded graceful shutdown.
  `ServerRegistry` + `StdioServerConfig` / `StreamableHttpServerConfig` (`config.py`) are
  the declarative server list; `catalog.py` holds discovered tools/resources/prompts and
  `ServerStatus`; `errors.py` defines typed errors with stable `error_code`s; `naming.py`
  qualifies tool names as `server__tool` (sanitized, capped at 64 chars).
- **Every server is only an entry in `Settings.mcp_servers`.** Nothing in `src/mcp/` knows
  about Playwright. The only self-description a server may give is `stateful: true`; a
  reconnect of a stateful server bumps a generation and raises
  `ServerConnectionLostError(stateful=True)` instead of being hidden. `${VAR}` is expanded
  strictly at validation; `{cdp_port}` / `{cdp_endpoint}` runtime placeholders are filled by
  `src/harness/mcp_setup.py` once the session's Chrome is up.
- **Session wiring lives in the harness.** `build_mcp_runtime()` returns an `MCPRuntime`
  (manager, live `MCPToolSource`, browser server name, normalizers). With no
  `mcp_servers` configured it falls back to `default_mcp_servers()` — Playwright MCP over
  `npx` attached to the session Chrome via `--cdp-endpoint`. `SessionContext` starts it
  once per session; a failing browser server aborts startup
  (`BrowserServerUnavailableError`), a failing auxiliary server only shows up in status.
  Shutdown closes MCP before Chrome.
- **`src/harness/mcp_tools.py` bridges catalog → tools.** `MCPToolSource` exposes tools under
  the qualified name, except for the browser server, which is exposed unprefixed so the
  canonical `browser_*` vocabulary used by policy, observation, evals and golden traces keeps
  working. `CallToolResult.isError` becomes `MCPToolExecutionError`; content blocks are
  flattened to text.
- **`BrowserProvider` adapters are replaced by stateless `ToolCallNormalizer`s.**
  `ToolBroker` folds every call through `EngineResources.tool_normalizers`:
  `BrowserToolNormalizer` maps canonical `browser.*` names to the exposed tool name, and
  `SchemaArgsNormalizer` drops arguments the tool schema forbids. There is no ref rewriting,
  snapshot lookup or error-text parsing any more.

## Consequences

- Adding a server (search, filesystem, a second browser) is configuration only.
- The engine and `src/agent_loop/execution/` are server-neutral (see
  [ADR-2026-09-28: Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)).
- Deleted: `src/mcp/playwright_runtime.py`, `src/browser/adapters/`,
  `src/browser/observation.py`, `tests/test_playwright_mcp_provider.py`,
  `tests/test_fake_browser_provider.py`. `src/browser/provider.py` and `FakeBrowserProvider`
  remain only as test scaffolding.
- Stale refs are no longer silently repaired: the model sees the server's own error and must
  re-snapshot, as the [browser rules](../development/browser-agent-rules.md) require.
- The manager never retries a `tools/call` after a timeout or connection loss (the SDK does
  not send `notifications/cancelled`, so the server may still complete the action).
- The browser server is chosen by the root setting `browser_mcp_server`; unset, it falls
  back to the `playwright` entry when one exists, and an unknown name fails startup.
- New dependency: `mcp>=1.24,<2`.

## Alternatives Considered

- Keep `BrowserProvider` and add one provider per server: duplicates lifecycle and reconnect
  logic per server and keeps server knowledge in the tool path.
- Use an MCP client from an agent framework: reintroduces the dependency stack removed by
  [ADR-2026-09-03](2026-09-03-drop-langchain-stack-provider-neutral-model.md).
- Prefix every tool, browser tools included: breaks the `browser_*` vocabulary relied on by
  prompts, policy, evals and golden traces.

## Related

- [Glossary](../glossary.md)
- [MCP Runtime diagram](../diagrams/mcp-runtime.md)
- [Tool Call Normalization diagram](../diagrams/browser-provider-boundary.md)
- [Migration guide](../development/2026-09-24-mcp-manager-migration.md)
- Code: `src/mcp/`, `src/harness/mcp_setup.py`, `src/harness/mcp_tools.py`,
  `src/harness/normalization.py`, `src/browser/normalization.py`
- Tests: `tests/test_mcp_manager.py`, `tests/test_mcp_tools_bridge.py`,
  `tests/test_browser_normalization.py`, `tests/mcp_fixtures/fake_server.py`
