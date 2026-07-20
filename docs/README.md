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
- [Diagrams](diagrams/index.md): Mermaid diagrams for graph and harness
  boundaries.
- [Architecture Decisions](decisions/index.md): ADR index and template.
- [Research](research/index.md): current open questions and suggested spikes.
- [Glossary](glossary.md): shared project terms.

## Maintenance Rules

- Keep docs aligned with observed code and configuration.
- Preserve historical ADRs; add superseding records instead of rewriting them.
- Update diagrams when graph nodes, harness boundaries, policy routing, or MCP
  integration changes.
- Update prompt documentation and `tests/test_prompts.py` together when agent
  behavior rules change.
