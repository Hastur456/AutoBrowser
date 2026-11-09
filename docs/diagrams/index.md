# Diagrams

This directory contains Mermaid diagrams for architecture, runtime flows, and
development workflows.

## Index

- [Agent Runtime Flow](agent-runtime-flow.md): the engine-native `AgentLoopEngine` in three
  views — the run (goal hooks, plan or recorded plan, the tier escalation check, turn cap,
  replan), one turn (step source or model on the current tier, stop hook, budget) and one tool
  call (progress guard, `pre_tool_use`, `PermissionEngine`, approval judge,
  `permission_request`, the human, invoke, `post_tool_use`, observe).
- [Harness Boundaries](harness-boundaries.md): session ownership through
  `SessionContext`, front ends and the stand as `SessionController` clients, runtime
  infrastructure bundled by `BrowserHarness` into `EngineResources`, and the session-scoped
  collaborators (hooks, permissions, memory, skills, replay, model tiers).
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
- [Memory Layers](memory-layers.md): per-turn context assembly (history budget,
  `Memory` and `Working Notes` blocks, memory tools in the normal pipeline) and the
  task boundary (task digest, staged trust, opt-in consolidation).

- [UI Integration](ui-integration.md): the terminal UI thread model (UI loop vs.
  `RuntimeLoop`, the render pump as the only writer) and a task with an approval card.
- [Eval Platform Sync](eval-platform-sync.md): batch folder to Langfuse dataset, experiment
  and scores — components and boundaries, one sync, and the states of an experiment trace.
- [Trace Export](trace-export.md): session events through the sinks (`events.jsonl`,
  `agent_trace.jsonl`, the OTLP sink) to Langfuse or another OTLP backend, and the span tree
  of one task.
- [Serve Mode](serve-mode.md) (phases 1, 3 and 4 implemented): the stand process
  (`autobrowser serve`) as a `SessionController` client and its import fences, the cost ladder
  state machine (probe → playback → retarget → agent → human), one replayed step with ref
  rebinding, retargeting and handoff, the model tier escalation, the Telegram channel and the
  worker pool.

## Update Guidance

Update diagrams when engine phases, loop boundaries, session lifecycle,
harness injection, tool execution, policy routing, lifecycle hooks, memory
layers, or MCP integration behavior changes.
