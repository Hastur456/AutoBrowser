# Permissions Guide

How tool authorization works, how to write permission rules, which mode to run in, how
approvals and session grants behave, and how to debug a decision. The decisions behind the
design are in [ADR-2026-10-01: PermissionEngine](../decisions/2026-10-01-permission-engine.md);
the background is the [PermissionEngine research](../research/2026-10-01-permission-engine-research.md);
terms are in the [glossary](../glossary.md).

## What It Is (and Is Not)

The `PermissionEngine` (`src/harness/permissions.py`) is a **deterministic authorization
function** evaluated before every tool call:

```text
(final normalized call, resources, rules, mode, session grants) -> allow | ask | deny
                                                                   + reason + rule_id + source
```

- It decides what the runtime **lets** run. The model decides what the agent **tries**; a
  prompt instruction never changes a permission.
- It is **not** progress control. "This exact call already returned the same result three
  times" is the progress guard (`guards.progress_block_reason`, event `policy.decided`).
- It is **not** a sandbox. Rules cover the usual shape of a call; prompt injection or a
  redirect can still lead the browser somewhere. The real browser boundaries are the profile
  and Playwright MCP's own options — see [Second Layer: Sandbox](#second-layer-sandbox).

## Where It Sits in a Tool Turn

```text
action.proposed
progress guard (raw request)          -> blocked: tool message to the model (policy.decided)
ToolBroker.prepare                    normalizers, resolve tool + server
  unknown tool                        -> hooks and permissions skipped, error result observed
pre_tool_use hooks                    deny -> blocked; ask -> escalation; updated_input -> re-prepare
PermissionEngine.evaluate             on the FINAL arguments            (permission.decided)
  deny                                -> tool message with the reason, the turn continues
  ask   -> approval.requested
           permission_request hooks   deny -> task ends blocked; allow -> run
           human callback             once | session (+grant) | deny -> task ends blocked
           approval.resolved
  allow
tool.started -> invoke -> post_tool_use hooks -> observe
```

Consequences worth knowing:

- Rules see what will really run: after normalization and after a hook's `updated_input`.
- A hook `allow` cannot lift a rule `deny`/`ask` — it runs before the rules.
- A hook `ask` is escalated through the same logic as an `ask` rule (`dont_ask` turns it into
  a deny; `bypass` and grants cannot skip it).
- A permission **deny is not terminal**: the model reads `"<tool>\n\n<reason>"` as the tool
  output (`consecutive_failures + 1`) and may choose another path. Only a **refused
  approval** (human or `permission_request` hook) ends the task `blocked`.

## Modes

`permissions.mode` (`AUTOBROWSER_PERMISSIONS__MODE`, or `--permission-mode` for one run):

| Mode | No matching rule | `ask` | Typical use |
|---|---|---|---|
| `default` | allow | the human (or a `permission_request` hook) | interactive REPL |
| `read_only` | allow only `readOnlyHint` tools; everything else is denied unless an `allow` rule matches | the human | research / "look, don't touch" |
| `dont_ask` | allow | **deny** (non-terminal) | headless, batch, evals |
| `bypass` | allow | **allow** — except `always_ask` rules and hook asks | trusted automation; prints a warning |

Deny rules hold in **every** mode, including `bypass`.

Without a terminal (piped `--task`, `scripts/run_batch.py`) a configured `default` becomes
`dont_ask`, so a guarded call is denied and the model continues instead of every such task
ending `blocked`. `scripts/run_evals.py` scenarios run in `dont_ask` unless they say
otherwise. An explicit `--permission-mode` always wins.

`destructiveHint` is ignored on purpose: Playwright MCP sets it on every mutating tool
(`browser_click`, `browser_navigate`, …), so honouring it would ask on every click. The
annotation inventory is in the ADR. `readOnlyHint` is accurate and is what `read_only` uses.

## Rules

Rules live in `permissions.rules` — the only rules there are (see
[No Shipped Rules](#no-shipped-rules)). Prefer the YAML file (`config.yaml`); a list set in
one settings source **replaces** the list of the sources below it, it never merges.

```yaml
permissions:
  mode: default
  rules:
    - id: shop-only                 # required, unique
      decision: deny                # allow | ask | deny
      server: playwright            # exact MCP server name; "" (default) = any
      tool: browser_navigate        # re.fullmatch on the exposed tool name; default ".*"
      not_domains: [ozon.ru, wildberries.ru]
      reason: "Only the two shops are allowed."   # shown to the model; {tool} is replaced
```

| Field | Matches when |
|---|---|
| `tool` | `re.fullmatch(tool, name)`; `a\|b` works as a list, escape a literal dot |
| `server` | equal to `MCPTool.server` (empty = any; the browser server's tools are exposed unprefixed, so match them by `tool`) |
| `args` | every `{key: regex}` is found (`re.search`) in `str(args[key])`; a missing argument does not match |
| `domains` | the resolved domain equals or is a subdomain of one entry (`ozon.ru` covers `www.ozon.ru`, `seller.ozon.ru`) |
| `not_domains` | the resolved domain is outside every entry |
| `target` | `re.search(target, resolved_target)` |
| `always_ask` | (`ask` only) no session grant and no `bypass` can cover the call |

Precedence is **deny > ask > allow**, whatever the order and however specific: a narrow
`allow` never carves an exception out of a broad `deny`. There are no priorities.

**Fail closed.** When a rule needs a resource (`domains`, `not_domains`, `target`) that could
not be resolved — a `data:` page, a snapshot without `Page URL:`, no snapshot at all — the rule
**matches** if it is a `deny`/`ask` and **does not match** if it is an `allow`. Any exception
during evaluation is a `deny` with `source: error` (the exception text never reaches the
model or the events).

Startup fails on a rule that is wrong: a regex that does not compile, a duplicate id,
`always_ask` on a non-`ask` rule, `domains` together with
`not_domains`, or a domain that is not a host (`https://…`, a path, a port).

Domains are normalized on both sides: lowercase, no `www.`/`*.`/leading dot, IDN as punycode
(`пример.рф` → `xn--e1afmkfd.xn--p1ai`).

### No Shipped Rules

No rule ships with the code, and neither the engine nor `src/browser/` names a tool. Out of
the box nothing asks for approval: every call runs unless a rule you configure says
otherwise (`read_only` mode still denies non-`readOnlyHint` tools). MCP annotations cannot
tell page JavaScript from a click (Playwright MCP marks both `readOnlyHint: false,
destructiveHint: true`), so a guard for risky tools is a rule that names them. Ready-made
opt-in examples are commented out in `config.example.yaml`:

| Example id | Decision | Tools | Why you might want it |
|---|---|---|---|
| `page-js` | ask, `always_ask` | `browser_evaluate`, `browser_run_code`, `browser_run_code_unsafe` | arbitrary page JavaScript bypasses every other rule |
| `file-handoff` | ask | `browser_file_upload`, `browser_drop` | hands local files to the page |
| `sensitive-tool-names` | ask | names containing `payment`, `purchase`, `delete_account`, `credential` (any case) | the former name markers |

`EngineResources` built without a session engine (unit tests) gets
`PermissionEngine.from_settings()`: the code-default settings (no rules), never the personal
config. See the [ADR](../decisions/2026-10-01-name-free-permission-defaults.md).

## Resources: Domain and Target

`src/browser/permissions.py` (`BrowserResourceResolver`) gives rules two resources:

- **`domain`** — the call's `url` argument when it has one (`browser_navigate`,
  `browser_tabs` `new`, any other tool taking a `url`): the destination, never the current
  page. Otherwise the `- Page URL:` line of the latest tool output, else of the current
  snapshot. The resolver keys on the argument, not on tool names.
- **`target`** — what an element action acts on: the model's `element` description plus the
  snapshot line of the referenced element (`args.target`, or `ref`), e.g.
  `Buy button` + `button "Купить"`; for `browser_fill_form` every field's `name` and snapshot
  line. Joined with newlines, so a rule matches either source.

The target is a **heuristic guardrail**: the model writes `element`, the page writes the
accessible name, and both can be steered by injection. Use it to add an approval step on
purchases and submissions, not as a boundary:

```yaml
    - id: purchases
      decision: ask
      tool: browser_click
      target: '(?i)купить|оформить|оплатить|удалить|отправить|buy|pay|delete|submit'
```

## Approvals and Session Grants

In a terminal the CLI asks:

```text
Approval needed (rule purchases): browser_click on shop.example
  Approval required by rule purchases.
  args: {"element": "Buy button", "target": "e5"}
  [y] once   [s] session for browser_click on shop.example   [n] deny
```

- `y` runs this call; `n` ends the task `blocked`; `s` runs it and stores a **grant** for
  `(server, tool, domain)` until the session ends — later asks for the same tool on the same
  domain are allowed (`approval.resolved` with `by: grant`). A grant never covers another
  tool or domain, an `always_ask` rule, a hook ask, or a deny, and never outlives the
  session. `s` is not offered when it could not be stored.
- Answer at the `autobrowser>` prompt (the next line is the answer) or, during the startup
  `--task`, at `approve>`. Russian answers work too (`да`, `с`, `нет`).
- The wait is bounded at 80 % of `loop.progress_timeout_seconds` (the goal watchdog sees no
  events while you think); no answer is a deny.
- A `permission_request` hook (for example `src.harness.builtin_hooks:approve_tools`) can
  answer instead of the human — the way to pre-approve tools in a profile.

## Model Approval Judge

Rules only see strings. `permissions.approval_judge` lets models decide when to ask
([ADR](../decisions/2026-10-02-model-approval-judge.md), `src/agent_loop/execution/approval.py`):

```yaml
permissions:
  approval_judge: model          # off | model | classifier | both ("off" quoted in YAML)
  # classifier_model: qwen3:8b   # classifier only; unset = the session model
  classifier_timeout_seconds: 30.0
  rules: []                      # optional; rules and judges combine
```

| Mode | Who decides | Cost | Weak spot |
|---|---|---|---|
| `model` | The acting model fills the optional `approval_request` argument offered on every tool without `readOnlyHint` | none | a model persuaded by the page may skip it |
| `classifier` | A separate model call judges each state-changing call the rules let through (task, page URL, tool, arguments, target line) | one call per state-changing tool call | latency; may over-ask on ambiguous actions |
| `both` | Either one asking is enough; a model ask skips the classifier | as `classifier` minus model-flagged calls | — |

- The question names who asked and shows their reason, written for you:

  ```text
  Approval needed (asked by the model): browser_click on ozon.ru
    Оплата заказа на 9 826 ₽ (2 товара), способ оплаты: сохранённая карта.
    args: {"element": "Оплатить онлайн", "ref": "e412"}
    [y] once   [n] deny
  ```

- Judge asks are `always_ask`: no `s` (a `(server, tool, domain)` grant would silence the next
  payment), not lifted by `bypass`; under `dont_ask` (no TTY, batch) they are denies the model
  reads. They never lift a deny rule, and an `allow` rule or a session grant skips the
  classifier.
- A classifier failure, timeout or unreadable answer asks (fail closed); the
  `model.responded` event with `phase: approval` carries `needs_approval` and `error`.
- `permission.decided` reports `source: model` or `source: classifier`.
- The agent prompt tells the model that this gate exists, so it proceeds with purchases the
  user asked for instead of refusing up front, and leaves card numbers, CVV, passwords and
  one-time codes to the user (stop `blocked` with instructions).

## Debugging a Decision

Every evaluated call emits `permission.decided` — `tool`, `server`, `decision`, `source`
(`rule`, `hook`, `model`, `classifier`, `mode`, `annotation`, `grant`, `error`), `rule_id`, `mode`,
`reason`, **never the arguments**. Approvals emit `approval.requested` (with `rule_id`) and
`approval.resolved` (`decision`, `by: hook|human|grant`, `scope: once|session`).

```powershell
python scripts/replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

prints non-`allow` decisions under the action they gate:

```text
2. browser_navigate {"url": "https://evil.com"}
   permission [browser_navigate]: deny (shop-only) - Only the two shops are allowed.
```

`policy_block_count` in metrics, replay and exports counts permission denies (and hook
denies, and progress blocks). `LoopState.policy_event` carries `source` and `rule_id` of the
last gate decision. The session's mode is in `session.json` → `permissions.mode`.

Typical surprises:

- *A domain rule fires on every call* — the domain could not be resolved (no snapshot yet, a
  `data:` page, a changed snapshot format), so the rule fails closed. Check the `Page URL:`
  line in the snapshot.
- *An `allow` rule "does not work"* — some `ask`/`deny` also matches (they always win), or the
  rule needs a resource that is unknown (allow never matches then).
- *A hook says `allow` but the call is still blocked* — hooks run before the rules and
  cannot lift them.

## Second Layer: Sandbox

Rules are a logical layer; keep a real one underneath:

- **Profile isolation** — run Playwright MCP with `--isolated` or a dedicated
  `browser.user_data_dir` without saved logins, payment methods or sessions you would not
  give the agent. A logged-in profile is the largest risk a rule cannot remove.
- **Network origins** — Playwright MCP's `--allowed-origins` / `--blocked-origins` (blocked
  wins) restrict what the browser loads. Its documentation calls them a convenience, not a
  security boundary (they do not cover redirects), so use them together with
  `not_domains` rules, not instead of them.
- **Files** — keep the Playwright MCP file roots narrow; `browser_file_upload` already asks.

The `url_policy` hook (`src/browser/hooks.py`) keeps working for `browser_navigate`; for new
setups prefer `domains`/`not_domains` rules — they cover every browser tool, carry a rule id
in the audit trail and are evaluated deny-first with everything else.

## Testing

Build the engine from explicit settings, never from `get_settings()` (a personal
`config.yaml` would leak in):

```python
from src.config import PermissionRule, PermissionsSettings
from src.harness.permissions import PermissionEngine

engine = PermissionEngine.from_settings(
    PermissionsSettings(mode="dont_ask", rules=[PermissionRule(id="r", decision="deny", tool="x")])
)
```

and pass it as `EngineResources.from_harness(..., permissions=engine)`. Reference tests:
`tests/test_permissions.py` (engine), `tests/test_browser_permissions.py` (resolver,
configured browser rules), `tests/test_agent_loop_permissions.py` (loop order, approvals, grants),
`tests/test_cli_approval.py` (CLI prompt, modes), and the eval scenarios
`purchase_click_denied_*` (`permissions:` and `human: {answers: [...]}` blocks).
