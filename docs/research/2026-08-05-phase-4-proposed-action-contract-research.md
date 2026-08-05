# Phase 4 ProposedAction Contract Research

Status: Research
Date: 2026-08-05

## Question

Phase 4 of the Codex-Claude migration plan asks for an explicit
`ProposedAction` contract and a `ModelDriver` that can represent model output as
typed actions instead of direct state mutation.

This note records how that maps onto the current AutoBrowser codebase and what
the lowest-risk transition should look like before the LangGraph path is
replaced.

## Sources Checked

- `src/agent/agent.py`
- `src/agent/nodes.py`
- `src/agent/utils.py`
- `src/agent/state.py`
- `src/agent_loop/events.py`
- `src/agent_loop/goals.py`
- `src/agent_loop/outcomes.py`
- `src/agent_loop/tracing.py`
- `src/browser/contracts.py`
- `tests/test_agent_graph.py`
- `tests/test_browser_contracts.py`
- `docs/research/2026-07-26-codex-claude-runtime-migration-plan.md`

## Current Transition Point

There is no standalone `AgentLoop` engine yet. The current reasoning boundary is
`src/agent/nodes.py:create_agent_node()`, which turns an LLM response into one
of the legacy graph updates:

- `decision: "tool_call"` with a `tool_request`;
- `decision: "replan"` with an observation;
- `decision: "done"` with a `final_answer`.

That node already handles:

- native tool calls via `response.tool_calls`;
- JSON fallback parsing from model text;
- stale-snapshot and repeated-action guards;
- compatibility with the existing LangGraph routing.

So the safest phase 4 move is not to change graph behavior. It is to introduce
typed actions underneath the current node and convert them back into the same
legacy updates.

## Proposed Contract Shape

The action layer should be typed and discriminated, with a small base contract
plus specialized action records:

- `AnswerAction`
- `ToolCallAction`
- `AskUserAction`
- `UpdatePlanAction`
- `DelegateAction`
- `CompactMemoryAction`
- `StopAction`

For the current code, the useful mapping is:

- `done` -> `AnswerAction`
- `tool_call` -> `ToolCallAction`
- `replan` -> `UpdatePlanAction`

That keeps the legacy graph semantics intact while giving the new runtime a
stable action vocabulary.

## Recommended Runtime Boundary

The new layer should sit between the model response and the graph update:

```text
LLM response
  -> ActionParser
  -> ProposedAction
  -> legacy adapter
  -> current AgentState update
```

This keeps `create_agent_node()` behavior unchanged for now, while making the
typed action contract reusable by the future explicit loop.

## Why This Is The Right Cut

The current code already has stable concepts that should remain untouched for
phase 4:

- `AgentState` remains the graph state contract.
- `PolicyEngine` still gates tool requests.
- `observe_node` still compiles tool output into browser state.
- `GoalRunner` still owns session/task lifecycle.
- `EventEmitter` and `EventRecord` already provide durable trace plumbing.

The action contract should not replace those pieces yet. It should only make the
model’s intent explicit before state mutation happens.

## Open Questions

- Should `StopAction` represent only explicit terminal stops, or also invalid
  model output that must be turned into a blocked/retry path?
- Should `UpdatePlanAction` carry a full plan replacement, a partial patch, or
  just a reason string in the first cut?
- Should the adapter preserve the old `decision` field for compatibility until
  the LangGraph route is retired?
- Should invalid JSON from the model become a typed `StopAction`, a retryable
  parse error, or a blocked event in the event stream?

## Recommendation

Implement `ProposedAction` as a typed contract in `src/agent_loop/actions.py`,
add a `ModelDriver` plus parser in `src/agent_loop/model.py`, and use a thin
adapter in `src/agent_loop/adapters/langgraph.py` to translate those actions
back into the existing `create_agent_node()` updates.

That gives phase 4 a real contract without changing current task behavior.
