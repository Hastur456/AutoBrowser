# Replay Guide

How AutoBrowser makes a repeated task nearly free: record a completed run once, then replay
it without the model, and let a model step in only where the page changed. The decision
record is [ADR-2026-11-02: Serve Mode, Cost Ladder and Replay](../decisions/2026-11-02-serve-mode-and-cost-ladder.md);
the design is the [serve mode design](2026-11-02-serve-mode-design.md) (§5–§6) and the
rollout is the [phase 1 plan](2026-11-02-serve-mode-phase1-plan.md).

In code, replaying is **playback** and the artifact is a **recording**; `autobrowser replay`
is the older trace viewer and has nothing to do with it. The settings section is `replay`.

## The Cost Ladder

A run climbs only as far as it has to, and never above `RunProfile.max_stage`:

| Stage | Who acts | Model calls |
| --- | --- | --- |
| `playback` (1) | the recording's steps, references rebound to the current page | 0 |
| `retarget` (2) | one model call re-chooses the arguments of one diverged step | 1 per diverged step |
| `agent` (3) | the agent finishes the task from where playback stopped, with the replayed history | as usual |
| `human` (4) | approvals, as in any task | — |

Stage 0 (`probe`) belongs to the stand (`autobrowser serve`, [serve mode guide](serve-mode.md)). Every step of every
stage goes through the normal pipeline: progress guard, `pre_tool_use` hooks, permissions,
approvals, the broker, `post_tool_use` and observation. A replay never skips a rule.

## Switching It On

```yaml
replay:
  enabled: true            # off by default
```

or `AUTOBROWSER_REPLAY__ENABLED=true`. Every field of the section is in
`config.example.yaml` and `.env.example`: thresholds (`checkpoint_min_similarity`,
`checkpoint_url`), the descriptor context (`describe_context_lines`), retargeting
(`retarget_enabled`, `max_retargets_per_run`, `retarget_context_chars`,
`retarget_timeout_seconds`), `max_steps`, `default_answer`, `keep_previous`.

## Recording a Run

```powershell
uv run main.py -p --record price-watch "Open https://shop.example/item/1 and tell me the price"
uv run main.py --record price-watch          # the first task of the terminal UI
```

In the terminal UI or plain mode, `/record <name>` records the **next** task, `/record off`
disarms and `/record` shows what is armed. `--record` does not work with `--classic`.

A recording is saved after `goal_end`, and only when:

- the task ended `completed` (so `stop` hooks, if any, accepted the answer);
- it made at least one successful tool call and at most `replay.max_steps`.

What goes in: the successful tool calls with the arguments the model (or a `pre_tool_use`
hook) gave, the plan, a checkpoint fingerprint of each output and the catalog entry (server,
`readOnlyHint`, schema digest) of each tool used. What stays out: failed calls, calls a
guard, hook or permission blocked, the session's own `memory` and `skills` tools, and any
secret: arguments go through the event redaction, a step that types into a field whose page
label matches `replay.secret_field_pattern` (password, PIN, CVV, token, ...) keeps no typed value, and a step with a
redacted value is marked and never replayed blindly (logins belong in the browser profile). Fingerprints are hashes,
so a recording keeps no page text.

Names are 1–64 lowercase letters, digits and hyphens. Files live in
`<home>/recordings/<name>/`: `recording.json`, `previous.json` (the replaced one, with
`keep_previous`) and `stats.json` (playback counters). `recording.saved` /
`recording.skipped{reason}` in the session's `events.jsonl` say what happened; a failed save
never fails the task.

## Replaying

```powershell
uv run main.py -p --recording price-watch                       # the recorded task
uv run main.py -p --recording price-watch --answer output --output-format json
```

The recorded plan replaces the planner call, so a clean replay makes **no model call**.
`--answer` (default: the recording's own, `replay.default_answer` when it was saved) decides
how the task ends after the last step:

- `agent` — one agent turn answers the task from the current page;
- `output` — the region of the last tool output becomes the answer (no model);
- `summary` — a fixed "Replayed N recorded steps" (no model; for action-only jobs).

The json result carries `stop_reason` (`playback_completed` when playback finished the task),
`stage` and `usage`. Replay in the terminal UI is not in v1 (Q15 of the design).

### How a Step Is Rebound

Element references such as `[ref=e42]` are valid only until the page changes. When recording,
any argument value that the page text before the step shows as `[ref=V]` becomes a binding
with a descriptor of its snapshot line: role, accessible name, position among equal lines,
the nearest named ancestors and the frame prefix. Before replaying the step, the binding is
rebound to the one current line with the same role and name (a changed number in the name,
such as a price, still matches), narrowed by context, then by position while the count of
equal lines is unchanged; frame prefixes change between loads and only break ties. None or
several matching lines is a divergence **before** the call. Only `src/browser/playback.py`
knows this format; other servers use the plain-text adapter (no bindings).

### Divergences and Handoff

| Kind | When | Next |
| --- | --- | --- |
| `catalog` | the tool is gone, or its schema digest or `readOnlyHint` changed | agent |
| `binding` | no unique element for a reference, or a redacted step | retarget, else agent |
| `tool_error` | the replayed call failed | retarget once, else agent |
| `checkpoint` | the output after the step is below `checkpoint_min_similarity`, or the URL path differs | agent (the action already ran) |
| `denied` | a guard, hook or permission blocked the step | agent (reads the reason) |
| `repeat` | the engine's repeat guard stopped an identical replayed call | agent |

A handoff adds one `[harness]` note and the model continues in the same turn with the
replayed calls and their outputs in its history. With `max_stage: playback` a handoff ends
the task `blocked` with `stop_reason=ladder_cap` instead. A refused approval stays terminal
`blocked`, as in any task.

The retarget model call sees only the recorded tool, the task, the recorded step with its
element descriptions, the reason and the current page (cut to `retarget_context_chars`). Its
answer counts only as exactly one call of that tool whose reference arguments are live
references of the current page. The prompt is `PLAYBACK_RETARGET_SYSTEM_PROMPT` in
`src/agent_loop/prompts.py`.

The retarget runs on the session model unless a model cascade is on: then on
`cascade.retarget_tier` (0 = the session model, 1.. = a tier of `cascade.tiers`), with that
tier's label and price in `usage` and `tier` in its `model.*` events
([serve mode guide](serve-mode.md#model-cascade)).

## Budgets and Cost

Every task reports `usage`: model calls, input and output tokens, cost, and buckets by ladder
stage and by model (`goal.*` events, `TaskOutcome`, print-mode json, batch rows). Cost needs a
price:

```yaml
llm:
  prices:
    gemma4:31b-cloud: {input_per_mtok: 0.2, output_per_mtok: 0.6}
  currency: USD
loop:
  max_task_model_calls: 30      # None by default: no cap
  max_task_tokens: null
  max_task_cost: 0.05           # in llm.currency
```

The budget is checked right before each model call (the plan, a replan, an agent turn, a
retarget); exhaustion ends the task `blocked` with `stop_reason=budget_exhausted`. Replayed
steps call no model and are never blocked by it. A call without reported tokens or without a
price makes the cost unknown (`None`); the money cap is then not checked and the call cap
still is — set `max_task_model_calls` when you rely on a money cap. The approval classifier's
calls count as calls with an unknown cost; with no budget left for it, the classifier does not
run and the call goes to the human instead (fail closed). Memory consolidation runs after the
task and is outside its budget. `stats.json` counts each replay with the stage the task ended
at and the divergence that stopped playback.

## Debugging

- `autobrowser recordings list | show <name> | rm <name>` — what is stored, step by step,
  with reference counts and redacted marks, and how often it was replayed.
- Events of a replayed task: `playback.started`, `action.proposed{source: playback|retarget}`,
  `playback.step{step, stage, binding, status}`, `playback.diverged{step, kind, detail}`,
  `ladder.escalated{from, to, reason, step}`, `model.*{phase: retarget}`.
- Evals: `uv run scripts/run_evals.py --scenarios tests\evals\playback_scenarios --baseline
  tests\evals\baselines\playback_v1.json` pins the cost of replay scenarios (a clean replay
  makes zero model calls).

## Code Map

| Piece | Where |
| --- | --- |
| Contracts (`RunProfile`, `StepSource`, `PlaybackAdapter`, `TaskUsage`) | `src/contracts.py` |
| Recording format | `src/agent_loop/recordings.py` |
| Store | `src/harness/recording_store.py` |
| Recorder (event sink) | `src/harness/recorder.py` |
| Step source, factory, retargeter, plain-text adapter | `src/harness/playback.py` |
| Browser adapter (snapshot format) | `src/browser/playback.py` |
| Engine branch, handoff, budgets | `src/agent_loop/execution/loop.py`, `usage.py` |
| Session wiring, `run_task(profile)` | `src/harness/session.py` |
| CLI and UI | `src/cli/bootstrap.py`, `src/cli/subcommands.py`, `src/ui/controller.py`, `src/ui/tui/commands.py` |
