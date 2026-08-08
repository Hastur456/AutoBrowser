# 2026-08-08 Agent Loop Engine Development Plan

Branch: `feat/agent-loop-engine`
Sources:
- [2026-08-06 AgentLoopEngine Architecture Review](../research/2026-08-06-agentloopengine-architecture-review.md)
- [2026-07-26 Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md)
Supporting note:
- [2026-08-08 Agent Loop Engine Migration Touchpoints](2026-08-08-agent-loop-engine-migration-touchpoints.md)

Status: Planned

## Goal

Implement the `AgentLoopEngine` itself: an explicit runtime loop that owns
turn repetition, model invocation, action parsing, tool execution handoff,
observation ingestion, and terminal result production for one goal.

This plan is only about the loop and the minimal adapters it needs to replace
graph control flow. Components that are intentionally out of scope for this
phase include permission engine redesign, hooks, skills, subagents, service
API, and UI control plane. Those can follow after v2 is working.

Target shape:

```text
SessionRuntime
  -> GoalRunner
      -> AgentLoopEngine
          -> ContextAssembler
          -> ModelDriver
          -> ActionParser
          -> existing ToolRegistry / PolicyEngine / Observer adapters
          -> CompletionController
```

The loop should be introduced incrementally and kept behind a feature flag
until parity is proven.

## Working Rules

- Keep LangGraph as the active fallback until v2 passes scenario parity.
- Do not add new autonomy features while building the loop core.
- Do not fold permission-engine refactors into this plan.
- Preserve current CLI behavior while switching the execution core.
- Prefer small commits that isolate one boundary at a time.

## Commit Plan

### Commit 1: Architecture Fence

Goal: freeze ownership before moving code.

Deliverables:

- update architecture docs to name `AgentLoopEngine` as the target runtime;
- keep LangGraph documented as v1 compatibility;
- align glossary terms for loop, turn, action, and result if needed;
- keep the migration touchpoints note current.

Exit gate:

- docs agree on ownership boundaries;
- no runtime behavior changes.

### Commit 2: Event Stream And Trace Envelope

Goal: make the loop observable before changing control flow.

Deliverables:

- typed event records for session, goal, turn, action, model, tool, and
  observation lifecycle;
- durable JSONL trace output under `.autobrowser/sessions/<session_id>/`;
- in-memory sink for tests;
- redaction and JSON-safety checks;
- compact trace replay helper.

Exit gate:

- traces are durable and readable after exit;
- existing CLI behavior stays the same.

### Commit 3: Scenario Eval Baseline

Goal: lock current behavior with replayable scenarios before the engine swap.

Deliverables:

- deterministic fake-browser scenarios for search, stale ref recovery,
  repeated action loops, dynamic search input, and filter/apply flows;
- scenario runner and metrics;
- baseline file for current v1 behavior;
- compact failure output from traces.

Exit gate:

- v1 scenarios run from the current loop;
- failures are explicit and reproducible.

### Commit 4: Context Split

Goal: separate prompt assembly from initial state construction.

Deliverables:

- `InitialStateFactory` or equivalent;
- `ContextAssembler` for per-turn model-visible context;
- browser-only context gated by browser relevance;
- compatibility rendering for the current prompt path.

Exit gate:

- non-browser tasks do not receive browser-only context;
- prompt coverage stays green.

### Commit 5: Action Contract And Model Driver

Goal: make model output explicit before routing it through the engine.

Deliverables:

- `ProposedAction` / `ActionBatch` / normalized model response types;
- `ModelDriver` boundary;
- response classification into final, tool, ask-user, replan, and invalid
  outputs;
- adapter path that still feeds the current graph during migration.

Exit gate:

- tool calls and final answers can be represented without legacy state
  mutation;
- eval baseline does not regress.

### Commit 6: Observation And Completion Split

Goal: make loop state updates explicit.

Deliverables:

- observation normalization from tool results;
- browser state reduction;
- progress / ineffective-action detection;
- completion controller for terminal status decisions;
- explicit result object for the next turn or final exit.

Exit gate:

- browser-specific safety behavior still holds;
- replayed traces show the same visible progression.

### Commit 7: Engine Shell

Goal: introduce the explicit engine loop behind a feature flag.

Deliverables:

- `AgentLoopEngine` with turn repetition and terminal results;
- `TurnController` or equivalent per-model-turn boundary;
- typed `AgentLoopResult`;
- feature flag for `v1` vs `v2`.

Exit gate:

- v2 can run the same fake-browser scenarios;
- v1 remains the rollback path.

### Commit 8: Parity And Default Switch

Goal: make v2 the default only after parity is proven.

Deliverables:

- parity tests against v1 baseline;
- cleanup of legacy outcome inspection;
- docs updated to describe the new default runtime;
- touchpoints note reduced to remaining migration debt.

Exit gate:

- default runtime is the engine loop;
- LangGraph is compatibility only.

## Suggested Slice Ordering

Use this execution order if the work is split across multiple PRs:

1. Commit 1
2. Commit 2
3. Commit 3
4. Commit 4
5. Commit 5
6. Commit 6
7. Commit 7
8. Commit 8

This keeps observability and evals ahead of the actual engine switch.

## Code Ownership Map

Keep these boundaries stable while implementing:

- `SessionRuntime`: process/session lifecycle, not task solving.
- `GoalRunner`: one goal lifecycle, not action selection.
- `AgentLoopEngine`: repeated model/tool loop only.
- `ContextAssembler`: model-visible context assembly.
- `ModelDriver`: provider/model invocation.
- `ActionParser`: normalize model output into actions.
- `CompletionController`: terminal status decisions.

The current `ToolRegistry`, `PolicyEngine`, and observer code remain the
adapters the engine uses at first. Their internal redesign is a follow-up
phase, not part of this plan.

## Exit Criteria

The migration is complete when:

- `AgentLoopEngine` is the default runtime;
- the current LangGraph path is only compatibility or fallback;
- durable events exist for every goal and turn;
- scenario evals cover the known browser failure modes;
- prompt/context assembly is explicit and testable;
- docs and glossary match the implemented ownership model.

## Open Checks

Before starting code, confirm these points in the repo:

- current file names under `src/agent_loop/` still match the plan;
- `docs/glossary.md` has the stable terms you want to keep;
- existing eval scenarios are enough to serve as v1 baseline;
- the feature flag name is still `AUTOBROWSER_AGENT_LOOP`.
