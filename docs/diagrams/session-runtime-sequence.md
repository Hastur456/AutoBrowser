# Session Runtime Sequence

This diagram shows the long-lived CLI session lifecycle. A terminal agent state
ends the current task only; the session keeps waiting for another user request
until the process is interrupted or an exit command is entered.

```mermaid
sequenceDiagram
  participant User
  participant CLI as main.py
  participant Session as SessionRuntime
  participant Harness as BrowserHarness
  participant Agent as LangGraph Agent
  participant MCP as MCP/Browser Tools

  User->>CLI: Start process with optional task
  CLI->>Session: Build config and run session
  Session->>Session: Initialize model and task config
  opt Browser tools enabled
    Session->>MCP: Start Chrome/CDP and load tools
  end
  Session->>Harness: Build one harness for the session
  loop For each user task
    User->>Session: Submit task
    Session->>Harness: run_task(task)
    Harness->>Agent: Invoke compiled graph
    Agent->>MCP: Execute approved tool calls
    MCP-->>Agent: Tool results and snapshots
    Agent-->>Harness: Terminal task state
    Harness-->>Session: Final task result
    Session-->>User: Print result and prompt again
  end
  User->>Session: Ctrl+C, EOF, quit, or exit
  Session->>MCP: Close MCP session when enabled
```
