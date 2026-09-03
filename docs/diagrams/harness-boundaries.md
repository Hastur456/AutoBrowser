# Harness Boundaries

This diagram shows how `SessionRuntime` coordinates lifecycle through
`SessionContext`, while `BrowserHarness` is the composition root whose
collaborators `EngineResources.from_harness` bundles for the engine-native
`AgentLoopEngine`.

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
  Session --> GoalRunner[GoalRunner]
  GoalRunner --> Engine[AgentLoopEngine]
  Harness --> ContextBuilder[ContextBuilder]
  Harness --> Tools[ToolRegistry]
  Harness --> Policy[PolicyEngine]
  Harness --> Telemetry[TelemetryObserver]
  Engine --> Resources[EngineResources]
  Resources --> ContextBuilder
  Resources --> Tools
  Resources --> Policy
  Resources --> LLM
  Tools --> StaticTools[Static tools]
  Tools --> Providers[Generic providers]
  Tools --> BrowserProviders[BrowserProvider adapters]
  BrowserProviders --> PlaywrightProvider
  BrowserProviders --> FakeProvider[FakeBrowserProvider]
  PlaywrightProvider --> MCP[MCP clients]
  Engine --> TurnController[TurnController]
  TurnController --> Completion[CompletionController]
  TurnController --> ModelDriver[ModelDriver]
  TurnController --> ToolBroker[ToolBroker]
  TurnController --> ObsCompiler[ObservationCompiler]
  ToolBroker --> Tools
```

The boundary is intentional: `SessionRuntime` coordinates interaction lifecycle,
`SessionContext` owns session-scoped state and resources, `BrowserHarness` is a
pure composition root (no graph), and `AgentLoopEngine` owns reasoning and state
transitions over a frozen `LoopState`. Browser-specific schema adaptation is
owned by `BrowserProvider` adapters registered in `ToolRegistry`, not by the
engine. Conversation history is not a harness or `EngineResources` resource:
`src/harness/memory.py` provides functional message-shaping helpers the engine
calls, and the durable `list[Message]` is carried on `LoopState.messages` /
`SessionContext.state`. `SessionRuntime` carries useful state between tasks
through `SessionContext.state` and resets task-local fields before the next run,
while session metadata remains available in `.autobrowser`.
