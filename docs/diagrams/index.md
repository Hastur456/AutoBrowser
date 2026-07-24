# Diagrams

This directory contains Mermaid diagrams for architecture, runtime flows, and
development workflows.

## Index

- [Agent Runtime Flow](agent-runtime-flow.md): LangGraph node flow from task
  input through planning, policy, execution, observation, and completion.
- [Harness Boundaries](harness-boundaries.md): session ownership through
  `SessionContext` and runtime infrastructure injected by `BrowserHarness`.
- [Session Runtime Sequence](session-runtime-sequence.md): process-long session
  startup, repeated task execution, task checkpoint cleanup, persisted session
  records, and shutdown.
- [Search Task Sequence](search-task-sequence.md): expected browser-tool flow
  for search and result extraction tasks.

## Update Guidance

Update diagrams when graph nodes, subgraph boundaries, session lifecycle,
harness injection, tool execution, policy routing, or MCP integration behavior
changes.
