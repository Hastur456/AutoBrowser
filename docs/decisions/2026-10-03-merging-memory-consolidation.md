# ADR: Merging Memory Consolidation

Status: Accepted
Date: 2026-10-03

## Context

[ADR-2026-10-02: Layered Agent Memory](2026-10-02-layered-agent-memory.md) added opt-in
consolidation: after a `done` task one model call proposes up to three
`{path, description, body}` entries, written through `MemoryStore.create` as `unverified`.
The first real sessions with `consolidate_on_goal_end: true` showed three defects:

1. **Trust never accumulated.** `SessionRuntime._update_memory` runs
   `MemoryStore.record_outcome` (the task's success: `uses += 1`) and then consolidation. On a
   site visited again, consolidation proposes the same `sites/<domain>.md`, and `create`
   rewrote it with `uses: 0`. With `promote_after_successes: 2` an entry for a regularly used
   site stayed `unverified` forever.
2. **Knowledge was replaced, not accumulated.** The consolidation prompt carried only the
   index (one description line per entry), yet told the model to "repeat what stays true". The
   model could not see the body it was replacing, so a second task on the same site dropped
   what the first one had learned.
3. **DOM-scraping advice passed the policy.** A consolidation answer saved "searching the page
   text for currency symbols or using broad `a` tag filters for product links is more
   reliable". `BrowserMemoryPolicy` only refused refs, CSS/XPath syntax, `querySelector`,
   injection phrases and secrets, so advice that steers the snapshot-driven agent towards
   `browser_evaluate` and tag filters was persisted and rendered into every later Ozon turn.

The memory modules also held their own limits (`MAX_ENTRIES = 3`, `_FIELD_CHARS = 4_000`,
`_DIGEST_REQUEST_CHARS = 300`, `_DIGEST_ANSWER_CHARS = 500` and inline caps), against the rule
that every tunable lives in `src/config.py`.

## Decision

- **Consolidation sees the bodies.** The user prompt gains a `Current entries for the sites
  visited` section with the full body of every entry scoped to a visited domain (cut to
  `file_max_chars`) and an `Entry limit`. The system prompt says an entry it returns replaces
  the whole file: keep every fact that still holds, add what the task taught, and leave out
  entries it has nothing new for.
- **A consolidation rewrite keeps the trust counters.** `MemoryStore.create` takes
  `keep_trust: bool = False`; consolidation passes `True`, so an existing entry keeps `uses`
  and `failures` (the status is still `unverified`, the source is the new task). A
  `memory_write` `create` or `str_replace` by the acting model still starts trust over.
  `verified` and user entries stay untouchable for consolidation, as before.
- **The content policy refuses DOM-scraping advice.** `BrowserMemoryPolicy` gains a scraping
  category: tag names in code form followed by "tag"/"element" (`` `a` tag ``, `<div>
  elements`), "tag/class filters/selectors/names", `browser_evaluate`, `document.*`,
  `getElementsBy*`, `innerText`/`textContent`/`innerHTML`/`outerHTML`, `DOM`, and searching or
  parsing the page text/source/HTML. Plain prose ("add a tag", "the page text updates after
  scrolling") stays allowed. The consolidation prompt and the `memory_write` description name
  the same ban.
- **Memory limits are settings.** New `MemorySettings` fields replace the module constants:
  `digest_request_chars` (300), `digest_answer_chars` (500), `index_description_chars` (120),
  `block_min_section_chars` (40), `consolidation_max_entries` (3),
  `consolidation_field_chars` (4000), `event_reason_chars` (300). The defaults keep the
  previous behavior. `tests/test_memory_boundaries.py` fails on any numeric module constant in
  the memory modules.

## Consequences

- A site entry that consolidation keeps refining can now reach `verified` after
  `promote_after_successes` successful tasks, after which consolidation leaves it alone; new
  facts about that site then need a new entry (for example a `procedures/` file) or a human
  edit.
- Facts merged into an entry inherit its counters, so a fact added on the task before
  promotion is promoted after one success of its own. The merge keeps the entry about one
  site, so this is accepted.
- The consolidation prompt grows by the bodies of the visited sites' entries (bounded by
  `file_max_chars` each).
- The scraping patterns can refuse a legitimate sentence that names the DOM or page source;
  the refusal is a tool error the model reads, or a `rejected` item in `memory.consolidated`.
- Entries persisted before this change are not re-checked: the policy runs on writes only.

## Alternatives Considered

- **Merge in code (append new bullets to the old body).** Deterministic, but it cannot drop
  facts that turned out false and grows every file until `file_max_chars` refuses the write.
- **Skip consolidation for entries the task loaded.** Keeps the counters, but then a site's
  entry never learns anything new once it exists.
- **Reset trust on every rewrite (previous behavior).** Correct for arbitrary rewrites, but it
  made promotion unreachable for exactly the sites the agent uses most.

## Related

- Extends [ADR-2026-10-02: Layered Agent Memory](2026-10-02-layered-agent-memory.md).
- Code: `src/harness/memory_consolidation.py`, `src/harness/memory_store.py`
  (`create(keep_trust=...)`), `src/browser/memory.py`, `src/agent_loop/prompts.py`,
  `src/config.py` (`MemorySettings`).
- Guide: [Memory Guide](../development/memory.md); diagram:
  [Memory Layers](../diagrams/memory-layers.md).
