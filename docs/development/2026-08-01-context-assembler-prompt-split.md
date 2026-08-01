# 2026-08-01 Context Assembler And Prompt Split Plan

Branch: `feat/context-assembler-prompt-split`
Source plan: [Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md)
Status: Planned

## Goal

Implement Phase 3 of the runtime migration: replace the current monolithic
agent prompt with an explicit, testable context assembly boundary while keeping
the LangGraph v1 loop behavior unchanged by default.

The desired result is not a shorter prompt by itself. The desired result is a
runtime-owned way to decide which context blocks the model receives for a given
task:

- core runtime instructions;
- user task;
- plan or todo state;
- recent transcript summary;
- current observation;
- current tool inventory;
- active skills or rule resources;
- policy hints;
- browser state when browser tools are relevant.

## Current State

Prompt and context ownership is currently split across:

| Area | Current file | Notes |
| --- | --- | --- |
| System prompt | `src/agent/prompts.py` | `AGENT_SYSTEM_PROMPT` contains core reasoning rules, browser contract, loop guards, completion rules, and output format rules. |
| Per-turn user prompt | `src/agent/prompts.py` | `AGENT_USER_PROMPT` renders task, plan, current step, observation, counters, snapshot, and refs. |
| Session prompt injection | `src/harness/context.py` | `ContextBuilder.get_system_prompt()` returns one system prompt for durable message history. |
| Initial graph state | `src/harness/context.py` | `ContextBuilder.build_initial_state()` builds only `{"task": task}` plus overrides. |
| Agent prompt use | `src/agent/nodes.py` | `create_agent_node()` calls `history_builder(state)` and appends one formatted `AGENT_USER_PROMPT`. |
| Browser rule resource | `docs/development/browser-agent-rules.md` | Existing development doc already isolates many snapshot/ref/search rules. |
| Trace and eval foundation | `src/agent_loop/events.py`, `src/agent_loop/evals.py` | Phase 1/2 infrastructure exists and should be used to protect behavior. |

This phase should introduce `src/agent_loop/context.py`,
`src/agent_loop/prompts.py`, and `src/agent_loop/skills.py` as runtime-facing
boundaries, but keep the graph path as the execution engine.

## Non-Goals

Do not change these in this branch:

- graph routing or node names;
- tool execution semantics;
- browser provider request/result normalization;
- policy decisions;
- observer correction behavior;
- completion criteria;
- eval scenario expected outcomes;
- default CLI output.

Do not remove hard browser invariants from model context unless the same
invariant is already covered by policy, observer logic, or scenario evals.

## Target Design

Add a context assembler that produces ordered context blocks. Each block should
be typed, named, and renderable to text.

Suggested minimum shape:

```python
ContextBlock
  name: str
  role: Literal["system", "user", "developer"]
  content: str
  priority: int
  source: str
  token_budget: int | None
  metadata: dict[str, Any]

AssembledContext
  system_prompt: str
  turn_prompt: str
  blocks: list[ContextBlock]
```

The initial implementation can render to the existing LangChain message shape:

```text
SystemMessage: core runtime prompt + selected system blocks
HumanMessage: task block + state blocks + action instruction
```

The assembler should be deterministic. Given the same state, tool inventory,
and selected skills, it should produce the same ordered blocks.

## Context Blocks

### Core Runtime Instructions

Keep model role, tool-use rules, final-answer rule, and JSON fallback format in
`src/agent_loop/prompts.py`.

This block should be task-agnostic and should not mention browser-specific
semantics unless browser context is selected.

### User Task

Render the raw user task exactly once. It should remain the highest-signal
turn-specific block.

### Plan State

Render the existing `plan` and `current_step` state using the current
`_format_plan()` behavior or a small shared formatter. Do not change plan
semantics in Phase 3.

### Transcript Summary

Render only durable, session-useful message history already carried by the
harness. Do not add a new summarizer in this branch unless an existing summary
field is present.

If summary support is absent, add the block type and leave it empty with tests
that prove empty blocks are omitted.

### Observation

Render `observation`, `consecutive_failures`, `repeat_count`, and related retry
state. Preserve current wording that prevents stale loop behavior until evals
prove an equivalent split is safe.

### Tool Inventory

Render tool names and compact descriptions from `ToolRegistry.get_all()` when
available. This should be informational only; native tool binding remains the
source of executable tool schemas.

### Browser State

Render browser context only when browser tools are available, the task appears
browser-related, or browser state already exists in `AgentState`.

Include:

- latest `snapshot`;
- extracted refs;
- snapshot reuse rule;
- stale-ref hints from observation/state;
- browser rule resource text or its selected sections.

Non-browser tasks should not receive browser-only instructions, snapshots, or
refs.

### Skills And Rule Resources

Use `src/agent_loop/skills.py` for a minimal local resource loader, not the full
Phase 7 skill system.

For this phase, a "skill" can be a typed prompt resource:

```python
PromptResource
  name: str
  path: Path
  description: str
  trigger_terms: tuple[str, ...]
  scopes: tuple[str, ...]
```

Initial resource:

- `browser-agent-rules`: loads `docs/development/browser-agent-rules.md` or a
  shorter copied runtime resource if tests show the development doc is too
  verbose.

Keep this loader read-only and deterministic. Do not add subagent execution,
external downloads, or user-installed skill discovery in Phase 3.

### Policy Hints

Render current policy state and blocked-action messages as hints only. Do not
move permission decisions into the prompt.

## Proposed Files

New source files:

- `src/agent_loop/context.py`
- `src/agent_loop/prompts.py`
- `src/agent_loop/skills.py`

Changed source files:

- `src/harness/context.py`
- `src/harness/runtime.py`
- `src/agent/nodes.py`
- `src/agent/prompts.py`

New test files:

- `tests/test_context_assembler.py`

Changed test files:

- `tests/test_prompts.py`
- `tests/test_agent_graph.py`
- `tests/test_harness_runtime.py`
- `tests/test_agent_loop_evals.py`

Optional docs follow-up:

- update `docs/architecture/overview.md` only after implementation lands;
- update `docs/glossary.md` with `ContextAssembler`, `ContextBlock`, and
  `PromptResource` if those names become stable.

## Implementation Slices

### Slice 1: Prompt Inventory And Golden Tests

Deliverables:

- add tests that capture the current required prompt constraints;
- classify constraints as core, browser, observation, completion, or output
  format;
- add golden tests for browser and non-browser rendering expectations.

Important constraints to preserve:

- no invented tool names;
- final answer only when task is complete;
- use latest observation and snapshot;
- snapshot refs are ephemeral;
- ref-based browser actions require visible current refs;
- stale or invalid refs trigger fresh snapshot/replan;
- search tasks type into editable controls before submitting;
- repeated unchanged actions require a different strategy;
- result extraction completes once enough requested data is visible;
- non-tool responses must be valid decision JSON.

Exit gate:

```powershell
python -m pytest tests\test_prompts.py
```

### Slice 2: Context Block Model

Files:

- `src/agent_loop/context.py`
- `tests/test_context_assembler.py`

Deliverables:

- `ContextBlock`;
- `AssembledContext`;
- deterministic block sorting;
- omission of empty blocks;
- rendering helpers for system and turn prompts.

Keep the API small:

```python
assembler = ContextAssembler()
context = assembler.assemble(state, tools=tools)
context.system_prompt
context.turn_prompt
```

Exit gate:

```powershell
python -m pytest tests\test_context_assembler.py
```

### Slice 3: Compatibility Renderer

Files:

- `src/agent_loop/prompts.py`
- `src/agent/prompts.py`
- `tests/test_prompts.py`
- `tests/test_context_assembler.py`

Deliverables:

- move prompt constants into named sections;
- render a compatibility prompt that contains the current required behavior;
- keep `AGENT_SYSTEM_PROMPT` and `AGENT_USER_PROMPT` exported for rollback and
  existing imports;
- add tests proving the compatibility renderer can reproduce the old prompt
  content needed by current graph tests.

Exit gate:

```powershell
python -m pytest tests\test_prompts.py tests\test_context_assembler.py
```

### Slice 4: Browser Rule Resource Selection

Files:

- `src/agent_loop/skills.py`
- `src/agent_loop/context.py`
- `docs/development/browser-agent-rules.md`
- `tests/test_context_assembler.py`

Deliverables:

- minimal `PromptResource` loader;
- browser relevance predicate;
- browser block rendering from selected rule resource;
- tests proving browser rules appear for browser tasks and are omitted for
  plain non-browser tasks.

Browser relevance should be conservative:

- browser tools are registered;
- state contains `snapshot`, browser action metadata, or browser observation;
- task text asks to open, browse, search a site, click, type, inspect a page,
  extract page results, or otherwise use the browser.

Exit gate:

```powershell
python -m pytest tests\test_context_assembler.py tests\test_prompts.py
```

### Slice 5: Harness Integration

Files:

- `src/harness/context.py`
- `src/harness/runtime.py`
- `src/agent/nodes.py`
- `tests/test_harness_runtime.py`
- `tests/test_agent_graph.py`

Deliverables:

- inject `ContextAssembler` through `ContextBuilder`;
- preserve `ContextBuilder.get_system_prompt()` for durable message history;
- expose a method for per-turn prompt rendering from graph state;
- update `create_agent_node()` to ask the harness/context layer for the
  assembled turn prompt instead of formatting `AGENT_USER_PROMPT` directly;
- avoid async tool inventory calls inside hot paths unless already available
  from the registry call used for model tool binding.

The first integration can keep the old prompt renderer as default and enable
the assembled renderer behind an internal option. Once tests and evals pass,
make assembled rendering the default while preserving a rollback path.

Exit gate:

```powershell
python -m pytest tests\test_harness_runtime.py tests\test_agent_graph.py
```

### Slice 6: Eval Baseline Check

Files:

- `tests/test_agent_loop_evals.py`
- `tests/evals/baselines/langgraph_v1.json`

Deliverables:

- run all existing scenario evals with assembled context;
- compare tool-call count, repeated action count, policy blocks, terminal
  status, and final answer assertions against the v1 baseline;
- document any intentional baseline change before accepting it.

Exit gate:

```powershell
python -m pytest tests\test_agent_loop_evals.py
```

### Slice 7: Full Verification

Commands:

```powershell
python -m pytest tests\test_context_assembler.py tests\test_prompts.py
python -m pytest tests\test_agent_graph.py tests\test_harness_runtime.py
python -m pytest tests\test_agent_loop_evals.py
python -m pytest
```

Deliverables:

- assembled context is the default prompt path;
- old prompt compatibility remains available for rollback;
- browser eval baseline does not regress;
- non-browser prompt path is materially smaller and does not include browser
  state or browser-only rules.

## Feature Flags And Rollback

Use a small runtime prompt mode switch:

```text
AUTOBROWSER_CONTEXT_MODE=assembled
AUTOBROWSER_CONTEXT_MODE=legacy
```

Default during development can be `legacy`. The branch is complete only when
`assembled` can become the default without eval regression.

Rollback path:

- set `AUTOBROWSER_CONTEXT_MODE=legacy`;
- `ContextBuilder.get_system_prompt()` returns the old `AGENT_SYSTEM_PROMPT`;
- agent node renders the old `AGENT_USER_PROMPT`;
- skill/resource loading is bypassed.

## Acceptance Criteria

Phase 3 is complete when:

- `ContextAssembler` owns prompt block selection and rendering;
- required browser constraints still appear for browser tasks;
- non-browser tasks do not receive browser-only context;
- latest observation, plan, counters, snapshot, and refs still reach the model
  when relevant;
- `AGENT_SYSTEM_PROMPT` no longer has to contain every browser/search/filter
  rule inline;
- hard invariants remain backed by policy, observer logic, or eval coverage
  before being removed from prompt text;
- scenario evals pass without increased repeated-action failures;
- the full pytest suite passes;
- rollback to legacy prompt rendering is a config-only change.

## Risks

- Removing browser rules too early can reintroduce stale-ref and repeated-click
  loops.
- Splitting prompt text without golden tests can silently drop completion rules.
- Tool inventory rendering can drift from native tool schemas if treated as
  executable truth.
- Loading long rule resources can increase context size instead of reducing it.
- Browser relevance detection that is too aggressive will keep polluting
  non-browser tasks; detection that is too narrow will omit required safety
  rules.

## Follow-Up Branches

After this branch:

1. `feat/proposed-action-contract`
2. `feat/tool-broker-permissions`
3. `feat/agent-loop-hooks`
4. `feat/agent-loop-skills`

Do not start Phase 4 until the assembled context path is covered by prompt
tests and scenario evals.
