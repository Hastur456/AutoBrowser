# MCP Runtime

How the session builds, starts and shuts down the universal `MCPManager`, and how MCP tools
reach the engine. See [ADR-2026-09-28: Universal MCP Manager](../decisions/2026-09-28-universal-mcp-manager.md)
and the [glossary](../glossary.md).

```mermaid
flowchart LR
  Settings["Settings.mcp_servers<br/>(fallback: default_mcp_servers)"] --> Build[build_mcp_runtime]
  Chrome[Chrome/CDP port] -->|"{cdp_port} / {cdp_endpoint}"| Build
  Build --> Registry[ServerRegistry]
  Build --> Runtime[MCPRuntime]
  Runtime --> Manager[MCPManager]
  Registry --> Manager
  Manager --> Conn1["owner task: playwright (stdio, stateful)"]
  Manager --> ConnN["owner task: other servers (stdio / streamable HTTP)"]
  Manager --> Catalog[Catalog: tools, resources, prompts, ServerStatus]
  Runtime --> Source[MCPToolSource]
  Catalog --> Source
  Source -->|"browser server unprefixed, others server__tool"| Registry2[ToolRegistry]
  Runtime --> Norm["normalizers: BrowserToolNormalizer, SchemaArgsNormalizer"]
  Norm --> Registry2
  Registry2 --> Resources[EngineResources]
  Resources --> Broker[ToolBroker]
  Broker -->|MCPTool.invoke| Manager
```

```mermaid
sequenceDiagram
  participant Session as SessionContext
  participant Runtime as MCPRuntime
  participant Manager as MCPManager
  participant Server as MCP server

  Session->>Runtime: build_mcp_runtime(cdp_port)
  Session->>Runtime: start()
  Runtime->>Manager: start()
  Manager->>Server: initialize + discovery
  alt browser server not READY
    Runtime->>Manager: shutdown()
    Runtime-->>Session: BrowserServerUnavailableError
  end
  Note over Manager,Server: reconnect supervisor, list_changed, liveness pings
  Session->>Runtime: close() (before Chrome)
  Runtime->>Manager: shutdown() (bounded)
```
