# Session Runtime Sequence

This diagram shows the long-lived session lifecycle. A terminal `AgentLoopResult` ends the
current task only; the session keeps waiting for another user request until the process is
interrupted or an exit command is entered. Useful agent context is carried across tasks
through the terminal result's `session_state` and `SessionContext.state`, and written to
`<session>/state.json` so a later process can continue the session (`--continue`,
`--resume`). Session files live in the AutoBrowser home (`AUTOBROWSER_HOME`, else
`~/.autobrowser`).

```mermaid
sequenceDiagram
  participant User
  participant CLI as autobrowser / main.py
  participant Session as SessionRuntime
  participant Context as SessionContext
  participant Files as <home>/sessions/<id>
  participant Harness as BrowserHarness
  participant Goals as GoalRunner
  participant Engine as AgentLoopEngine
  participant MCP as MCP/Browser Tools

  User->>CLI: Start, optionally --continue / --resume <id>
  alt resume
    CLI->>Files: Pick the session (has tasks + state.json, its .lock not held)
    CLI->>Session: SessionRuntime(resume_from=dir)
    Session->>Context: SessionContext.restore(dir)
    Context->>Files: Take the OS lock on .lock (held until close; refused if held)
    Context->>Files: Read session.json, tasks.json, state.json (dialogue only), last event sequence
  else new session
    CLI->>Session: SessionRuntime(config)
  end
  Note over Session,Context: Resources start lazily with the first task
  User->>Session: Submit task
  Session->>Context: initialize()
  Context->>Files: New session: take the OS lock on .lock (a resumed one already holds it)
  Context->>Context: Workspace, model, tools, telemetry; events continue the stored sequence
  opt Browser tools enabled
    Context->>MCP: Start Chrome/CDP and load tools
  end
  Context->>Harness: Build one harness for the session
  Session->>Context: build_agent() -> one Agent for the session
  loop For each user task
    Session->>Context: reset_task(task)
    Context->>Files: Update session.json, tasks.json, state.json
    Session->>Context: Build carried state and reset task-local fields
    Session->>Goals: run(TaskRequest: task, ids, carried state, turn cap)
    Goals->>Engine: Agent.run(TaskRequest) -> AgentLoopEngine.run
    Engine->>Engine: plan + bounded TurnController loop
    Engine->>MCP: Execute approved tool calls
    MCP-->>Engine: Tool results and snapshots
    Engine-->>Goals: Terminal AgentLoopResult
    Goals-->>Session: GoalRunResult with explicit status
    Session->>Context: Remember result.session_state (messages, observation, snapshots)
    Session->>Context: finish_task(result) (also when memory consolidation is cancelled)
    Context->>Files: Update session.json, tasks.json, state.json
    Session-->>User: Show the outcome and wait for the next task
  end
  User->>Session: Ctrl+C x2, Ctrl+D, /exit
  CLI->>Session: SessionController.shutdown(bounded) then close()
  Session->>Context: close()
  Context->>MCP: Close MCP, then Chrome
  Context->>Files: Persist (only the lock holder writes; a never-started session holds none), release the lock
```

A resumed session carries the dialogue only: page state (observation, kept snapshots, last
browser action) described a browser that is gone after a restart.
