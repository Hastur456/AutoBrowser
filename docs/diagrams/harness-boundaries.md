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
  SessionCtx --> MCPRuntime[MCPRuntime]
  SessionCtx --> Harness[BrowserHarness]
  MCPRuntime --> Manager[MCPManager]
  MCPRuntime --> ToolSource[MCPToolSource]
  MCPRuntime --> Normalizers[ToolCallNormalizers]
  Manager --> MCP[MCP servers]
  Session --> GoalRunner[GoalRunner]
  GoalRunner --> Engine[AgentLoopEngine]
  Harness --> ContextAssembler[ContextAssembler]
  Harness --> Tools[ToolRegistry]
  Harness --> Telemetry[TelemetryObserver]
  Engine --> Resources[EngineResources]
  Resources --> ContextAssembler
  Resources --> Tools
  Resources --> LLM
  Tools --> StaticTools[Static tools]
  Tools --> Providers[Generic providers]
  Tools --> ToolSource
  Tools --> Normalizers
  ToolSource --> Manager
  Engine --> TurnController[TurnController]
  TurnController --> Completion[CompletionController]
  TurnController --> ModelDriver[ModelDriver]
  TurnController --> ToolBroker[ToolBroker]
  TurnController --> ObsCompiler[ObservationCompiler]
  ToolBroker --> Tools
  ToolBroker --> Normalizers
  TurnController --> Journal[Action journal]
```

The boundary is intentional: `SessionRuntime` coordinates interaction lifecycle,
`SessionContext` owns session-scoped state and resources, `BrowserHarness` is a
pure composition root (no graph), and `AgentLoopEngine` owns reasoning and state
transitions over a frozen `LoopState`. MCP servers are owned by the session-scoped
`MCPRuntime` (see [MCP Runtime](mcp-runtime.md)); tools reach `ToolRegistry`
through `MCPToolSource`, and name/argument adaptation is done by stateless
`ToolCallNormalizer`s folded by `ToolBroker`, not by the engine. Conversation history is not a harness or `EngineResources` resource:
`src/harness/memory.py` provides functional message-shaping helpers the engine
calls, and the durable `list[Message]` is carried on `LoopState.messages` /
`SessionContext.state`. `SessionRuntime` carries useful state between tasks
through `SessionContext.state` and resets task-local fields before the next run,
while session metadata remains available in `.autobrowser`.
