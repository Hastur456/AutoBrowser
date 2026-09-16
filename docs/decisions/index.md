# Architecture Decisions

This directory contains Architecture Decision Records (ADRs).

## Existing Records

- [2026-09-16 Opt-In YAML Settings File](2026-09-16-opt-in-yaml-settings-file.md):
  adds a YAML file as a fourth, strictly opt-in settings source (`AUTOBROWSER_CONFIG_FILE`,
  no working-directory scan) below `.env` and above secret files; deep per-field merging, and
  the `llm.reasoning_effort`/`max_output_tokens`/`max_reasoning_tokens` fields. Supersedes the
  typed-settings ADR's rejection of a config file format.
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
- [2026-07-26 Browser Provider Boundary](2026-07-26-browser-provider-boundary.md)
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
