# Browser Provider Boundary

This diagram shows how browser-specific adaptation is isolated behind
`BrowserProvider` implementations while the agent graph and executor keep using
the shared tool registry and state contracts.

```mermaid
sequenceDiagram
  participant Agent as Agent graph
  participant Policy as PolicyEngine
  participant Executor
  participant Registry as ToolRegistry
  participant Provider as BrowserProvider
  participant Tool as Browser tool
  participant Observer

  Agent->>Policy: Tool request
  Policy-->>Executor: Approved browser action
  Executor->>Provider: normalize_request(request, state)
  Provider-->>Executor: Runtime tool name and args
  Executor->>Registry: Resolve tool
  Registry-->>Executor: Tool instance
  Executor->>Tool: Invoke normalized request
  Tool-->>Executor: Raw result
  Executor->>Provider: normalize_result(result)
  Provider-->>Executor: Shared ToolResult
  Executor-->>Observer: tool_result
  Observer-->>Agent: Observation, snapshot, and freshness state
```

Production browser tools are wrapped by `PlaywrightMCPBrowserProvider`.
Deterministic tests can use `FakeBrowserProvider` without Chrome, CDP, or MCP.
