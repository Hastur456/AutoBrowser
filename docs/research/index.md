# Research

This directory collects unresolved questions, spikes, alternatives, and
behavioral notes for the AutoBrowser agent.

## Current Research Themes

### Search Flow Robustness

Recent execution traces showed a failure mode on dynamic commerce pages:

1. The agent navigates to a marketplace home page.
2. A snapshot exposes a visible Search button or search affordance.
3. The agent clicks that button instead of typing into the editable input.
4. The visible snapshot does not change.
5. The agent repeats the same click/snapshot cycle until the recursion limit.

Current mitigation is prompt-level:

- prefer direct `browser_type` into visible editable search controls;
- click a search affordance at most once;
- avoid `browser_find` for generic implementation words on localized pages;
- use direct search URL navigation as an early fallback when the site supports
  query parameters.

Open question: should this become an explicit policy or router rule instead of
being handled only through prompts?

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
