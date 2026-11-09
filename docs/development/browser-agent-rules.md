---
name: browser-agent-rules
description: Snapshot-driven browser interaction and extraction rules.
metadata:
  always: "true"
  requires: browser
  title: Browser Rules
---
# Browser Agent Rules

AutoBrowser is a Playwright MCP browser agent, not a traditional Playwright
test project. Browser actions must be driven by snapshots and refs.

## Source of Truth

Use `browser_snapshot` as the source of truth for visible browser state. Do not
infer selectors, XPath, class names, or DOM structure that are not exposed by
the snapshot or a verified tool result. A `browser_evaluate` script may rely
only on structure you have actually observed (roles, visible text, link targets
from the snapshot or from earlier evaluate output); an empty or unexpected
result means the assumed structure does not exist, so change the assumption
instead of re-running the same script.

Treat iframe entries in a snapshot as frame boundaries, not as proof that the
iframe itself is the main interactive content. After a failed click/type or an
unexpected page shape, take a fresh or deeper snapshot and identify the actual
visible controls inside the relevant frame or main content before acting.

## Element Identity

Use snapshot refs such as `ref=e123` for ref-based browser tools. A ref is
valid only for the exact snapshot that produced it. After a browser action that
can change visible state, old refs must be treated as stale unless the tool
result itself returns fresh refs.

Preferred interactions:

- `browser_click(ref)`
- `browser_type(ref)`
- `browser_hover(ref)`

Ref-based actions require a current snapshot. If the current state has no
snapshot, take `browser_snapshot` before clicking, typing, or hovering. If the
requested ref is not present in the latest snapshot, replan from visible refs
instead of reusing a ref from history.

Before `browser_type`, verify from the latest snapshot that the chosen ref is
editable: textbox, searchbox, combobox, textarea, input, or a generic element
whose accessible name, placeholder or visible label clearly marks an input.
Never type into a button, link, iframe, heading, image, or clearly non-editable
container; find the nested editable ref, focus the control, or take a deeper
snapshot first.

## Tabs

If a successful action reports that it opened or exposed another tab (for
example an Open tabs list with the new page), switch to that tab with
`browser_tabs` (`action=select` and the reported index) before
`browser_snapshot` or any page interaction. Do not repeat the click that opened
the tab.

## Search Flow

For search tasks:

1. Start with one snapshot after navigation.
2. If an editable search control is visible, type the query into that control.
3. Prefer submitting through typing when supported.
4. Use a search button only after the query is confirmed in the input.
5. After the results page loads, extract visible result titles and URLs or a
   representative sample if the user did not request exhaustive extraction.
6. Finish once the extracted data satisfies the user request.

Do not click a Search button before entering the query when an editable
textbox, searchbox, combobox, textarea, input, or clearly editable generic
control is visible. Inspect the current value of the search input first: type
or replace the query only when the field is empty or holds something else, and
prefer Enter after typing. A submit is not progress unless the page shows
results or another visible effect after it.

## Filters

Typing a value into a filter field (price, for example) is not enough to
complete the step. Confirm it with Enter or a visible Apply control, then use a
fresh snapshot to verify that the result list, count, selected filter chip, URL
or visible prices changed. If one attempt leaves the result list unchanged, do
not retype the same value into the same field: use a different visible control
or a URL with the filter parameters when the site supports them.

## Dynamic Search Controls

Some commerce pages expose only a visible search affordance at first. In that
case, the agent may click or focus the search affordance once, then take a
fresh snapshot to find the newly exposed editable input.

If that one attempt does not expose an editable input or the visible page is
unchanged, do not repeat the click and do not double-click the same control.
Use a different visible editable control if present, or switch to direct search
URL navigation when the site supports it.

For Ozon, the documented fallback pattern is:

```text
https://www.ozon.ru/search/?text=<url-encoded query>
```

## Snapshot Use

Avoid requesting a fresh snapshot after every successful action. Request a
snapshot when:

- the page has changed and fresh refs are needed;
- a ref was rejected or became stale;
- newly loaded results need to be confirmed;
- the visible structure is insufficient and a deeper snapshot may expose the
  needed controls or result details.

A `depth` of 5 (`{"depth": 5}`) is usually enough to show visible results;
increase it only when specific child elements are missing.

If the current snapshot still reflects the page, reuse it for extraction or
choose a different strategy instead of asking for the same snapshot again. The
engine does not enforce this with a snapshot-specific policy; only the generic
repeat guards apply.

## Non-Progress Signals

Treat these patterns as non-progress:

- repeating the same tool call with the same arguments;
- re-running a call that already returned an empty or identical result after
  re-capturing `browser_snapshot` — an unchanged page means the earlier result
  still holds (the Action History marks such calls "identical result Nx");
- clicking the same Search button/search icon after an unchanged snapshot;
- using `browser_find` for implementation words such as `search`, `input`,
  `textbox`, or `button` on localized pages;
- repeatedly repairing failed `browser_evaluate` snippets for the same task
  phase;
- requesting deeper snapshots when the current snapshot already contains enough
  visible result data.

When non-progress is detected, replan to a different visible control, direct URL
navigation, snapshot-based extraction, or a final answer with the data already
available. If the task cannot be completed, finish as blocked with what was
found instead of reporting it as done.

## Result Extraction

Prefer extracting result data from `browser_snapshot` when it contains enough
visible titles, links, prices, snippets, or product cards. Use `browser_find`
only for simple plain-text checks, never with regular expressions. Use
`browser_evaluate` only when snapshots and simple text search are insufficient;
if it fails with syntax, escaping or selector errors twice in the same task
phase, stop using it for that phase.

For product/search-result tasks, a representative sample is sufficient unless
the user explicitly asks for all results. If URLs are not directly available
from the snapshot, do not block completion solely to obtain URLs.
