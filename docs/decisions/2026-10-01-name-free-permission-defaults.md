# ADR-2026-10-01: Name-Free Permission Engine Without Shipped Rules

Status: Accepted
Date: 2026-10-01

## Context

The [PermissionEngine ADR](2026-10-01-permission-engine.md) kept non-removable rules in code:
`BUILTIN_RULES` (tool names with `payment|purchase|delete_account|credential` → ask) in
`src/harness/permissions.py` and `BROWSER_BUILTIN_RULES`
(`browser_evaluate|browser_run_code(_unsafe)?` → ask + `always_ask`,
`browser_file_upload|browser_drop` → ask) in `src/browser/permissions.py`. The browser
resolver also named `browser_navigate`/`browser_tabs` to find a destination URL. So the
engine and the browser layer both hardcoded tool names, and out of the box the agent asked
for approval on page JavaScript and file upload. Headless runs denied both.

Other hardcoded names came from the deleted browser adapters. `src/browser/names.py` mapped a
"canonical" dotted vocabulary (`browser.click`, `browser.evaluate`, …) to Playwright names.
`src/browser/contracts.py` declared it as `BrowserActionName`, and `BrowserToolNormalizer`
resolved dotted names through it. The prompts, eval scenarios and fakes still used the dotted
names, and the fake browser kept an adapter-era `browser_evaluate(expression | script)`. The
REPL `url` command ran `browser_evaluate` directly, bypassing the PermissionEngine.

MCP annotations cannot replace the names: Playwright MCP marks `browser_evaluate` and
`browser_click` alike (`readOnlyHint: false`, `destructiveHint: true`, `openWorldHint: true`).
Any rule that tells page JavaScript from a click has to name the tools.

## Decision

- **No rule ships with the code.** `BUILTIN_RULES`/`BROWSER_BUILTIN_RULES` and the
  `builtin`/`extra_builtin` parameters are deleted, and nothing replaces them with defaults.
  `permissions.rules` (default `[]`) is the only rule list. Out of the box nothing asks for
  approval. The agent must not stop for confirmation on routine actions, and guarding risky
  tools is an explicit, opt-in profile decision. `config.example.yaml` carries commented
  examples (`page-js`, `file-handoff`, `sensitive-tool-names`).
- **The engine knows no names.** `PermissionEngine(rules=…)` holds only what it is given.
  `PermissionEngine.from_settings(settings=None)` uses the code-default settings (no rules,
  never the personal config) and is the default of `EngineResources.permissions`.
- **The `builtin` verdict source is removed** from `PermissionSource`. Rule verdicts are
  `source: rule`.
- **Resolver by argument, not by name.** A call with a `url` argument acts on that URL's
  domain (a destination without a host still never falls back to the current page). Any
  other call uses the `Page URL:` of the latest output or snapshot, as before.
- **One vocabulary: the exposed names.** `CANONICAL_TO_PLAYWRIGHT`/`PLAYWRIGHT_TO_CANONICAL`,
  `to_playwright_browser_name`/`to_canonical_browser_name`, `src/browser/contracts.py`
  (`BrowserAction`, `BrowserActionName`, `BrowserResult`) and the `unknown_action` error code
  are deleted. `BrowserToolNormalizer` no longer translates names, so a dotted name reaches the
  broker as an unknown tool. Prompts, eval scenarios and tests use `browser_*` names. The fake
  browser's `browser_evaluate` is removed.
- **The REPL `url` command is read-only.** It reads the snapshot's `Page URL:` and never runs
  page JavaScript.

## Consequences

- Behaviour change: page JavaScript, file upload and tool names such as `purchase_item` run
  without approval unless a profile adds a rule. `dont_ask` runs no longer deny them. The
  engine stays a guardrail you opt into; profile isolation and `--blocked-origins` remain the
  browser boundaries.
- The engine and `src/browser/` contain no tool names. The remaining browser names
  (`browser_snapshot`, `browser_tabs`, the `browser_` prefix) belong to the snapshot-driven
  loop invariants, not to authorization.
- Tests that exercise ask/`always_ask`/grants configure their rules explicitly.
- A model that still emits `browser.click` gets an unknown-tool error instead of a silent
  translation. The prompts no longer teach that name.

## Alternatives Considered

- Default rules as settings data (`permissions.default_rules`, shipped and replaceable):
  removes names from the engine, but still asks on page JavaScript out of the box and keeps a
  second list. Rejected in favour of opt-in rules.
- Per-server annotation overrides (`mcp_servers.<server>.tool_annotations`) with rules over
  annotations: a new concept and more code for the same opt-in facts.
- Keeping the rules in code: that is the hardcode this ADR removes.

## Related

- [PermissionEngine](2026-10-01-permission-engine.md): superseded on builtin rules, the
  `builtin` source and the browser builtin rules
- [Universal MCP Manager](2026-09-28-universal-mcp-manager.md)
- [Permissions Guide](../development/permissions.md)
