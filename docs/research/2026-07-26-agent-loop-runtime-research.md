# Agent Loop Runtime Research

Status: Research
Date: 2026-07-26

## Question

AutoBrowser currently uses a LangGraph browser-task loop. The next product
direction is to build an AutoBrowser-owned agent loop with a structure closer
to Claude Code and Codex: a long-running runtime that owns goals, context,
tools, approvals, events, skills, subagents, and verification, with browser
automation as one capability rather than the whole architecture.

This note records the structural patterns worth adopting and a migration plan
that preserves the working browser core.

## Sources Checked

- AutoBrowser code and docs:
  - `src/agent/agent.py`
  - `src/agent/nodes.py`
  - `src/agent/utils.py`
  - `src/harness/runtime.py`
  - `src/harness/session.py`
  - `src/harness/tools.py`
  - `src/harness/policy.py`
  - `docs/architecture/overview.md`
  - `docs/research/index.md`
- OpenAI Codex manual, fetched with the official Codex manual helper on
  2026-07-26:
  - `https://developers.openai.com/codex/codex-manual.md`
  - [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents.md)
  - [Codex best practices](https://learn.chatgpt.com/guides/best-practices.md)
  - [Skills and plugins](https://learn.chatgpt.com/docs/skills-and-plugins.md)
  - [Agent approvals and sandboxing](https://learn.chatgpt.com/docs/agent-approvals-security)
- Anthropic Claude Code / Claude Agent SDK documentation:
  - [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
  - [How the agent loop works](https://code.claude.com/docs/en/agent-sdk/agent-loop)
  - [Claude Code hooks](https://code.claude.com/docs/en/hooks)
  - [SDK hooks](https://code.claude.com/docs/en/agent-sdk/hooks)
  - [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
  - [SDK subagents](https://code.claude.com/docs/en/agent-sdk/subagents)
  - [Claude Code skills](https://code.claude.com/docs/en/skills)
  - [SDK Claude Code features](https://code.claude.com/docs/en/agent-sdk/claude-code-features)
  - [Claude Code memory](https://code.claude.com/docs/en/memory)
  - [SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
  - [Secure agent deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment)
- Anthropic engineering guidance:
  - [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

## What These Systems Have In Common

Claude Code and Codex are not just one prompt plus a tool list. Their useful
shape is a runtime around a model loop:

1. A durable session or goal is created.
2. Context is assembled from user input, repo guidance, memory, tool state, and
   recent observations.
3. The model chooses one next step: answer, tool call, ask for input, update
   plan, delegate, or stop.
4. A permission or policy layer classifies the proposed action.
5. A tool broker executes approved actions and records structured results.
6. Observations are normalized into compact state.
7. The runtime streams events to the UI and stores an audit trail.
8. The loop repeats until a terminal result, budget limit, user stop, or block.

The important distinction is ownership. The model proposes actions, but the
runtime owns permissions, state transitions, eventing, tool execution, retries,
compaction, and completion criteria.

## Relevant Codex Patterns

Codex uses durable instructions and configuration as first-class inputs to the
loop. The current manual emphasizes:

- `AGENTS.md` for repository-specific guidance, commands, conventions, and
  verification expectations.
- `config.toml` for durable settings such as model defaults, reasoning effort,
  sandboxing, approval policy, profiles, and MCP setup.
- MCP for external tools and context.
- Skills for reusable task workflows with instructions and supporting
  resources.
- Plugins for installable bundles that can include skills, connectors, MCP
  servers, hooks, and metadata.
- Subagents for independent parallel work, where the main thread keeps
  requirements and decisions while child agent threads return summaries.
- Sandbox and approval controls as runtime policy, not prompt-only behavior.

For AutoBrowser, the strongest takeaway is to stop growing one large browser
prompt. Repeated rules should become one of:

- durable project guidance;
- a browser skill;
- a hard policy or hook;
- an observer invariant;
- an eval regression case.

## Relevant Claude Code Patterns

Claude Code's public documentation exposes a similar runtime shape:

- The Agent SDK documents the loop as: receive prompt and context, evaluate,
  request tool calls or answer, feed tool results back into another evaluation,
  and repeat until completion.
- CLI, SDK, GitHub Actions, and other surfaces are different entry points over
  an agent runtime.
- Hooks are deterministic commands that run at lifecycle points, useful for
  logging, validation, formatting, or permission checks.
- Subagents are specialized agents with separate context, tool access, and
  instructions.
- Skills package reusable instructions, scripts, and resources for repeatable
  workflows.
- Memory files keep durable project and user context out of the one-off prompt.
- MCP provides external tools and resources.
- Settings and permissions configure behavior instead of relying only on model
  instructions.

The main architectural lesson is the same: keep the loop simple, but make the
runtime around it explicit and extensible.

## Current AutoBrowser Shape

AutoBrowser already has several strong pieces:

- `build_agent_graph()` assembles a verified LangGraph flow:

```text
START -> plan -> agent -> policy -> executor -> observe -> agent
```

- `SessionRuntime` owns the process-long session lifecycle.
- `BrowserHarness` injects runtime infrastructure into the graph.
- `ToolRegistry` lazily loads tools from providers.
- `PolicyEngine` gates tool execution.
- `PlaywrightMCPBrowserProvider` isolates browser backend adaptation.
- `FakeBrowserProvider` makes deterministic browser behavior testable.
- `human_input_node` already models approval as a graph interrupt.
- `observe_node` already normalizes snapshots, invalid refs, and ineffective
  actions.

The current weak point is that several runtime responsibilities are still
distributed across prompts and graph nodes:

- browser interaction policy is partly prompt text, partly policy, partly
  observer;
- the agent prompt is too large and mixes strategy, hard constraints, and
  task-completion rules;
- tool policy is marker-based and not configurable by tool class, domain, user,
  or channel;
- there is no event-sourced audit trail for every model step and tool result;
- there is no skill/plugin/subagent model;
- there is no service API or UI over the runtime;
- evals are unit-test heavy but not scenario/trace oriented.

## Proposed Agent Loop V2

The target should be an explicit runtime loop, with LangGraph kept as an
implementation detail only while it remains useful.

```text
SessionRuntime
  -> GoalRunner
      -> ContextAssembler
      -> ModelDriver
      -> ActionParser
      -> PermissionEngine
      -> ToolBroker
      -> ObservationCompiler
      -> MemoryStore
      -> EventStream
      -> CompletionGuard
```

### Loop Contract

Each loop iteration should produce and consume typed events:

- `goal.started`
- `context.built`
- `model.requested`
- `model.responded`
- `plan.updated`
- `action.proposed`
- `policy.decided`
- `approval.requested`
- `tool.started`
- `tool.finished`
- `observation.compiled`
- `memory.compacted`
- `subagent.started`
- `subagent.finished`
- `goal.completed`
- `goal.blocked`
- `goal.cancelled`

The model should not mutate state directly. It should emit a proposed action:

- `answer`
- `tool_call`
- `ask_user`
- `update_plan`
- `delegate`
- `compact`
- `stop`

Runtime components then decide whether the action is allowed, executed,
converted into a user approval request, or blocked.

## Proposed Modules

Keep current code working, but introduce a new runtime package beside the
existing graph:

```text
src/agent_loop/
  actions.py       # typed model actions and runtime decisions
  engine.py        # explicit while-loop orchestration
  events.py        # event schema and event sink interfaces
  context.py       # context assembly, prompt sections, compaction inputs
  model.py         # model invocation and action parsing
  tools.py         # tool broker facade over ToolRegistry and providers
  permissions.py   # policy, approvals, sandbox, tool scopes
  hooks.py         # pre/post lifecycle hook runner
  memory.py        # durable state, summaries, session transcripts
  skills.py        # skill discovery and routing
  subagents.py     # child task/session orchestration
  evals.py         # scenario runner interfaces
```

Map existing components into this package gradually:

| Current component | V2 role |
| --- | --- |
| `SessionRuntime` | Process/session owner stays, calls `GoalRunner` |
| `BrowserHarness` | Becomes or wraps `GoalRunner` composition root |
| `ToolRegistry` | Lower-level provider loader behind `ToolBroker` |
| `PolicyEngine` | Seed for `PermissionEngine` |
| `human_input_node` | Seed for approval request/resume events |
| `observe_node` | Seed for `ObservationCompiler` |
| `MemoryManager` | Seed for durable `MemoryStore` |
| `AGENT_SYSTEM_PROMPT` | Split into core prompt plus skills/policies/hooks |
| Planner subgraph | Becomes optional `plan.updated` action handler |

## Browser-Specific Skill Split

The browser rules now embedded in `AGENT_SYSTEM_PROMPT` should move into a
browser skill plus hard runtime guards.

Recommended split:

- Core agent prompt: role, action contract, completion contract, no invented
  tools.
- Browser skill: snapshot/ref semantics, search flow, result extraction,
  fallback strategy.
- Policy/permissions: no stale ref action, no repeated unchanged action, no
  redundant snapshot, approval for risky domains/actions.
- Observer: snapshot normalization, browser state fingerprints, result
  extraction hints.
- Eval fixtures: Ozon-like search, stale refs, unchanged search click,
  dynamic search input, filter confirmation.

This makes the loop easier to reason about and reduces prompt drift.

## Permission Model

The current marker list in `PolicyEngine` is a useful seed but not enough for a
Codex/Claude-style runtime. Add explicit tool scopes:

- `read`
- `write`
- `browser.read`
- `browser.action`
- `browser.navigation`
- `network`
- `filesystem.read`
- `filesystem.write`
- `credential`
- `payment`
- `destructive`
- `external_post`

Then load policy from configuration:

```yaml
default: ask
allow:
  - browser.read
  - browser.navigation
  - browser.action
ask:
  - filesystem.write
  - external_post
deny:
  - payment
  - credential
domains:
  "*.bank.example": deny
  "localhost": allow
```

The browser agent should not rely on prompt text for high-risk boundaries.

## Hooks

Add deterministic hooks before making the loop more autonomous:

- `PreModel`: redact secrets, add runtime warnings, trim context.
- `PostModel`: validate action JSON/tool-call shape.
- `PreTool`: enforce policy, log intent, block unsafe args.
- `PostTool`: normalize result, redact secrets, attach artifacts.
- `OnObservation`: run browser-specific checks.
- `OnGoalEnd`: run verification, write trace summary.

Hooks should be configured, audited, and disabled by default unless trusted.

## Subagents

Subagents should not be added as free-form recursion. Start with bounded child
goals:

- `research`: read-only, web/docs/code search, returns summary.
- `trace_analyzer`: reads event trace and classifies loop failures.
- `browser_eval_worker`: runs a deterministic fake-browser scenario.
- `reviewer`: checks a proposed change or plan.

Child goals should receive:

- a narrow objective;
- explicit read/write permissions;
- a token/step budget;
- expected output schema;
- no authority to mutate parent state except through a returned summary.

This matches the useful Codex pattern: keep noisy exploration out of the main
thread and merge summaries, not raw logs.

## Durable Runtime State

Move from session JSON metadata toward event-sourced runtime records:

```text
.autobrowser/sessions/<session_id>/
  session.json
  events.jsonl
  goals/<goal_id>.json
  goals/<goal_id>.trace.jsonl
  checkpoints/<goal_id>.json
  workspace/
    screenshots/
    downloads/
    artifacts/
```

Later, mirror this into SQLite for API/UI use.

The key requirement is replayability: a failed browser task should produce a
trace that can be loaded into an eval fixture or a trace analyzer.

## Migration Plan

### Phase 1: Event Envelope Around Current LangGraph

Keep `build_agent_graph()` unchanged. Add an event sink around
`BrowserHarness.stream_updates()` and tool execution.

Deliverables:

- event schema;
- JSONL session trace;
- trace export command;
- tests for event ordering and redaction;
- docs for trace format.

### Phase 2: Prompt Split And Context Assembler

Extract prompt sections into typed context blocks.

Deliverables:

- core agent prompt;
- browser skill prompt/resource;
- context builder that chooses relevant blocks;
- tests that browser rules still appear for browser tasks;
- prompt regression tests for stale-ref and search-flow constraints.

### Phase 3: PermissionEngine And Hooks

Replace marker-only policy with config-driven scopes and lifecycle hooks.

Deliverables:

- tool metadata and scope model;
- YAML/TOML policy config;
- approval event records;
- `PreTool` and `PostTool` hooks;
- policy tests for browser, credential, payment, filesystem, and external post
  actions.

### Phase 4: Scenario Evals

Create a repeatable browser eval harness before adding more autonomy.

Deliverables:

- fake-browser scenario files;
- success metrics;
- trace assertions for tool count and non-progress loops;
- regression fixtures for current research themes.

### Phase 5: Skills And Subagents

Add skills first, then subagents.

Deliverables:

- skill manifest and loader;
- browser skill;
- research/reviewer subagent types;
- subagent event records;
- summary schema;
- tests ensuring subagents cannot mutate parent state directly.

### Phase 6: Explicit AgentLoop Engine

After events, context, policy, hooks, evals, skills, and subagents exist, decide
whether to replace LangGraph with `src/agent_loop/engine.py`.

The first explicit loop can be small:

```python
while not goal.done:
    context = context_assembler.build(goal)
    action = model_driver.next_action(context)
    decision = permission_engine.classify(action)
    if decision.needs_approval:
        action = approval_queue.wait(action)
    result = tool_broker.execute(action)
    observation = observation_compiler.compile(action, result)
    memory_store.record(action, result, observation)
    completion_guard.check(goal, observation)
```

Keep LangGraph available behind a feature flag until evals show the explicit
loop is at least as reliable.

## Acceptance Criteria

Agent Loop V2 is ready when:

- every model step and tool action is visible in a durable trace;
- browser tasks can be replayed as eval fixtures;
- hard browser safety invariants live outside prompt text;
- permissions are configurable by tool scope and target domain;
- user approvals are durable events, not only graph interrupts;
- skills can add context without editing the core prompt;
- subagents can run bounded read-only work and return summaries;
- CLI and future API/UI surfaces use the same runtime;
- the old LangGraph path can be compared against V2 on the same scenario suite.

## Open Questions

- Should the explicit loop replace LangGraph completely, or should LangGraph
  remain the graph engine behind the new runtime interfaces?
- Which state store should come first: JSONL plus checkpoint JSON, or SQLite?
- Should browser skills be loaded through the same skill system as future
  non-browser workflows?
- Should tool schemas declare scopes manually, or should scopes be inferred
  from provider metadata with overrides?
- How should streaming UI represent subagent events without overwhelming the
  main task view?
- Which eval scenarios define the minimum acceptable browser-agent reliability
  before increasing autonomy?

## Recommendation

Do not start by rewriting the graph. First build the runtime shell that Codex
and Claude Code have around their loops: events, context assembly, permissions,
hooks, durable traces, and evals. Once those boundaries exist, replacing the
LangGraph control flow with an explicit AutoBrowser-owned loop becomes a
measured migration instead of a risky rewrite.
