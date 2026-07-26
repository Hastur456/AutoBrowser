# 2026-07-26 Browser Engine Migration Branch

This note records the branch-level changes currently present on
`browser-engine-migration`.

Compared against: `origin/main`
Merge base: `05da80a7652bfb025b081578513c87b857448b2e`

## Branch Commits

| Commit | Change |
| --- | --- |
| `76679ca` | Extract Playwright MCP argument adaptation from the executor into a provider. |
| `1e8b70b` | Move Playwright MCP behind a browser adapter boundary. |
| `0f03d0d` | Move Playwright tool adaptation behind providers. |
| `4ca965c` | Introduce canonical browser contracts and tool-name helpers. |
| `afc095f` | Enforce current snapshots for ref-based browser actions. |
| `0dcf2e8` | Add a fake browser backend provider for deterministic tests. |
| `0044111` | Wire browser providers through CLI bootstrap and session startup. |

## Committed Branch Diff Summary

At audit time, `git diff --stat origin/main...HEAD` reported 32 committed
branch files changed with 1680 insertions and 243 deletions. This summary does
not include the documentation files added by this note.

New source files:

- `src/browser/__init__.py`
- `src/browser/adapters/__init__.py`
- `src/browser/adapters/playwright_mcp.py`
- `src/browser/contracts.py`
- `src/browser/errors.py`
- `src/browser/fake.py`
- `src/browser/names.py`
- `src/browser/provider.py`

New test files:

- `tests/test_browser_contracts.py`
- `tests/test_fake_browser_provider.py`
- `tests/test_playwright_mcp_provider.py`

## Architecture Changes

### Browser Provider Boundary

The branch adds `src/browser/` as the neutral browser backend boundary.
Production browser access is still Playwright MCP, but Playwright-specific
request and result adaptation is no longer owned by the executor.

The new boundary includes:

- `BrowserProvider`: protocol for backends that expose tools and normalize
  tool requests/results.
- `BrowserAction` and `BrowserResult`: provider-neutral typed contracts.
- Canonical browser action names such as `browser.navigate`,
  `browser.snapshot`, `browser.click`, `browser.type`, `browser.hover`, and
  `browser.evaluate`.
- Shared browser error codes, including `invalid_ref`, `unknown_action`, and
  `action_failed`.

### Playwright MCP Adapter

`PlaywrightMCPBrowserProvider` wraps loaded Playwright MCP tools and handles
schema-specific adaptation:

- maps canonical `browser.*` names to Playwright MCP names such as
  `browser_snapshot`;
- maps `ref` to `target` when a Playwright MCP schema expects `target`;
- maps ref-like `target` values back to `ref` for legacy schemas;
- fills an `element` argument from the current snapshot line when required;
- removes unsupported arguments when a tool schema forbids extras;
- normalizes Playwright invalid-ref failures to the shared `invalid_ref`
  browser error code.

### Fake Browser Provider

`FakeBrowserProvider` exposes deterministic browser tools backed by a sequence
of snapshots. It supports navigation, snapshot, click, type, hover, and
evaluate actions without starting Chrome or MCP.

Use it for graph, policy, and executor tests that need browser behavior but
should not depend on external services.

### Harness and CLI Wiring

`ToolRegistry` now accepts both generic MCP-like providers and browser
providers. `SessionContext.initialize()` loads a `BrowserProvider` through
`load_browser_provider()` when MCP is enabled, then registers it with
`ToolRegistry`.

`BrowserHarness` still owns runtime injection for the compiled graph. The
agent graph builder accepts browser providers for compatibility, but the
session path now normally passes them through the shared registry.

### Executor Behavior

The executor now:

- resolves tools through `ToolRegistry`;
- sends browser requests through registered provider normalizers before tool
  invocation;
- sends raw tool results through provider result normalizers before returning
  state;
- returns shared browser error codes for unknown canonical browser actions;
- leaves raw non-provider tools unadapted.

This keeps Playwright MCP schema rules out of the executor itself.

### Snapshot and Ref Freshness

The branch strengthens snapshot-driven execution:

- ref-based `browser_click`, `browser_type`, and `browser_hover` require a
  current snapshot;
- if a ref-based action is requested without a current snapshot, the agent
  requests `browser_snapshot` first;
- if the requested ref is not present in the latest snapshot, the agent
  replans instead of reusing a stale ref from history;
- observer state clears the current snapshot after successful browser actions;
- invalid-ref, stale-element, and ref-timeout failures can set
  `needs_fresh_snapshot`;
- repeated unchanged snapshots and repeatedly ineffective browser actions are
  tracked for loop prevention.

### Policy and Prompts

Policy now understands the canonical browser vocabulary as well as Playwright
MCP tool names. Redundant snapshot blocking applies to `browser.snapshot` and
`browser_snapshot`, and accumulated ineffective browser actions can be blocked
before another UI retry.

Prompt changes reinforce the same runtime contract:

- use refs only from the current snapshot;
- prefer snapshot-based result extraction;
- avoid repeated search affordance clicks after unchanged snapshots;
- replan or use a fallback route when fresh snapshots do not resolve stale
  refs.

## Test Coverage Added or Expanded

The branch adds coverage for:

- canonical browser contracts, name mapping, error codes, and provider
  protocol shape;
- Playwright MCP request/result normalization;
- fake browser provider behavior and executor integration;
- `ToolRegistry` browser provider loading;
- CLI bootstrap returning a provider instead of raw MCP tools;
- agent snapshot freshness guards;
- observer stale-ref, timeout, and ineffective-action handling;
- graph acceptance of canonical browser tool names.

Useful focused checks:

```powershell
python -m pytest tests\test_browser_contracts.py tests\test_fake_browser_provider.py tests\test_playwright_mcp_provider.py
python -m pytest tests\test_agent_graph.py tests\test_harness_tools.py tests\test_main_cli.py
```

## Development Rules After This Branch

- Put browser backend contracts and adapters in `src/browser/`.
- Keep Playwright MCP process/session lifecycle in `src/mcp/`.
- Register browser backends through `BrowserProvider` and `ToolRegistry`.
- Keep backend-specific schema adaptation out of the executor and agent
  prompts.
- Prefer canonical `browser.*` names in provider-neutral tests; the
  Playwright adapter maps them to runtime MCP tool names.
- Use `FakeBrowserProvider` for deterministic tests that need browser behavior
  without Chrome, CDP, or MCP.

## Follow-Up Notes

- The production browser backend remains Playwright MCP. The provider boundary
  makes additional backends testable and swappable, but no second production
  backend is implemented in this branch.
- Keep the snapshot/ref rules in prompts, policy, observer logic, and browser
  provider tests aligned. A change in any one layer can reintroduce stale-ref
  loops.
