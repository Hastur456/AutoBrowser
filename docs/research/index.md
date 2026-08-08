# Research

This directory collects unresolved questions, spikes, alternatives, and
behavioral notes for the AutoBrowser agent.

## Current Research Themes

### Agent Loop Runtime

AutoBrowser currently has a LangGraph browser-task loop. The next architectural
step is an AutoBrowser-owned runtime loop inspired by Claude Code and Codex:
typed events, context assembly, tool brokering, permissions, hooks, skills,
subagents, durable traces, and scenario evals.

See [Agent Loop Runtime Research](2026-07-26-agent-loop-runtime-research.md).
See also [Codex-Claude Runtime Migration Plan](2026-07-26-codex-claude-runtime-migration-plan.md)
for a phased implementation plan.

### Proposed Action Contract

Phase 4 of the migration plan introduces a typed `ProposedAction` contract and
`ModelDriver` boundary before the LangGraph loop is replaced.

See [Phase 4 ProposedAction Contract Research](2026-08-05-phase-4-proposed-action-contract-research.md).

### Search Flow Robustness

Recent execution traces showed a failure mode on dynamic commerce pages:

1. The agent navigates to a marketplace home page.
2. A snapshot exposes a visible Search button or search affordance.
3. The agent clicks that button instead of typing into the editable input.
4. The visible snapshot does not change.
5. The agent repeats the same click/snapshot cycle until the recursion limit.

Current mitigation is split between prompts and runtime guards:

- prefer direct `browser_type` into visible editable search controls;
- click a search affordance at most once;
- avoid `browser_find` for generic implementation words on localized pages;
- use direct search URL navigation as an early fallback when the site supports
  query parameters.
- block accumulated ineffective browser actions when repeated UI attempts do
  not change the visible snapshot.

Open question: should search-affordance-specific loop detection become an
explicit policy or router rule instead of relying on generic ineffective-action
tracking?

### Snapshot Depth Strategy

The agent currently relies on `browser_snapshot` as the source of truth and can
request deeper snapshots when visible structure is insufficient.

Open questions:

- What default depth gives enough result data without excessive tool output?
- When should the observer recommend a deeper snapshot versus direct result
  extraction?
- Should the policy layer cap repeated depth increases per task phase?

### Tool Output Compression

When `--compress-tools` is enabled, the observer LLM sees only one tool result
and emits compact observation JSON.

Open questions:

- Which ref and visible-state fields are mandatory for safe next actions?
- How much product/result detail should be preserved before extraction becomes
  lossy?
- Should compression be disabled automatically for result-list extraction?

### Recursion-Limit Recovery

`BrowserHarness` attempts to return the latest checkpoint if the graph hits
`GraphRecursionError` after a final answer has already been produced.

Open questions:

- Should non-final recursion failures produce a partial diagnostic answer?
- Which loop patterns should be promoted from prompt guidance into hard policy?
- How should repeated non-progress be surfaced in CLI output?

## Suggested Spikes

- Add a policy test that blocks repeated identical search-affordance clicks
  after an unchanged snapshot.
- Add an integration trace fixture for Ozon-like search pages using mocked MCP
  tools.
- Compare direct search URL fallback versus UI-only search for marketplace
  sites.
