# ADR: Model-Based Approval Judge

Status: Proposed
Date: 2026-10-02

## Context

The [PermissionEngine](2026-10-01-permission-engine.md) is deterministic and, after
[name-free defaults](2026-10-01-name-free-permission-defaults.md), ships no rules: what is
risky is only what the user writes as regular expressions over tool names, arguments and the
target's snapshot line. That keeps the engine honest, but it makes "ask me before paying"
a matter of guessing button labels (`Перейти к оформлению` slips past `оформить\s+заказ`),
and it cannot see the meaning of an action reached another way (Enter instead of a click,
a direct checkout URL, page JavaScript).

A live session also showed the opposite failure: asked to add jackets to the cart and pay,
the acting model refused on its first turn (`{"decision":"blocked"}`) before any tool call,
so the permission engine never ran and the user was never asked. The system prompt did not
tell the model that a human approval gate exists, so the model treated safety as its own job.

## Decision

1. **Prompts (variant A).** The agent and planner prompts say the agent acts on the user's
   behalf, that user-requested purchases and account actions are legitimate, that a human
   approval gate sits in front of risky calls, and that `blocked` is only for tasks that
   cannot be completed — never for "this seems risky". Card numbers, CVV, passwords and
   one-time codes stay with the user.
2. **`permissions.approval_judge: off | model | classifier | both`** (default `off`) adds
   two model-based sources of an `ask` that need no rule:
   - **model** — every tool without the MCP `readOnlyHint` is offered an optional
     `approval_request` string argument. The acting model fills it with one sentence for the
     user ("Оплата заказа на 9 826 ₽") when the call spends money, commits an order, sends data
     or changes the account. The loop strips it when it parses the call (before history, repeat
     tracking, the progress guard, hooks, events and the tool) and passes it as
     `PermissionCheck.model_ask_reason`. No extra model call.
   - **classifier** — a separate, stateless model call (`ApprovalClassifier`, optional
     `permissions.classifier_model`) judges each state-changing call that the engine would
     allow only by default. It sees the task, the page URL, the tool and its description, the
     arguments and the snapshot line of the target, and answers
     `{"approval": bool, "reason": ...}`. A failure, timeout or unreadable answer asks
     (fail closed). Its "needs approval" is `PermissionCheck.classifier_ask_reason`.
3. **The engine stays deterministic.** `PermissionEngine` never calls a model; it decides on
   the check, which now carries the judgments. A model/classifier ask behaves like a hook ask:
   it only adds an approval, never lifts a deny rule, is `always_ask` (no session grant, not
   bypassed by `bypass`), and becomes a non-terminal deny in `dont_ask`. `permission.decided`
   reports `source: model | classifier`; the classifier call emits
   `model.requested`/`model.responded` with `phase: approval`.
4. The classifier is skipped when the rules already decided (deny, ask, an `allow` rule, a
   session grant), for `readOnlyHint` tools, and when the model itself asked.

Still no tool names anywhere in the code: the model argument goes to every tool without
`readOnlyHint`, and the classifier prompt describes consequences (money, orders, personal
data, publishing, account changes), not tools. Nothing asks out of the box.

## Consequences

- Purchases become possible with a human in the loop: the model proceeds, the user approves
  the irreversible step with a reason written for them.
- The model judge is free but only as reliable as the acting model; a page that persuades the
  model can also persuade it to omit the argument. The classifier is harder to steer (it sees
  the action, not the page) but costs one model call per state-changing tool call
  (latency and tokens) and can over-ask on ambiguous actions.
- Deterministic rules remain available and combine with both judges (any one asking is
  enough); they are the only guard that does not depend on a model.
- `BrowserResourceResolver` now also returns the full `url` (informational; rules do not
  match on it) so the classifier sees paths such as `/gocheckout`.
- `LoopState` gains the task-local `approval_request` field.

## Alternatives Considered

- **Only rules.** Rejected as the sole mechanism: regexes over labels miss renamed buttons and
  other routes to the same action, and every site needs its own list.
- **A separate `request_approval` tool the model calls before the risky call.** Rejected:
  pairing a grant with "the next call" is fragile (the model may change the call in between);
  an argument on the call itself binds the request to exactly that call.
- **Let the model's judgment lift rules or skip asks.** Rejected: a model can only add
  approvals, so prompt injection can at worst suppress the model's own ask, never a rule's.
- **Session grants for judge asks.** Rejected for now: a grant is per `(server, tool, domain)`,
  so approving one click "for the session" would silence the next payment click.

## Related

- `src/agent_loop/execution/approval.py`, `src/agent_loop/prompts.py`,
  `src/harness/permissions.py`, `src/agent_loop/execution/loop.py`
- Guide: [Permissions](../development/permissions.md#model-approval-judge)
- [2026-10-01 PermissionEngine](2026-10-01-permission-engine.md),
  [2026-10-01 Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
