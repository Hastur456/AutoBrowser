# Harness Boundaries

This diagram shows how `BrowserHarness` composes runtime infrastructure around
the LangGraph agent loop.

```mermaid
flowchart LR
  CLI[main.py CLI] --> Harness[BrowserHarness]
  Harness --> Context[ContextBuilder]
  Harness --> Memory[MemoryManager]
  Harness --> Tools[ToolRegistry]
  Harness --> Policy[PolicyEngine]
  Harness --> Telemetry[TelemetryObserver]
  Harness --> Graph[Compiled LangGraph]
  Tools --> StaticTools[Static tools]
  Tools --> Providers[Tool providers]
  Tools --> MCP[MCP clients]
  Graph --> Planner[Planner subgraph]
  Graph --> Agent[Agent node]
  Graph --> Executor[Executor subgraph]
  Graph --> Observer[Observer subgraph]
```

The boundary is intentional: browser-specific clients and toolsets should be
registered through `ToolRegistry` or injected through `BrowserHarness`, while
the agent graph owns reasoning and state transitions.
