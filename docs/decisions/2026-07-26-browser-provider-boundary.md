# Browser Provider Boundary

Status: Accepted
Date: 2026-07-26

## Context

AutoBrowser is snapshot-driven and currently uses Playwright MCP for browser
automation. Before the browser-engine migration branch, Playwright-specific
request adaptation lived close to executor behavior. That made the executor
know about backend schema details such as `ref` versus `target`, optional
`element` arguments, and invalid-ref error text.

This coupling made browser behavior harder to test without MCP and made future
browser backend changes more likely to touch the agent loop.

## Decision

Introduce `src/browser/` as a provider-neutral browser boundary.

The boundary includes:

- `BrowserProvider`, a protocol for backends that expose tools and normalize
  tool requests/results.
- Provider-neutral action/result contracts in `src/browser/contracts.py`.
- Canonical browser names in `src/browser/names.py`, mapped to Playwright MCP
  names by adapters.
- Shared browser error codes in `src/browser/errors.py`.
- `PlaywrightMCPBrowserProvider` as the production adapter around loaded MCP
  tools.
- `FakeBrowserProvider` as a deterministic backend for tests and replay-style
  fixtures.

`ToolRegistry` registers browser providers, and the executor delegates
browser-specific request/result normalization to those providers before and
after tool invocation.

## Consequences

- Playwright MCP schema adaptation is no longer embedded in the executor.
- Browser behavior can be tested without starting Chrome, CDP, or MCP.
- The graph can accept canonical `browser.*` names in provider-neutral tests
  while production runtime still invokes Playwright MCP tool names.
- Policy, observer, prompts, and tests must stay aligned with the browser
  provider vocabulary and snapshot/ref freshness rules.
- The production backend is still Playwright MCP; this decision creates a
  boundary, not a new production engine.

## Alternatives Considered

- Keep Playwright MCP adaptation in the executor: rejected because backend
  schema details would keep leaking into graph execution logic.
- Treat browser tools as generic MCP tools only: rejected because browser refs,
  snapshots, stale elements, and invalid-ref recovery require browser-specific
  semantics.
- Build a full browser-engine abstraction now: rejected because the current
  need is a narrow provider boundary plus deterministic test backend.

## Related

- `src/browser/provider.py`
- `src/browser/contracts.py`
- `src/browser/names.py`
- `src/browser/errors.py`
- `src/browser/adapters/playwright_mcp.py`
- `src/browser/fake.py`
- `src/agent/subgraphs/executor/nodes.py`
- `src/harness/tools.py`
- `src/mcp/playwright_runtime.py`
- `tests/test_browser_contracts.py`
- `tests/test_fake_browser_provider.py`
- `tests/test_playwright_mcp_provider.py`
- [Browser Engine Migration Branch](../development/2026-07-26-browser-engine-migration.md)
- [Browser Provider Boundary Diagram](../diagrams/browser-provider-boundary.md)
