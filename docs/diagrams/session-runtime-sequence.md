# Session Runtime Sequence

This diagram shows the long-lived CLI session lifecycle. A terminal agent state
ends the current task only; the session keeps waiting for another user request
until the process is interrupted or an exit command is entered. Useful agent
context is carried across tasks through a session-scoped LangGraph thread and
`SessionContext.state`.

```mermaid
sequenceDiagram
  participant User
  participant CLI as main.py
  participant Session as SessionRuntime
  participant Context as SessionContext
  participant Files as .autobrowser
  participant Harness as BrowserHarness
  participant Memory as MemoryManager
  participant Agent as LangGraph Agent
  participant MCP as MCP/Browser Tools

  User->>CLI: Start process with optional task
  CLI->>Session: Build config and run session
  Session->>Context: initialize()
  Context->>Context: Create workspace and metadata
  Context->>Context: Create model, memory, tools, telemetry
  Context->>Files: Write session.json
  opt Browser tools enabled
    Context->>MCP: Start Chrome/CDP and load tools
  end
  Context->>Harness: Build one harness for the session
  loop For each user task
    User->>Session: Submit task
    Session->>Context: reset_task(task)
    Context->>Files: Update session.json and tasks.json
    Session->>Context: Build carried state and reset task-local fields
    Session->>Harness: run_task(task, session thread_id, state overrides)
    Harness->>Agent: Invoke compiled graph with current task
    Agent->>MCP: Execute approved tool calls
    MCP-->>Agent: Tool results and snapshots
    Agent-->>Harness: Terminal task state
    Harness-->>Session: Final task result
    Session->>Harness: Read latest checkpoint state
    Session->>Context: Remember messages, observation, snapshot, browser state
    Session->>Context: finish_task(result)
    Context->>Files: Update session.json and tasks.json
    Session-->>User: Print result and prompt again
  end
  User->>Session: Ctrl+C, EOF, quit, or exit
  Session->>Context: close()
  Context->>Files: Mark session closed
  Context->>MCP: Close MCP session when enabled
```
