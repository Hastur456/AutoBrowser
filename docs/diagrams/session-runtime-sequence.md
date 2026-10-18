# Session Runtime Sequence

This diagram shows the long-lived CLI session lifecycle. A terminal
`AgentLoopResult` ends the current task only; the session keeps waiting for
another user request until the process is interrupted or an exit command is
entered. Useful agent context is carried across tasks through the terminal
result's `session_state` and `SessionContext.state`.

```mermaid
sequenceDiagram
  participant User
  participant CLI as main.py
  participant Session as SessionRuntime
  participant Context as SessionContext
  participant Files as .autobrowser
  participant Harness as BrowserHarness
  participant Goals as GoalRunner
  participant Engine as AgentLoopEngine
  participant MCP as MCP/Browser Tools

  User->>CLI: Start process with optional task
  CLI->>Session: Build config and run session
  Session->>Context: initialize()
  Context->>Context: Create workspace and metadata
  Context->>Context: Create model, tools, telemetry
  Context->>Files: Write session.json
  opt Browser tools enabled
    Context->>MCP: Start Chrome/CDP and load tools
  end
  Context->>Harness: Build one harness for the session
  Session->>Context: build_agent() -> one Agent for the session
  loop For each user task
    User->>Session: Submit task
    Session->>Context: reset_task(task)
    Context->>Files: Update session.json and tasks.json
    Session->>Context: Build carried state and reset task-local fields
    Session->>Goals: run(TaskRequest: task, ids, carried state, turn cap)
    Goals->>Engine: Agent.run(TaskRequest) -> AgentLoopEngine.run
    Engine->>Engine: plan + bounded TurnController loop
    Engine->>MCP: Execute approved tool calls
    MCP-->>Engine: Tool results and snapshots
    Engine-->>Goals: Terminal AgentLoopResult
    Goals-->>Session: GoalRunResult with explicit status
    Session->>Context: Remember result.session_state (messages, observation, snapshot)
    Session->>Context: finish_task(result)
    Context->>Files: Update session.json and tasks.json
    Session-->>User: Print final answer and prompt again
  end
  User->>Session: Ctrl+C, EOF, quit, or exit
  Session->>Context: close()
  Context->>Files: Mark session closed
  Context->>MCP: Close MCP session when enabled
```
