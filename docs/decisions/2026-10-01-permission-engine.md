# ADR-2026-10-01: PermissionEngine

Status: Accepted
Date: 2026-10-01

## Context

Tool authorization lives in `src/agent_loop/execution/policy.py`: `classify_tool_request`
mixes a name-marker check (`payment`, `purchase`, `delete_account`, `credential` →
`needs_human`) with a progress guard (an identical outcome repeated
`loop.max_ineffective_actions` times → `blocked`). No Playwright MCP tool name contains a
marker, so the built-in approval branch is dead for the browser; there are no declarative
rules, no modes, no rule id in the audit trail, and no deny-first ordering between sources.
The [PermissionEngine research](../research/2026-10-01-permission-engine-research.md)
compared Claude Code, Codex, Gemini CLI, the Agents SDK and MCP annotations; the
[implementation plan](../development/2026-10-01-permission-engine-implementation-plan.md)
maps the result onto the current code. This ADR records the decisions.

## Decision

- **Split progress from authorization.** The identical-outcome check becomes a progress guard
  (`progress.ineffective_repeat_reason`) that runs on the raw request before `prepare` and
  keeps emitting `policy.decided`. Authorization moves to a deterministic, session-scoped
  `PermissionEngine` (`src/harness/permissions.py`); `policy.py` is deleted.
- **Contract** (`src/contracts.py`): `PermissionDecision = allow | ask | deny`,
  `PermissionMode = default | read_only | dont_ask | bypass`, frozen `PermissionCheck`
  (final normalized `tool`, `server`, `args`, `read_only`, `hook_ask_reason`) and
  `PermissionVerdict` (`decision`, `reason`, `source`, `rule_id`, `always_ask`, `grant_key`).
- **Rules** (`permissions.rules`, one list): `id`, `decision`, `tool` (fullmatch regex),
  `server` (exact), `args` (`{key: regex}`), `domains`/`not_domains` (suffix match),
  `target` (regex), `always_ask`, `reason`. Conflicts resolve **deny > ask > allow**; order
  and specificity do not matter. Non-removable rules live in code (`builtin`), config holds
  only user rules, because pydantic-settings replaces lists instead of merging them.
- **Position in the turn:** progress guard → `prepare` → `pre_tool_use` hooks →
  `PermissionEngine.evaluate` on the **final** prepared request (after `updated_input`).
  A hook `allow` therefore cannot lift a rule deny/ask; a hook `ask` is passed in as
  `hook_ask_reason` and goes through the same mode logic.
- **Modes:** `default` — no matching rule → allow; `read_only` — a tool without
  `readOnlyHint` and without an allow rule → deny; `dont_ask` — every ask → deny;
  `bypass` — ask → allow, but deny rules, `always_ask` and hook asks still hold.
- **`destructiveHint` is ignored.** The annotation inventory below shows that Playwright MCP
  sets `destructiveHint: true` on every non-read-only tool (`browser_click`,
  `browser_navigate`, `browser_type`, `browser_hover`, …), so "destructive → ask" would ask
  on every click and, headless, deny every action. Only `readOnlyHint` (accurately set) is
  used, by `read_only` mode. Restrictions on mutating tools are expressed as rules.
- **Fail closed.** An exception in rule matching or resource resolution → `deny` with
  `source: error`. A rule that needs a resource (`domains`, `not_domains`, `target`) that
  could not be resolved does not match for `allow` and does match for `deny`/`ask`.
- **Denials.** A rule/builtin/mode deny is **not terminal**: the model gets a tool message with
  the reason (`consecutive_failures + 1`) and may choose another path. A human refusal or a
  `permission_request` hook deny stays terminal `blocked` (parity).
- **Approvals.** `ask` → `approval.requested` → `permission_request` hooks → human callback
  answering `once | session | deny`. `session` stores an in-memory grant keyed
  `(server, tool, domain)` — never a `ref`, never "the whole tool forever"; grants do not
  cover `always_ask` rules or hook asks and do not outlive the session.
- **Browser specifics stay in `src/browser/permissions.py`:** a resource resolver (domain from
  `args.url` or the snapshot's `Page URL:`, click target from `element` + the snapshot line of
  the ref) and browser builtin rules — `browser_evaluate|browser_run_code(_unsafe)?` →
  `ask` + `always_ask`, `browser_file_upload|browser_drop` → `ask`.
- **Audit:** `permission.decided` (`tool`, `server`, `decision`, `source`, `rule_id`, `mode`,
  `reason`) and `approval.resolved` (`decision`, `by`, `scope`), both without arguments.

## Annotation Inventory

`@playwright/mcp@latest` (Playwright 1.64.0-alpha), all capabilities (`--caps=vision,pdf,
testing,tracing`); every tool also has `openWorldHint: true`.

| `readOnlyHint` | `destructiveHint` | Tools |
|---|---|---|
| `true` | `false` | `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_network_request`, `browser_find`, `browser_wait_for`, `browser_highlight`, `browser_hide_highlight`, `browser_annotate`, `browser_generate_locator`, `browser_pdf_save`, `browser_start_recording`, `browser_stop_recording`, `browser_start_tracing`, `browser_stop_tracing`, `browser_verify_*`, `browser_*_video`, `browser_video_*` |
| `false` | `true` | `browser_navigate`, `browser_navigate_back`, `browser_click`, `browser_type`, `browser_hover`, `browser_press_key`, `browser_fill_form`, `browser_select_option`, `browser_drag`, `browser_drop`, `browser_tabs`, `browser_evaluate`, `browser_run_code_unsafe`, `browser_file_upload`, `browser_handle_dialog`, `browser_close`, `browser_resize`, `browser_emulate_media`, `browser_resume`, `browser_mouse_*` |

Default capabilities expose 25 of them (no `vision`, `pdf`, `testing`, `tracing` groups). The
JavaScript runner is named `browser_run_code_unsafe` in this version.

`tests/mcp_fixtures/fake_server.py` now annotates `get.user` as read-only and `increment` as
a non-destructive mutation; `echo` stays unannotated to cover servers without annotations.

## Consequences

- One place answers "what may run", with a rule id in every decision and deny-first ordering
  between rules, modes, hooks and grants.
- The event order of a tool turn changes: `permission.decided` sits between the
  `pre_tool_use` `hook.decided` records and `tool.started`; `policy.decided` is emitted only
  by the progress guard.
- Name-marker tools change from terminal `blocked` to non-terminal deny under `dont_ask` (no
  Playwright MCP tool has such a name).
- `browser_evaluate` starts asking (headless: deny and continue) once the browser builtin
  rules are wired.
- The engine is a logical layer, not a sandbox: profile isolation and Playwright's
  `--blocked-origins` remain the real browser boundaries.

## Alternatives Considered

- Numeric rule priorities (Gemini CLI): more ways to get the order wrong; deny > ask > allow is
  enough.
- Separate `allow`/`ask`/`deny` lists (Claude Code): list replacement would hit each list
  independently; one list is simpler to reason about.
- Evaluating permissions before hooks (current built-in policy position): rules would not see
  `updated_input`, and a hook `allow` could be mistaken for an approval.
- Honouring `destructiveHint`: rejected on the inventory above.
- `auto` mode with an LLM classifier, persistent grants, asynchronous approval: out of scope.

## Related

- [Permissions Guide](../development/permissions.md)
- [PermissionEngine research](../research/2026-10-01-permission-engine-research.md)
- [Implementation plan](../development/2026-10-01-permission-engine-implementation-plan.md)
- [Lifecycle Hooks Engine](2026-09-30-lifecycle-hooks-engine.md)
- [Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)
