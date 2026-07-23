# Harness Boundaries

This diagram shows how `SessionRuntime` owns process-lifetime resources and
uses `BrowserHarness` to compose runtime infrastructure around one task
execution.

```mermaid
flowchart LR
  CLI[main.py CLI] --> Session[SessionRuntime]
  Session --> LLM[Chat model]
  Session --> Chrome[Chrome/CDP]
  Session --> MCPRuntime[MCP session]
  Session --> Harness[BrowserHarness]
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

The boundary is intentional: `SessionRuntime` owns interaction lifecycle and
long-lived resources, `BrowserHarness` injects per-task runtime dependencies,
and the agent graph owns reasoning and state transitions.
