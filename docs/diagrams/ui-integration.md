# UI Integration

How the terminal UI talks to the session (`src/ui/`, `src/cli/bootstrap.run_frontend_cli`).
Design: [terminal UI design](../development/2026-10-19-gui-redesign-design.md) §5.3–§5.4;
guide: [terminal UI](../development/terminal-ui.md).

## Threads and the single writer

```mermaid
flowchart LR
  subgraph Main["Main thread: UI loop (prompt_toolkit)"]
    APP[TuiApp<br/>Application full_screen=False]
    PUMP[RenderPump<br/>only terminal writer]
    Q[(UI queue)]
    KEYS[keys / input / slash commands]
  end
  subgraph RT["RuntimeLoop thread: session loop"]
    CTL[SessionController]
    SR[SessionRuntime] --> GR[GoalRunner] --> AG[Agent] --> EN[AgentLoopEngine]
    EE[EventEmitter] --> CS[CompositeEventSink]
    CS --> JS[events.jsonl]
    CS --> AT[agent_trace.jsonl]
    CS --> EB[EventBroadcaster]
    AB[ApprovalBroker]
  end
  LOG[(ui.log_file: logging, warnings, tracebacks<br/>session_dir/stderr.log: MCP stderr)]
  TERM[[Terminal]]

  KEYS -- "RuntimeBridge: submit / cancel / reset" --> CTL
  KEYS -- "answer (thread-safe)" --> AB
  CTL --> SR
  EN --> EE
  EN -- HumanInputCallback --> AB
  EB -- StreamItem --> CTL
  CTL -- "project_event → FeedItem" --> Q
  AB -- "ApprovalTicket / ApprovalResolution" --> Q
  Q -- call_soon_threadsafe --> PUMP
  PUMP -- "run_in_terminal: frozen blocks" --> TERM
  PUMP -- "invalidate: live zone ≤ ui.live_max_lines" --> APP --> TERM
  RT -. "output_fn → OutputRelay → notice" .-> Q
  RT -. never .-> LOG
```

## A task with an approval

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant T as TuiApp / RenderPump
  participant C as SessionController
  participant E as Engine (via SessionRuntime/GoalRunner)
  participant B as ApprovalBroker
  participant S as EventBroadcaster

  U->>T: Enter "task"
  T->>T: freeze "user" block into the scrollback
  T->>C: submit(text) on the RuntimeLoop
  C->>E: run_task (background)
  E->>S: goal.started, plan.updated, turn.started
  S-->>T: plan (live), spinner
  E->>S: action.proposed, tool.started, tool.finished, observation.compiled
  S-->>T: call block live → frozen
  E->>B: human_input(request, reason, verdict)
  B-->>T: ApprovalTicket → card (live)
  U->>T: ↓ Enter
  T->>B: answer(ticket, "session", via="tui")
  B->>S: approval.answered
  B-->>T: ApprovalResolution → one-line summary (frozen)
  E->>S: goal.completed {status, final_answer, turns}
  S-->>T: outcome block (markdown) frozen, spinner gone
```

Esc or Ctrl+C during a task call `SessionController.cancel()`; `GoalRunner` emits
`goal.cancelled` and the session closes the `TaskRecord`.
