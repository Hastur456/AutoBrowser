# AutoBrowser Documentation

This documentation describes the current AutoBrowser architecture, development
workflow, decisions, diagrams, research notes, and shared vocabulary.

## Start Here

- [Architecture Overview](architecture/overview.md): project purpose, graph
  shape, runtime boundaries, and browser semantics.
- [Development Setup](development/setup.md): environment setup, tests, CLI
  usage, and prompt-change workflow.
- [Browser Agent Rules](development/browser-agent-rules.md): Playwright MCP
  interaction rules and search-flow debugging guidance.
- [Diagrams](diagrams/index.md): Mermaid diagrams for graph, session runtime,
  and harness boundaries.
- [Session Runtime Change](development/2026-07-23-session-runtime-change.md):
  implementation note for the long-lived session and `SessionContext` refactor.
- [Browser Engine Migration Branch](development/2026-07-26-browser-engine-migration.md):
  branch-level note for the provider boundary, fake backend, snapshot freshness
  guard, and related tests.
- [Agent Loop Observability Branch Plan](development/2026-07-27-agent-loop-observability-branch-plan.md):
  implementation plan for typed events, JSONL traces, replay helpers, scenario
  evals, and LangGraph v1 baseline comparison.
- [Batch And Export Data Contracts](development/2026-07-30-batch-export-data-contracts.md):
  current observability sources and JSONL contracts for batch scenarios, run
  indexes, feedback, metrics, and export rows.
- [Session-Scoped Agent Context Memory ADR](decisions/2026-07-25-session-scoped-agent-context-memory.md):
  decision record for preserving useful agent context across tasks in one
  interactive session.
- [Browser Provider Boundary ADR](decisions/2026-07-26-browser-provider-boundary.md):
  decision record for moving Playwright MCP adaptation behind browser providers.
- [Task Memory Isolation ADR](decisions/2026-07-24-task-memory-isolation-and-session-persistence.md):
  superseded historical decision record for per-task checkpoint cleanup and
  `.autobrowser` session records.
- [Architecture Decisions](decisions/index.md): ADR index and template.
- [Research](research/index.md): current open questions and suggested spikes.
- [Glossary](glossary.md): shared project terms.

## Maintenance Rules

- Keep docs aligned with observed code and configuration.
- Preserve historical ADRs; add superseding records instead of rewriting them.
- Update diagrams when graph nodes, session lifecycle, harness boundaries,
  browser provider boundaries, policy routing, or MCP integration changes.
- Update prompt documentation and `tests/test_prompts.py` together when agent
  behavior rules change.
