# AutoBrowser Documentation

This documentation describes the current AutoBrowser architecture, development
workflow, decisions, diagrams, research notes, and shared vocabulary.

## Start Here

- [Architecture Overview](architecture/overview.md): project purpose, engine
  loop shape, runtime boundaries, and browser semantics.
- [Development Setup](development/setup.md): environment setup, tests, CLI
  usage, and prompt-change workflow.
- [Browser Agent Rules](development/browser-agent-rules.md): Playwright MCP
  interaction rules and search-flow debugging guidance.
- [Lifecycle Hooks Guide](development/lifecycle-hooks.md): how hooks work, their
  architecture, configuration, Python and command hooks, and how to create, change,
  disable and test them.
- [Permissions Guide](development/permissions.md): tool authorization — rules
  (`deny > ask > allow`), modes, browser domain/target resources, interactive approvals and
  session grants, debugging `permission.decided`, and the sandbox layer underneath.
- [Diagrams](diagrams/index.md): Mermaid diagrams for the agent loop, session
  runtime, and harness boundaries.
- [Session Runtime Change](development/2026-07-23-session-runtime-change.md):
  historical note for the long-lived session and `SessionContext` refactor; its
  LangGraph checkpoint wording predates the engine-native runtime.
- [Browser Engine Migration Branch](development/2026-07-26-browser-engine-migration.md):
  branch-level note for the provider boundary, fake backend, snapshot freshness
  guard, and related tests.
- [Agent Loop Observability Branch Plan](development/2026-07-27-agent-loop-observability-branch-plan.md):
  implementation plan for typed events, JSONL traces, replay helpers, scenario
  evals, and the stored eval baseline comparison.
- [Batch And Export Data Contracts](development/2026-07-30-batch-export-data-contracts.md):
  current observability sources and JSONL contracts for batch scenarios, run
  indexes, feedback, metrics, and export rows.
- [Context Assembler And Prompt Split Plan](development/2026-08-01-context-assembler-prompt-split.md):
  historical plan for the assembled-context path; completed — `ContextAssembler` is the only
  prompt-construction implementation and the legacy context-mode switch was removed.
- [GoalRunner Branch Plan](development/2026-08-01-goal-runner-branch-plan.md):
  historical plan for the one-task lifecycle boundary between `SessionRuntime` and the
  engine; completed — the engine-native loop described in that plan is now the only
  runtime, so its LangGraph-era wording is a snapshot of the past.
- [Agent Loop Legacy Outcomes Cleanup](development/2026-08-05-agent-loop-legacy-outcomes.md):
  historical note for removing the transitional `outcomes.py` compatibility
  layer; completed by the engine-native ADR.
- [Agent Loop Engine Migration Touchpoints](development/2026-08-08-agent-loop-engine-migration-touchpoints.md):
  historical checklist of the legacy agent-loop, harness, browser, eval, CLI, and
  exporter coupling that the completed engine migration removed.
- [Typed Settings Module ADR](decisions/2026-09-16-typed-settings-module.md):
  the decision record for consolidating every tunable into the typed
  pydantic-settings root in `src/config.py`, with `AUTOBROWSER_<SECTION>__<FIELD>`
  names replacing the flat vendor variables.
- [Native Agent Loop Engine ADR](decisions/2026-08-31-native-agent-loop-engine.md):
  the engine-native `AgentLoopEngine` is the sole runtime; `src/agent/` and all
  compiled-graph control flow are removed.
- [Session-Scoped Agent Context Memory ADR](decisions/2026-07-25-session-scoped-agent-context-memory.md):
  decision record for preserving useful agent context across tasks in one
  interactive session.
- [Universal MCP Manager ADR](decisions/2026-09-28-universal-mcp-manager.md):
  every tool server is an `mcp_servers` entry managed by `MCPManager`;
  supersedes the browser provider boundary.
- [Server-Neutral Progress Journal ADR](decisions/2026-09-28-server-neutral-progress-journal.md):
  action journal, `Action History`, repeat blocking and honest completion.
- [MCP Manager Migration](development/2026-09-24-mcp-manager-migration.md):
  migration guide from browser providers to the MCP Manager (Russian).
- [Agent Loop Progress Recovery](development/2026-09-26-agent-loop-progress-recovery.md):
  post-migration plan for progress detection and completion (Russian).
- [Browser Provider Boundary ADR](decisions/2026-07-26-browser-provider-boundary.md):
  superseded record for the former Playwright adapter boundary.
- [Task Memory Isolation ADR](decisions/2026-07-24-task-memory-isolation-and-session-persistence.md):
  superseded historical decision record for per-task checkpoint cleanup and
  `.autobrowser` session records.
- [Architecture Decisions](decisions/index.md): ADR index and template.
- [Research](research/index.md): current open questions and suggested spikes.
- [Glossary](glossary.md): shared project terms.

## Maintenance Rules

- Keep docs aligned with observed code and configuration.
- Preserve historical ADRs; add superseding records instead of rewriting them.
- Update diagrams when engine phases, loop boundaries, session lifecycle,
  harness boundaries, tool-call normalization, policy routing, or MCP
  integration changes.
- Update prompt documentation and `tests/test_prompts.py` together when agent
  behavior rules change.
