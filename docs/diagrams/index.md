# Diagrams

This directory contains Mermaid diagrams for architecture, runtime flows, and
development workflows.

## Index

- [Agent Runtime Flow](agent-runtime-flow.md): LangGraph node flow from task
  input through planning, policy, execution, observation, and completion.
- [Harness Boundaries](harness-boundaries.md): runtime infrastructure injected
  into the graph by `BrowserHarness`.
- [Search Task Sequence](search-task-sequence.md): expected browser-tool flow
  for search and result extraction tasks.

## Update Guidance

Update diagrams when graph nodes, subgraph boundaries, harness injection,
tool execution, policy routing, or MCP integration behavior changes.
