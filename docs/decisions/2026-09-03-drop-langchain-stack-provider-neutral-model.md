# ADR: Drop the LangChain/LangGraph/LangSmith Dependency Stack

Status: Accepted
Date: 2026-09-03

## Context

The [native engine-loop ADR](2026-08-31-native-agent-loop-engine.md) made
`AgentLoopEngine` the sole runtime and removed `src/agent/`, but the process
still ran on the LangChain/LangGraph/LangSmith libraries: `LoopState.messages`
were `langchain_core` `BaseMessage`s, tools flowed through langchain
`StructuredTool` objects, `MemoryManager` owned a LangGraph checkpoint saver,
model access went through `langchain_ollama.ChatOllama`, Playwright MCP tools
were loaded through `langchain_mcp_adapters.MultiServerMCPClient`, evals used
`langchain_core` `FakeListLLM`, and `configure_langsmith_tracing`
(`src/harness/langsmith.py`) + `src/agent_loop/tracing.py` wired the run into
LangSmith.

These dependencies dragged the whole provider ecosystem (LangChain-core,
LangGraph checkpoints, MCP adapters, LangSmith SDK) into a project whose control
flow is already engine-native and whose contracts were already provider-neutral.
They only provided wire serialization and object wrappers, while keeping the
project coupled to one framework and obscuring the model/tool boundary.

Decision: remove every LangChain/LangGraph/LangSmith import and dependency, and
define the provider-neutral contracts the engine already assumed as explicit,
dependency-free types in the repository.

## Decision

- **Provider-neutral chat contract:** `src/llm.py` now defines
  `ChatModel` (an `async complete(messages, *, tools=(), **params) ->
  ModelResponse` protocol) and the canonical `ModelResponse`
  (`content` and/or `tool_calls`, plus `finish_reason`). The engine drives only
  this contract; it never sees provider objects.
- **Provider-neutral messages:** `src/messages.py` is the single,
  standard-library-only home for `Message` (roles `system`/`user`/`assistant`/
  `tool`) and `ToolCall`. `LoopState.messages` is now a `list[Message]`.
- **Provider adapters:** `src/providers/` holds thin `ChatModel`
  implementations. `src/providers/ollama.py` (`OllamaChatModel`,
  `ollama_llm_factory`) serializes `Message`/`ToolDef` to Ollama's `/api/chat`
  shape and parses replies into `ModelResponse`, replacing
  `langchain_ollama.ChatOllama`.
- **Neutral tools:** `src/contracts.py` adds frozen `Tool` (name, async `func`,
  description, JSON-Schema `input_schema`, `to_def()`, `invoke()`) and `ToolDef`
  (the model-visible schema). These replace langchain `StructuredTool`s; the
  fake and Playwright MCP providers build `Tool` objects directly, and
  `ToolBroker`/`ToolRegistry` dispatch through a neutral `invoke` surface
  (`src/harness/tools.py` `to_tool_def` normalizes legacy duck-typed tools).
- **Functional memory:** `MemoryManager` in `src/harness/memory.py` is a
  stateless history service: it shapes a `list[Message]` and returns new lists.
  The LangGraph checkpoint saver and `CheckpointSaverFactory` are gone; durable
  history lives on `LoopState.messages` and the cross-task
  `SessionContext.state` carry-forward. `BrowserHarness` and `EngineResources`
  no longer carry a `MemoryManager`.
- **No LangSmith tracing:** `src/agent_loop/tracing.py` and
  `src/harness/langsmith.py` are deleted; `SessionConfig.tracing_enabled` and
  the `langsmith_tracing` configurable are removed. The only remaining local
  observability is the durable `events.jsonl` records with the `AgentTraceSink`
  projection and the `replay_trace.py`/`export_agent_trace.py` scripts.
- **MCP loading:** `src/mcp/mcp_setup.py` (LangChain `MultiServerMCPClient`)
  is removed; Playwright MCP tools are loaded through
  `src/mcp/playwright_runtime.py` and wrapped by `PlaywrightMCPBrowserProvider`.
- **Eval fake:** scenario evals use a local `FakeChatModel` over the neutral
  `ChatModel` contract (`src/agent_loop/evals.py`) instead of
  `langchain_core` `FakeListLLM`.

## Consequences

Positive:

- No LangChain/LangGraph/LangSmith dependency or import remains anywhere in
  `src/`; `requirements.txt` drops the whole stack. The model/tool/memory
  boundaries are now explicit, standard-library contracts the engine and
  harness already shared.
- The engine, harness, and browser layers no longer depend on any framework
  wire format; a new backend is one thin `ChatModel` adapter away.
- No checkpoint saver and no cloud tracing: session state and history are plain
  Python objects serialized into `.autobrowser` records, and diagnostics stay
  local.

Tradeoffs and risks:

- Provider adapters re-implement wire serialization that the removed libraries
  once provided (small, focused code in `src/providers/`), so Ollama wire-format
  changes must be tracked there.
- `AgentState`/`BrowserState` remain type-only `TypedDict`s in `src/state.py`
  for annotation; `Message` list types replace the old langchain `BaseMessage`
  annotations in loop/harness code.
- The eval baseline file is still named `tests/evals/baselines/langgraph_v1.json`
  (a historical golden set); it is data, not a dependency, and can be renamed
  without code impact when desired.

## Alternatives Considered

- **Keep LangChain as the serialization layer.** Rejected: control flow and
  contracts were already provider-neutral, so the framework added dependency
  weight and indirection without owning any decision.
- **Adopt a different provider SDK.** Rejected: the `ollama` Python client is
  already a dependency; one thin adapter covers the actual backend.
- **Retain a checkpoint saver abstraction.** Rejected: durable history already
  travels explicitly through `LoopState.messages` and `SessionContext.state`,
  so the saver was dead surface.

## Related

- Supersedes the remaining LangGraph-thread/checkpoint-saver assumptions in
  [2026-08-31-native-agent-loop-engine.md](2026-08-31-native-agent-loop-engine.md)
  (the engine-native decision stands; its "checkpoint saver retained" and
  "LangSmith-adjacent" notes no longer apply).
- Code: `src/llm.py`, `src/messages.py`, `src/providers/ollama.py`,
  `src/contracts.py` (`Tool`/`ToolDef`), `src/harness/memory.py`,
  `src/harness/tools.py` (`to_tool_def`), `src/agent_loop/execution/state.py`
  (`LoopState.messages`), `src/agent_loop/evals.py` (`FakeChatModel`),
  `src/mcp/playwright_runtime.py`.
- Deleted in this change: `src/agent_loop/tracing.py`,
  `src/harness/langsmith.py`, `src/mcp/mcp_setup.py`, `scripts/export_trace.py`,
  `tests/test_agent_loop_tracing.py`.
