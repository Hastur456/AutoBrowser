# Harness Boundaries

This diagram shows how `SessionRuntime` coordinates lifecycle through
`SessionContext`, while `BrowserHarness` composes runtime infrastructure around
task execution inside a session-scoped graph thread.

```mermaid
flowchart LR
  CLI[main.py CLI] --> Session[SessionRuntime]
  Session --> SessionCtx[SessionContext]
  SessionCtx --> Config[SessionConfig]
  SessionCtx --> Tasks[TaskRecord history]
  SessionCtx --> Workspace[Session workspace]
  SessionCtx --> SessionFiles[session.json and tasks.json]
  SessionCtx --> Artifacts[ArtifactRegistry]
  SessionCtx --> Events[SessionEventBus]
  SessionCtx --> State[SessionState]
  SessionCtx --> Metadata[SessionMetadata]
  SessionCtx --> LLM[Chat model]
  SessionCtx --> Chrome[Chrome/CDP]
  SessionCtx --> MCPRuntime[MCP session]
  SessionCtx --> Harness[BrowserHarness]
  MCPRuntime --> PlaywrightProvider[PlaywrightMCPBrowserProvider]
  Harness --> ContextBuilder[ContextBuilder]
  Harness --> Memory[MemoryManager]
  Memory --> Checkpoints[Session thread checkpoint]
  Harness --> Tools[ToolRegistry]
  Harness --> Policy[PolicyEngine]
  Harness --> Telemetry[TelemetryObserver]
  Harness --> Graph[Compiled LangGraph]
  Tools --> StaticTools[Static tools]
  Tools --> Providers[Generic providers]
  Tools --> BrowserProviders[BrowserProvider adapters]
  BrowserProviders --> PlaywrightProvider
  BrowserProviders --> FakeProvider[FakeBrowserProvider]
  PlaywrightProvider --> MCP[MCP clients]
  Graph --> Planner[Planner subgraph]
  Graph --> Agent[Agent node]
  Graph --> Executor[Executor subgraph]
  Graph --> Observer[Observer subgraph]
```

The boundary is intentional: `SessionRuntime` coordinates interaction
lifecycle, `SessionContext` owns session-scoped state and resources,
`BrowserHarness` injects graph runtime dependencies, and the agent graph owns
reasoning and state transitions. Graph checkpoints are scoped to the active
session thread. Browser-specific schema adaptation is owned by
`BrowserProvider` adapters registered in `ToolRegistry`, not by the agent loop.
`SessionRuntime` carries useful state between tasks through
`SessionContext.state` and resets task-local graph fields before the next
invocation, while session metadata remains available in `.autobrowser`.
