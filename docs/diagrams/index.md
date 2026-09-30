# Diagrams

This directory contains Mermaid diagrams for architecture, runtime flows, and
development workflows.

## Index

- [Agent Runtime Flow](agent-runtime-flow.md): engine-native `AgentLoopEngine`
  flow from task input through planning, policy, execution, observation, and
  completion, including the lifecycle hook points (`goal_start`, `pre_tool_use`,
  `permission_request`, `post_tool_use`, `stop`, `goal_end`) and the `continue`
  branch of a rejected completion.
- [Harness Boundaries](harness-boundaries.md): session ownership through
  `SessionContext` and runtime infrastructure bundled by `BrowserHarness` into
  `EngineResources` for the engine, plus the session-scoped `HookEngine` path.
- [MCP Runtime](mcp-runtime.md): settings-driven `MCPManager` construction,
  start/shutdown inside the session, and the catalog-to-`ToolRegistry` bridge.
- [Tool Call Normalization](browser-provider-boundary.md): request/result
  folding through stateless `ToolCallNormalizer`s in `ToolBroker` before MCP
  tool results return to the observer (replaces the `BrowserProvider` boundary).
- [Session Runtime Sequence](session-runtime-sequence.md): process-long session
  startup, repeated task execution with session-scoped context memory,
  persisted session records, and shutdown.
- [Search Task Sequence](search-task-sequence.md): expected browser-tool flow
  for search and result extraction tasks.

## Update Guidance

Update diagrams when engine phases, loop boundaries, session lifecycle,
harness injection, tool execution, policy routing, lifecycle hooks, or MCP
integration behavior changes.
