# Glossary

| Term | Meaning |
| --- | --- |
| Agent graph | The compiled LangGraph state machine assembled by `build_agent_graph`. |
| Agent node | The reasoning node that chooses a tool call, replans, or returns a final answer. |
| AutoBrowser | The browser automation agent implemented in this repository. |
| BrowserHarness | Runtime composition wrapper that injects context, tools, memory, policy, and telemetry into the graph. |
| `browser_evaluate` | Browser tool fallback for cases where snapshots cannot expose required information. |
| `browser_find` | Browser tool for plain-text search; not reliable for structured link or attribute extraction. |
| `browser_snapshot` | Source-of-truth browser observation containing visible page state and element refs. |
| Checkpointer | LangGraph persistence component owned by `MemoryManager`. |
| Compact observation | Short observer output derived from a tool result and used by the next agent step. |
| Direct search URL fallback | Navigating directly to a site's search results URL when UI search controls do not make progress. |
| Executor | Subgraph that resolves and invokes approved tool requests. |
| GraphRecursionError | LangGraph error raised when the graph exceeds the configured recursion limit. |
| Harness | Runtime layer around the graph; owns infrastructure that should not be hardcoded into agent nodes. |
| Ineffective browser action | A successful browser action whose follow-up snapshot has the same visible fingerprint as the previous snapshot. |
| LangGraph | Graph runtime used for the AutoBrowser agent loop and subgraphs. |
| MCP | Model Context Protocol, used here for browser tool integration. |
| Observer | Subgraph that translates tool results and snapshots into compact state updates. |
| Planner | Subgraph that creates or revises compact task plans. |
| Playwright MCP | Browser automation tool provider whose snapshot refs drive interactions. |
| PolicyEngine | Harness boundary that classifies tool requests as approved, needing human input, or blocked. |
| ref | Ephemeral Playwright MCP element identifier such as `e123`; valid only for the snapshot that produced it. |
| SessionConfig | Args-derived configuration used to initialize a long-lived `SessionRuntime` and shared task config. |
| SessionRuntime | Process-lifetime runtime that owns interaction lifecycle and long-lived resources, then delegates each task to `BrowserHarness`. |
| Snapshot depth | Tool argument that controls how much visible hierarchy `browser_snapshot` returns. |
| Task lifecycle | One user request delegated to the agent, ending when the compiled graph reaches a terminal task state. |
| ToolRegistry | Lazy registry that exposes tools from static lists, providers, or MCP clients. |
