# Architecture Decisions

This directory contains Architecture Decision Records (ADRs).

## Existing Records

- [2026-10-01 PermissionEngine](2026-10-01-permission-engine.md) (Proposed): deterministic,
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
