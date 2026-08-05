# 2026-08-05 Agent Loop Legacy Outcomes Cleanup

## Context

`src/agent_loop/outcomes.py` is a transitional compatibility layer between
`GoalRunner` and the current LangGraph-shaped agent output.

The file currently adapts legacy `AgentState`-style terminal data into a
provider-neutral `GoalState` so `GoalRunner` can emit goal lifecycle events
without owning the model/action loop. This is acceptable only while
`BrowserHarness` still invokes the old LangGraph Agent Loop and returns
terminal state through fields such as `final_answer` and `decision`.

## Legacy Items To Remove

After the migration to the new Agent Loop is complete, remove the transitional
compatibility layer from `outcomes.py`:

- `LegacyAgentStateObservationCompiler`
- `CompletionGuard`
- `GoalStateCompletionGuard`
- `_find_terminal_agent_state()`
- `_completion_status_from_agent_state()`

## Reason

`outcomes.py` exists only to keep `GoalRunner` compatible with the old
LangGraph Agent Loop. The legacy adapter currently:

- knows the internal structure of `AgentState`;
- searches nested results for `final_answer`;
- interprets `decision`;
- converts old graph state into provider-neutral `GoalState`.

That knowledge does not belong in `GoalRunner` or in the goal lifecycle
boundary. `GoalRunner` should not inspect agent-internal state to decide
semantic completion.

## Target Architecture

The new Agent Loop should return provider-neutral terminal state directly:
`GoalState` or an equivalent typed result with explicit completion status,
latest state, and result payload.

After that migration, `GoalRunner` should:

- work only with the new Agent Loop terminal result;
- emit lifecycle events from explicit terminal status;
- avoid any dependency on legacy `AgentState`;
- avoid searching for `final_answer`;
- avoid interpreting `decision`;
- avoid `CompletionGuard` and observation compiler adapters.

The expected runtime direction is:

```text
SessionRuntime
  -> GoalRunner
      -> AgentLoopEngine
          -> provider-neutral terminal GoalState
```

At that point, `outcomes.py` can either contain only stable provider-neutral
types that are still shared by the new loop, or be deleted entirely if those
types move into the new engine/result module.

## Migration Gate

Do not remove `outcomes.py` until all of these are true:

- the active Agent Loop returns a provider-neutral terminal state directly;
- `GoalRunner` no longer accepts or constructs `ObservationCompiler`;
- `GoalRunner` no longer accepts or invokes `CompletionGuard`;
- tests no longer import `LegacyAgentStateObservationCompiler`,
  `CompletionGuard`, or `GoalStateCompletionGuard`;
- tests cover `completed`, `blocked`, `cancelled`, and non-terminal failure
  handling through the new terminal result contract;
- trace/replay/metrics consumers have a documented source for terminal status
  and final user-facing answer.

## Affected Docs

Keep these documents aligned while migrating:

- [Architecture Overview](../architecture/overview.md)
- [GoalRunner Branch Plan](2026-08-01-goal-runner-branch-plan.md)
- [Batch And Export Data Contracts](2026-07-30-batch-export-data-contracts.md)
- [Glossary](../glossary.md)
