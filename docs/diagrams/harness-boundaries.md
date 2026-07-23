# Harness Boundaries

This diagram shows how `SessionRuntime` coordinates lifecycle through
`SessionContext`, while `BrowserHarness` composes runtime infrastructure around
one task execution.

```mermaid
flowchart LR
  CLI[main.py CLI] --> Session[SessionRuntime]
  Session --> SessionCtx[SessionContext]
  SessionCtx --> Config[SessionConfig]
  SessionCtx --> Tasks[TaskRecord history]
  SessionCtx --> Workspace[Session workspace]
  SessionCtx --> Artifacts[ArtifactRegistry]
  SessionCtx --> Events[SessionEventBus]
  SessionCtx --> State[SessionState]
  SessionCtx --> Metadata[SessionMetadata]
  SessionCtx --> LLM[Chat model]
  SessionCtx --> Chrome[Chrome/CDP]
  SessionCtx --> MCPRuntime[MCP session]
  SessionCtx --> Harness[BrowserHarness]
  Harness --> ContextBuilder[ContextBuilder]
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

The boundary is intentional: `SessionRuntime` coordinates interaction
lifecycle, `SessionContext` owns session-scoped state and resources,
`BrowserHarness` injects graph runtime dependencies, and the agent graph owns
reasoning and state transitions.
