# Tool Call Normalization

This diagram shows how `ToolBroker` (`src/agent_loop/execution/tools.py`) folds every tool call
through stateless `ToolCallNormalizer`s: `prepare` normalizes the request and resolves the tool,
the engine runs `pre_tool_use` hooks and the `PermissionEngine` on the **final** arguments,
and `invoke` runs the tool and normalizes its result. It replaces the former `BrowserProvider`
boundary (see
[ADR-2026-09-28: Universal MCP Manager](../decisions/2026-09-28-universal-mcp-manager.md)).

```mermaid
sequenceDiagram
  participant Engine as TurnController
  participant Broker as ToolBroker
  participant Norm as ToolCallNormalizers
  participant Registry as ToolRegistry
  participant Tool as MCP-backed tool
  participant Manager as MCPManager

  Engine->>Engine: progress guard on the raw request
  Engine->>Broker: prepare(request, state)
  Broker->>Registry: current name to tool map
  Broker->>Norm: normalize_request(request, state, tools)
  Note over Norm: SchemaArgsNormalizer drops arguments the tool schema forbids (any server)
  Broker-->>Engine: PreparedToolCall (tool, server, request), or an unknown-tool result
  Engine->>Engine: pre_tool_use hooks, PermissionEngine, approval on the final arguments
  Engine->>Broker: invoke(prepared)
  Broker->>Tool: invoke(args)
  Tool->>Manager: call_tool(server, tool, args)
  Manager-->>Tool: CallToolResult
  Tool-->>Broker: text, or MCPToolExecutionError when isError
  Broker->>Norm: normalize_result(result)
  Broker-->>Engine: ToolResult (+ error_code)
  Engine->>Engine: post_tool_use hooks, observe (snapshots per server, action journal)
```

Normalizers hold no tools and do no ref rewriting or snapshot lookups; no normalizer looks at
tool names. Deterministic tests use `tests/mcp_fixtures/fake_server.py` (a real MCP server)
or the `_FakeBrowserTools` helper of `src/agent_loop/evals.py`, without Chrome or CDP.
