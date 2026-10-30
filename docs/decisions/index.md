# Architecture Decisions

This directory contains Architecture Decision Records (ADRs).

## Existing Records

- [2026-10-31 Langfuse as the Dataset, Experiment and Score Platform](2026-10-31-langfuse-platform-integration.md)
  (Proposed, Russian): a hybrid — the regression verdict and CI exit codes stay local and
  hermetic, Langfuse is a second interface. No Langfuse SDK: a narrow `httpx` REST client plus
  OTLP/JSON with `langfuse.experiment.*` attributes. Each batch becomes one experiment built
  after the run from the batch folder (one item trace per scenario), idempotent through
  deterministic ids and a per-batch sync ledger. Dataset version = `tasks_sha256` + git commit,
  with Langfuse item versions as a projection. Grades, checks, `failure_kind`, `stop_reason` and
  the local verdict become scores; a managed LLM judge and an annotation queue are
  informational only. Vendor code lives in `scripts/langfuse_bridge/`, the settings section is
  the neutral `eval_platform`, content capture is `none` by default. Supersedes in part the
  2026-10-19 No-Langfuse ADR (decision 1, datasets/scores API); amends the Part B OTLP plan.
- [2026-10-26 Agent Skills](2026-10-26-agent-skills.md) (Accepted, Russian): skills in the
  Agent Skills `SKILL.md` format (folder, `name`/`description` frontmatter, body, reference
  files; AutoBrowser extensions only as string `metadata` keys `always`/`requires`/`title`);
  progressive disclosure — a `Skills` catalog block, bodies and files loaded by the model with
  the `readOnlyHint` harness tool `skill_view` (server `skills`); the model chooses, no matching
  on task words or tool names; built-in and `<home>/skills/` sources only, user skills shadow
  built-ins; no scripts in v1; kept apart from agent-written L4 procedures; the browser rules
  become the first built-in `always` skill with an unchanged prompt; neutral `PromptSection`
  replaces `PromptResource`; an off-by-default `skills` settings section.
- [2026-10-22 OpenAI-Compatible Chat Provider](2026-10-22-openai-compatible-chat-provider.md)
  (Accepted, Russian): one `OpenAICompatChatModel` over Chat Completions for any compatible
  service (OpenAI, aggregators such as OhMyLama/OpenRouter, local servers) chosen by config
  only — no vendor names in code; `llm.provider: ollama | openai_compat` and a provider
  registry as the default `llm_factory`; the `openai` SDK with explicit key/base URL and the
  `OPENAI_*` environment neutralised; optional `temperature`, `max_tokens_param`,
  `reasoning_effort`, `parallel_tool_calls`, `extra_headers`/`extra_body`, a 60 s call timeout
  for both providers with no retries by default; `doctor --llm` network probe; neutral `ModelProviderError` and `ModelUsage` in `src/llm.py`. Extends the
  LangChain-removal ADR on provider adapters.
- [2026-10-19 Autonomous Test Pipeline](2026-10-19-autonomous-test-pipeline.md) (Proposed,
  Russian): one `scripts/ci.py` entry point with `gate` (hermetic, Windows + Linux: unit,
  scripted evals against baseline v2, cassette replay of recorded model/tool responses, TUI
  tests, paired A/B offline bench) and `nightly` (real model + Playwright MCP on a local fixture
  site, golden set with generic graders, live latency/token benchmarks against a rolling
  history) profiles; exit codes and a one-line verdict; baselines change only by an explicit
  command; `ModelUsage` on `ModelResponse`; a `pipeline` settings section.
- [2026-10-19 No Langfuse; Neutral OTLP Export Deferred](2026-10-19-langfuse-observability.md)
  (Proposed, Russian): Langfuse is not added — the hermetic gate needs no service and events,
  usage and local history cover the verdict; a Langfuse-specific SDK sink is rejected; an
  off-by-default `OtlpEventSink` (any OTLP backend, Langfuse included) is recorded as a
  deferred option with explicit revisit triggers.
- [2026-10-19 Terminal UI over a Shared SessionController](2026-10-19-session-controller-and-local-web-ui.md)
  (Accepted, Russian): a Claude-Code-style inline TUI on prompt_toolkit + rich (frozen blocks
  in the scrollback, a bounded live zone, in-stream approvals, slash commands, status line) over
  a UI-neutral `SessionController` in `src/ui/`, a session-scoped `EventBroadcaster` sink and
  `ApprovalBroker`. Single-writer render rules, `full/degraded/plain` terminal levels, a themed
  palette with 256/16-colour and ASCII fallbacks, a new `ui` settings section, structured
  `goal.*` payloads and `goal.cancelled`. Web UI deferred. The file keeps its first-draft name.
- [2026-10-19 The `autobrowser` Command and Home Directory](2026-10-19-autobrowser-command-and-home-directory.md)
  (Accepted, Russian): `uv_build` packaging with an `autobrowser` console script (`uv tool
  install`, `pipx`, `uv run`). `AUTOBROWSER_HOME` (default `~/.autobrowser`) replaces cwd- and
  repo-relative lookups for `config.yaml`, `.env` and `storage.root_dir`, keeping legacy repo
  paths as a fallback. Claude-style CLI (`-p`, `--output-format`, `--continue/--resume`,
  `doctor`, `sessions`, `replay`, `completion` via argcomplete). Supersedes the default-YAML ADR
  on the file location once accepted.
- [2026-10-19 Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md): nothing
  decides by a tool name. Kept snapshots are per server from `readOnlyHint`; arguments are
  filtered by schema only; the session configures prompt resources; memory compresses every
  tool alike; the CLI reads the page URL from state. `src/browser/names.py`, `ToolRoles` and
  `BrowserToolNormalizer` are removed and `src/` is fenced against tool-name literals.
  Supersedes the injected tool roles ADR on how roles are decided.
- [2026-10-17 Explicit Agent Objects](2026-10-17-explicit-agent-objects.md): a `TaskRequest`
  replaces the `task_config` dict plumbing, an `Agent` object replaces the `native_task_runner`
  closure, `SessionContext.build_agent()` composes the production agent once per session,
  `GoalRunner(task_runner, events, GoalLimits)` drops `harness`/`session_config`/`getattr`,
  `EngineResources` fields are typed, and the graph-era shims (`native_latest_state_loader`,
  `src/agent_loop/engine.py`, `loop.latest_state_timeout_seconds`) go.
- [2026-10-17 Injected Tool Roles](2026-10-17-injected-tool-roles.md): the engine imports
  nothing from `src/browser/`; which tool returns the snapshot and which tools act on the
  environment comes from a `ToolRoles` object in `EngineResources` (`BrowserToolRoles` from the
  session, `NullToolRoles` by default). The dead tab-activation path and the
  `pending_browser_tab_*` fields are removed; `tests/test_engine_boundaries.py` fences the
  engine. Refines the server-neutral progress journal ADR.
- [2026-10-03 Merging Memory Consolidation](2026-10-03-merging-memory-consolidation.md):
  consolidation sees the full bodies of the visited sites' entries and merges into them; a
  consolidation rewrite keeps `uses`/`failures` (`create(keep_trust=True)`) so site entries
  can still reach `verified`; `BrowserMemoryPolicy` refuses DOM-scraping advice; the memory
  limits move from module constants to `MemorySettings`. Extends the layered memory ADR.
- [2026-10-02 Layered Agent Memory](2026-10-02-layered-agent-memory.md): four memory layers,
  all opt-in through `memory.*` — a deterministic history budget (`[cleared]` old tool
  outputs), task-local working notes (an optional `notes` tool argument), a task digest at the
  task boundary, and persistent per-site / procedure markdown files (`MemoryStore`, a
  generated index, a `Memory` context block, `memory_view`/`memory_write` tools through the
  normal permission pipeline, a content policy, staged trust, opt-in consolidation). The
  engine only calls `resources.memory.render(state)`. Extends the session-scoped context
  memory ADR.
- [2026-10-02 Model-Based Approval Judge](2026-10-02-model-approval-judge.md) (Proposed):
  opt-in `permissions.approval_judge` (`off | model | classifier | both`) lets the acting
  model's `approval_request` argument or a separate classifier call escalate a
  state-changing call to human approval without rules; judge asks are `always_ask` and never
  lift a rule; the engine stays deterministic. Agent/planner prompts stop refusing
  user-requested purchases and rely on the approval gate.
- [2026-10-01 Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md):
  the engine and `src/browser/` hold no tool names and no rule ships with the code
  (`BUILTIN_RULES`/`BROWSER_BUILTIN_RULES` deleted, `builtin` source gone), so nothing asks
  out of the box; risky-tool guards are opt-in config examples. The canonical `browser.*`
  vocabulary and the REPL's `browser_evaluate` are removed. Supersedes parts of the
  PermissionEngine ADR.
- [2026-10-01 PermissionEngine](2026-10-01-permission-engine.md): deterministic,
  session-scoped `allow | ask | deny` authorization after `pre_tool_use` hooks — config rules
  (`deny > ask > allow`), modes (`default`/`read_only`/`dont_ask`/`bypass`), fail-closed,
  session grants; progress guard split out of `policy.py`; `destructiveHint` ignored per the
  Playwright MCP annotation inventory.
- [2026-09-30 Command Hooks](2026-09-30-command-hooks.md): `type: command` hooks run an
  external process in the Claude Code/Codex style — event JSON on stdin, exit `2` blocks
  with stderr as the reason, JSON stdout maps onto `HookResult`; engine semantics unchanged.
- [2026-09-30 Lifecycle Hooks Engine](2026-09-30-lifecycle-hooks-engine.md):
  deterministic, config-driven `HookEngine` (`goal_start`, `pre_tool_use`,
  `permission_request`, `post_tool_use*`, `stop`, `goal_end`) called by the loop after
  built-in policy; sequential handlers, `deny > ask > allow`, `hook.decided` telemetry,
  session-scoped and disabled by default.
- [2026-09-28 Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md):
  tool-agnostic action journal (call + outcome fingerprints), `Action History` context block,
  repeat blocking via `loop.max_ineffective_actions`, `readOnlyHint`-based freshness and
  honest `blocked`/`cancelled` completion. Extends the native engine ADR.
- [2026-09-28 Universal MCP Manager](2026-09-28-universal-mcp-manager.md):
  server-agnostic `MCPManager` pool configured by `mcp_servers`, harness `MCPRuntime` and
  catalog-to-tool bridge, stateless `ToolCallNormalizer`s. Supersedes the browser provider
  boundary ADR.
- [2026-09-26 Default YAML Settings File](2026-09-26-default-yaml-settings-file.md):
  `config.yaml` at the repo root auto-loads when present; an explicit
  `AUTOBROWSER_CONFIG_FILE` stays mandatory. Supersedes the opt-in YAML ADR's activation
  clause.
- [2026-09-16 Opt-In YAML Settings File](2026-09-16-opt-in-yaml-settings-file.md):
  adds a YAML file as a fourth, strictly opt-in settings source (`AUTOBROWSER_CONFIG_FILE`,
  no working-directory scan) that outranks `AUTOBROWSER_*`/`.env` as a partial profile;
  deep per-field merging, and the `llm.reasoning_effort`/`max_output_tokens`/
  `max_reasoning_tokens` fields. Supersedes the typed-settings ADR's rejection of a config
  file format.
- [2026-09-16 Typed Settings Module](2026-09-16-typed-settings-module.md):
  consolidates every tunable into `src/config.py`, a pydantic-settings root
  using `AUTOBROWSER_<SECTION>__<FIELD>` names; removes the flat vendor names
  and the `load_dotenv()` path, and wires the provider API key explicitly.
- [2026-09-03 Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md):
  removes the whole LangChain/LangGraph/LangSmith dependency stack and defines
  the provider-neutral `ChatModel`/`ModelResponse` contract, `Message`/`ToolCall`
  types, and `Tool`/`ToolDef` objects the engine drives (extends the engine-native
  ADR below; the checkpoint-saver and LangSmith notes there no longer apply).
- [2026-08-31 Native Agent Loop Engine](2026-08-31-native-agent-loop-engine.md):
  the engine-native `AgentLoopEngine` is the sole runtime; `src/agent/` and all
  LangGraph control flow are removed (supersedes the LangGraph-thread decisions
  below).
- [2026-07-26 Browser Provider Boundary](2026-07-26-browser-provider-boundary.md):
  superseded by the 2026-09-28 Universal MCP Manager ADR.
- [2026-07-25 Session-Scoped Agent Context Memory](2026-07-25-session-scoped-agent-context-memory.md)
- [2026-07-24 Task Memory Isolation and Session Persistence](2026-07-24-task-memory-isolation-and-session-persistence.md)
- [2026-07-24 SessionContext Root Object](2026-07-24-session-context-root-object.md)
- [2026-07-23 Long-Lived Session Runtime](2026-07-23-long-lived-session-runtime.md)

## Templates

- [ADR Template](adr-template.md)

## Naming

Until the project establishes numbered ADRs, prefer date-based filenames:

```text
YYYY-MM-DD-short-decision.md
```

If numbered ADRs are introduced, use stable sequence numbers:

```text
0001-short-decision.md
0002-short-decision.md
```
