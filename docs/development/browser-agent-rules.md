# Browser Agent Rules

AutoBrowser is a Playwright MCP browser agent, not a traditional Playwright
test project. Browser actions must be driven by snapshots and refs.

## Source of Truth

Use `browser_snapshot` as the source of truth for visible browser state. Do not
infer selectors, XPath, class names, or DOM structure that are not exposed by
the snapshot or a verified tool result.

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
control is visible.

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

If policy says the current snapshot is already current, reuse it for extraction
or choose a different strategy instead of asking for the same snapshot again.

## Non-Progress Signals

Treat these patterns as non-progress:

- repeating the same tool call with the same arguments;
- clicking the same Search button/search icon after an unchanged snapshot;
- using `browser_find` for implementation words such as `search`, `input`,
  `textbox`, or `button` on localized pages;
- repeatedly repairing failed `browser_evaluate` snippets for the same task
  phase;
- requesting deeper snapshots when the current snapshot already contains enough
  visible result data.

When non-progress is detected, replan to a different visible control, direct URL
navigation, snapshot-based extraction, or a final answer with the data already
available.

## Result Extraction

Prefer extracting result data from `browser_snapshot` when it contains enough
visible titles, links, prices, snippets, or product cards. Use `browser_find`
only for simple plain-text checks. Use `browser_evaluate` only when snapshots
and simple text search are insufficient.

For product/search-result tasks, a representative sample is sufficient unless
the user explicitly asks for all results. If URLs are not directly available
from the snapshot, do not block completion solely to obtain URLs.
