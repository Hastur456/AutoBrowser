# Agent Runtime Flow

The engine-native control flow owned by `AgentLoopEngine`
(`src/agent_loop/execution/loop.py`), driven through `TurnController` for one goal run, in
three views: the run, one turn, and the pipeline of one tool call. There is no compiled graph.

## The Run

```mermaid
flowchart TD
  GR[GoalRunner] --> RUN[AgentLoopEngine.run]
  RUN --> GS{{"hook: goal_start"}}
  GS -->|deny| RES
  GS -->|"replay with a recorded plan"| SEED["recorded plan<br/>no model call"]
  GS -->|otherwise| B0{"run budget left?"}
  B0 -->|"no: budget_exhausted"| RES
  B0 -->|yes| PLAN["plan: model call #0<br/>(tier 0)"]
  SEED --> TOP
  PLAN --> TOP(["loop top"])
  TOP --> TIER{{"tier_escalation<br/>(several tiers only)"}}
  TIER -->|"stuck: ladder.escalated,<br/>model_tier + 1"| CAP
  TIER -->|"no change"| CAP{"agent turns<br/>within turn_cap?"}
  CAP -->|"no: turn_cap"| RES
  CAP -->|yes| TURN[["one turn<br/>(next diagram)"]]
  TURN -->|"continue"| TOP
  TURN -->|"replan: budget left"| REPLAN["rebuild the plan<br/>(current tier)"]
  REPLAN --> TOP
  TURN -->|"terminal: done / blocked / cancelled"| RES[AgentLoopResult]
  RES --> GE{{"hook: goal_end"}}
  GE -->|"status, final_answer, session_state"| SR[SessionRuntime]
```

## One Turn

```mermaid
flowchart TD
  T([TurnController.run_turn]) --> G{"pre-turn guards<br/>(limits, replans, failures)"}
  G -->|terminal| TERM([terminal])
  G --> STAGE{"ladder_stage"}
  STAGE -->|"playback / retarget"| SRC[["StepSource.next"]]
  SRC -->|"StepProposal: synthetic<br/>assistant tool call"| TOOL[["tool pipeline<br/>(next diagram)"]]
  SRC -->|"StepFinal"| STOP
  SRC -->|"StepHandoff, max_stage<br/>below agent"| TERM
  SRC -->|"StepHandoff: harness note,<br/>ladder.escalated"| BUD
  STAGE -->|agent| BUD{"run budget left?"}
  BUD -->|"no: budget_exhausted"| TERM
  BUD -->|yes| MODEL["model turn on<br/>tiers[model_tier]"]
  MODEL -->|"empty answer and a higher tier:<br/>tier_signal no_action"| CONT([continue])
  MODEL -->|tool_call| TOOL
  MODEL -->|replan| REP([replan])
  MODEL -->|done| STOP{{"hook: stop"}}
  STOP -->|deny| CONT
  STOP -->|"no deny"| TERM
  TOOL -->|"observed, or blocked<br/>(the model reads why)"| CONT
  TOOL -->|"refused approval"| TERM
```

## One Tool Call

```mermaid
flowchart TD
  REQ([tool request]) --> PG{"progress guard"}
  PG -->|"blocked: repeated identical outcome"| BACK([back to the model])
  PG --> PREP[ToolBroker.prepare]
  PREP -->|"unknown tool"| INV
  PREP --> PRE{{"hook: pre_tool_use"}}
  PRE -->|deny| BACK
  PRE -->|"allow / ask / rewrite"| PERM["PermissionEngine.evaluate<br/>(final arguments)"]
  PERM -->|deny| BACK
  PERM -->|allow| JUDGE{"approval judge on and<br/>a state-changing call?"}
  JUDGE -->|no| INV
  JUDGE -->|"classifier: safe"| INV
  JUDGE -->|"needs approval"| PR
  PERM -->|ask| PR{{"hook: permission_request"}}
  PR -->|allow| INV
  PR -->|deny| REFUSED([refused: blocked])
  PR -->|"no decision"| HUMAN["human: once / session / deny"]
  HUMAN -->|deny| REFUSED
  HUMAN -->|"once / session (grant)"| INV[ToolBroker.invoke]
  INV --> POST{{"hook: post_tool_use /<br/>post_tool_use_failure"}}
  POST --> OBS["observe: snapshots per server,<br/>action journal, usage"]
  OBS --> BACK
```

`AgentLoopEngine` builds the initial plan, then runs a bounded `while` loop of
`TurnController` turns. A turn checks progress and permissions before execution, observes the tool
result, and either continues, replans, or reaches a terminal status that becomes
the `AgentLoopResult` (`status` is always `done`/`blocked`/`cancelled`).
`GoalRunner` keeps the one-task lifecycle (timeouts, watchdog, goal events)
outside the engine; `SessionRuntime` injects carried state and persists the
result's `session_state` between tasks.

Progress tracking is server-neutral. `observe` appends every executed call to the
task-local action journal (`LoopState.action_history`, `execution/progress.py`),
keyed by tool + arguments and by result, and adds a repeat note when a call
reproduces an earlier identical outcome. The turn prompt renders the journal as
the `Action History` block. The progress guard (`guards.progress_block_reason`, event
`policy.decided`) skips a call that already returned the identical result
`settings.loop.max_ineffective_actions` times; the model reads the reason as the tool output. The
terminal status is read from the explicit `LoopState.completion_status` set by
whoever ended the run, so loop-protection stops and model `blocked`/`failed`
stops end as `blocked`, never as `done`.

Playback ([replay guide](../development/replay.md)) adds a branch before the model: with
`TaskRequest.profile.playback` the session's step source (`EngineResources.steps`) supplies the
recorded plan (no planner call) and proposes each recorded step as a synthetic assistant tool
call, which then takes the same path as a model call. After the tool turn the source observes
the result (or the block reason). A `StepHandoff` hands the rest of the task to the model in the
same turn with a `[harness]` note, unless `RunProfile.max_stage` is below `agent` (then
`ladder_cap`); a repeat-guard replan during playback becomes a handoff too. Replayed turns do
not count toward `loop.turn_cap` and are bounded by the recording. Every model call (plan,
agent turn, approval classifier, retarget) is added to `LoopState.usage`, and the run budget is
checked right before each one (`budget_exhausted`).

The model cascade (`cascade.*`, [serve mode guide](../development/serve-mode.md)) adds one
check at the top of every loop iteration: with more than one tier in `EngineResources.models`,
the pure `guards.tier_escalation` moves the agent up one tier after an empty answer, a
progress-guard block, `escalate_after_failures` failed calls in a row or
`escalate_after_replans` replans since the last change, and gives the new tier a fresh failure
window. Call #0 always runs on tier 0, a replan on the current tier. With one tier the check
is skipped and every event is unchanged.

Lifecycle hooks (`src/harness/hooks.py`, see the
[ADR](../decisions/2026-09-30-lifecycle-hooks-engine.md)) sit at the hexagon points and
are skipped entirely when hooks are disabled. `pre_tool_use` runs after the progress
guard and never after its `blocked`. `post_tool_use` may rewrite the
output before `observe`; hook context becomes a separate `[harness]` message after it.
`stop` runs only for a model `done` with status `done` (never for guard terminals); a
deny turns the turn into `decision: "continue"`, bounded by `hooks.max_stop_blocks` and
the turn cap. Every handler run emits one `hook.decided` event.

Authorization is the session's `PermissionEngine` (`src/harness/permissions.py`, see the
[ADR](../decisions/2026-10-01-permission-engine.md)), evaluated **after** `pre_tool_use` on
the final arguments and emitted as `permission.decided`. Rules resolve `deny > ask > allow`;
a hook `allow` cannot lift a rule, a hook `ask` is escalated through the same mode logic. A
permission `deny` is not terminal: the model reads the reason and may take another path. An
`ask` emits `approval.requested`; `permission_request` hooks may stand in for the human, and
the human answers `once`, `session` (stores a `(server, tool, domain)` grant) or `deny`
(terminal `blocked`). Every resolved approval emits `approval.resolved`.
With `permissions.approval_judge` on
([ADR](../decisions/2026-10-02-model-approval-judge.md)), the model's own `approval_request`
argument (stripped when the call is parsed) and the approval classifier can turn a default
`allow` into an `always_ask`; neither can lift a rule.
