# Diagrams

This directory contains Mermaid diagrams for architecture, runtime flows, and
development workflows.

## Index

- [Agent Runtime Flow](agent-runtime-flow.md): engine-native `AgentLoopEngine`
  flow from task input through planning, policy, execution, observation, and
  completion.
- [Harness Boundaries](harness-boundaries.md): session ownership through
  `SessionContext` and runtime infrastructure bundled by `BrowserHarness` into
  `EngineResources` for the engine.
- [Browser Provider Boundary](browser-provider-boundary.md): request/result
  normalization through `BrowserProvider` adapters before browser tool results
  return to the observer.
- [Session Runtime Sequence](session-runtime-sequence.md): process-long session
  startup, repeated task execution with session-scoped context memory,
  persisted session records, and shutdown.
- [Search Task Sequence](search-task-sequence.md): expected browser-tool flow
  for search and result extraction tasks.

## Update Guidance

Update diagrams when engine phases, loop boundaries, session lifecycle,
harness injection, tool execution, policy routing, or MCP integration behavior
changes.
