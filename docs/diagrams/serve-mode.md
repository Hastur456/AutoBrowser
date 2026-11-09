# Serve Mode

Accepted design (2026-11-02), implemented in full: replay and budgets (phase 1, 2026-11-02),
the minimal stand (phase 3, 2026-11-09) and phase 4 (2026-11-09): the worker pool, model tiers
(cascade) and the Telegram channel. It covers the stand process (`autobrowser serve`), the cost
ladder and replay (playback of a recording) inside the engine. Guide:
[serve mode](../development/serve-mode.md).
Design: [serve mode design](../development/2026-11-02-serve-mode-design.md),
[phase 4 design](../development/2026-11-08-serve-mode-phase4-design.md);
ADRs: [2026-11-02 serve mode and cost ladder](../decisions/2026-11-02-serve-mode-and-cost-ladder.md),
[2026-11-09 cascade, chat channels and the worker pool](../decisions/2026-11-09-cascade-channels-and-worker-pool.md).

## Components and Boundaries

`src/serve/` is a client of `SessionController`, as the terminal UI is. The engine sees only
the neutral protocols in `src/contracts.py`. Playback lives in `src/harness/`, page-format
knowledge lives in `src/browser/`, and vendor channels live in `src/channels/`. Only
`src/cli/` wires these pieces together.

```mermaid
flowchart TB
  subgraph CLI["src/cli (composition only)"]
    BOOT["run_serve_cli: sessions, brokers, channels"]
    MOD["models.py: cascade_factory<br/>(registry.build_chat_model)"]
  end
  subgraph SERVE["src/serve"]
    HTTP["ServeApp (starlette + uvicorn)<br/>/healthz, /v1/*, webhooks"]
    SCH["Scheduler: interval, cron"]
    DIS["Dispatcher<br/>queue, coalesce, cooldown, day budget,<br/>exclusion keys, day reservations"]
    POOL["WorkerPool: serve.workers slots, pins"]
    CMD["commands.py: CommandPort, ServeCommands"]
    DESK["ApprovalDesk over one ApprovalBridge per worker"]
    LED[("serve/ledger.jsonl<br/>RunReport")]
    NOTE["NotificationChannel: log, webhook-out, telegram"]
  end
  TG["src/channels/telegram<br/>BotApi, TelegramChannel (long polling)"]
  subgraph WORKER["worker w&lt;i&gt; (one per serve.workers)"]
    CTL["SessionController"]
    SR["SessionRuntime / SessionContext<br/>profile w&lt;i&gt;, CDP port base + i"]
    AB["ApprovalBroker"]
    EB["EventBroadcaster"]
    REC["Recorder (EventSink)"]
  end
  subgraph ENGINE["src/agent_loop/execution"]
    ENG["AgentLoopEngine / TurnController"]
  end
  PB["src/harness/playback<br/>PlaybackFactory, PlaybackSource, Retargeter"]
  RS[("recordings/&lt;name&gt;/recording.json")]
  BA["src/browser/playback<br/>BrowserPlaybackAdapter"]
  MCP["MCP servers (Playwright: headless, own profile)"]

  BOOT --> HTTP & SCH & DIS & POOL & NOTE
  BOOT --> TG
  BOOT --> MOD
  MOD -. "CascadeFactory" .-> SR
  HTTP --> DIS
  HTTP --> DESK
  SCH --> DIS
  TG -- "commands, button answers" --> CMD
  CMD --> DIS
  CMD --> DESK
  DESK --> AB
  TG -- "implements" --> NOTE
  DIS --> POOL --> CTL --> SR --> ENG
  SR --> AB & EB & REC
  REC --> RS
  ENG -- "StepSource protocol" --> PB
  PB --> RS
  PB -- "PlaybackAdapter protocol" --> BA
  PB -- "ChatModel (retarget tier)" --> LLM["ChatModel tiers"]
  ENG -- "ChatModel (model_tier)" --> LLM
  ENG --> MCP
  CTL -- "TaskOutcome + TaskUsage" --> DIS
  DIS --> LED
  DIS --> NOTE
  AB -. "tickets (bridge)" .-> NOTE
```

Import fences (tests):

- `src/serve` does not import `src.agent_loop.execution`, `src.browser`, `src.cli`,
  `src.providers`, `src.harness` or `src.channels` (`tests/test_serve_boundaries.py`).
- `src/channels` imports only `src.config`, `src.contracts` and the neutral ports
  `src.serve.{commands,notify,clock}`; only `src/cli/` imports it; the vendor host appears in
  `src/channels/telegram.py` only; neither package uses threads.
- `src/agent_loop`, `src/harness`, `src/ui`, `src/browser`, `src/mcp` and `src/providers` do
  not import `src.serve`; only `src/cli/serve.py` composes the stand.
- The engine imports no `src.browser` and holds no `browser_*` literals.

## Cost Ladder

A run starts at the lowest rung its job allows. It climbs only when the rung below cannot
finish, and never above `RunProfile.max_stage`. Each climb emits `ladder.escalated`.

```mermaid
stateDiagram-v2
  [*] --> Gate: trigger fired
  Gate --> Skipped: coalesced / cooldown / day budget
  Gate --> Probe: job.probe
  Gate --> Playback: recording, no probe
  Gate --> Agent: nothing recorded
  Probe --> Unchanged: region digest unchanged
  Probe --> Playback: changed + recording
  Probe --> Agent: changed, no recording
  Unchanged --> [*]: 0 model calls
  Playback --> Done: steps matched, answer=output/summary
  Playback --> Retarget: binding / tool_error
  Retarget --> Playback: step accepted
  Retarget --> Agent: rejected / limit
  Playback --> Agent: checkpoint / catalog / denied / answer=agent
  Agent --> AgentStrong: tier_escalation (cascade)
  Agent --> Human: ask via approval channel
  AgentStrong --> Human: ask via approval channel
  Human --> Agent: approved or non-terminal deny
  Human --> Blocked: refused
  Agent --> Done
  AgentStrong --> Done
  Agent --> Blocked: guards / budget_exhausted
  Playback --> Blocked: ladder_cap
  Done --> [*]
  Blocked --> [*]
  Skipped --> [*]
```

## One Replayed Step

Every proposal goes through the normal tool pipeline, whether it comes from the recording, the
retarget model or the agent.

```mermaid
sequenceDiagram
  autonumber
  participant E as TurnController
  participant S as PlaybackSource
  participant A as BrowserPlaybackAdapter
  participant R as Retargeter (ChatModel)
  participant P as guard / hooks / PermissionEngine / ToolBroker

  E->>S: next(StepView: latest output, snapshots, catalog)
  S->>S: catalog check (schema digest, readOnlyHint)
  S->>A: rebind(args, bindings, page texts)
  alt exactly one matching element
    A-->>S: args with fresh [ref=...]
  else none or ambiguous
    A-->>S: None (divergence: binding, before any call)
    S->>R: complete(task, recorded step, page) with one ToolDef
    R-->>S: one tool call (validated: refs exist on page)
  end
  S-->>E: StepProposal(stage, step, plan_step)
  E->>E: synthetic assistant tool call in messages
  E->>P: progress guard, pre_tool_use, permissions, approval, invoke, post_tool_use
  P-->>E: ToolResult + observation
  E->>S: observe(result)
  S->>A: checkpoint(output) vs recorded
  alt similarity below threshold / error / denied
    S-->>E: StepHandoff on the next turn
    E->>E: ladder.escalated playback->agent, [harness] note, model drives
  end
```

## Model Tier Escalation

With `cascade.enabled` the agent starts on tier 0 (the session model) and the engine checks,
before every turn, whether it is stuck. The tier only goes up; call #0 is always tier 0, a
replan runs on the current tier, a retarget on `cascade.retarget_tier`.

```mermaid
flowchart TB
  TOP{{"loop top (AgentLoopEngine.run)"}} --> ONE{"one tier?"}
  ONE -- yes --> TURN
  ONE -- no --> TE["guards.tier_escalation(state, cascade, tier_count)"]
  TE -- "no_action / progress_block /<br/>failures / replans" --> UP["model_tier + 1, marks moved,<br/>consecutive_failures = 0<br/>ladder.escalated{from_tier, tier, model}"]
  TE -- none --> TURN
  UP --> TURN["turn on tiers[model_tier]<br/>model.* {tier, model}"]
  TURN -- "empty answer, higher tier" --> SIG["decision continue,<br/>tier_signal no_action"]
  TURN -- "progress block" --> PB["progress_blocks + 1"]
  SIG --> TOP
  PB --> TOP
  TURN -- "tool call / replan" --> TOP
```

## Telegram Channel

Long polling, no inbound port. The offset is saved before the updates are handled, so a
restart never repeats a command.

```mermaid
sequenceDiagram
  autonumber
  participant U as Allowed chat
  participant T as TelegramChannel.run
  participant B as Bot API (httpx)
  participant C as ServeCommands
  participant D as Dispatcher
  participant K as ApprovalDesk

  T->>B: getUpdates(offset, timeout=poll_timeout_seconds)
  B-->>T: updates
  T->>T: save offset (channels/telegram.json)
  alt chat not in allowed_chat_ids
    T->>C: rejected(sender = HMAC of the chat id)
  else /run price-watch (job has a telegram trigger)
    T->>C: handle(ChatCommand run)
    C->>D: fire(job, TriggerRef(telegram, trigger))
    C-->>T: reply text
    T->>B: sendMessage (plain text, no link preview)
  else button a:<ticket>:o
    T->>C: answer_approval(ticket, once)
    C->>K: answer(ticket, once, via=channel)
    T->>B: answerCallbackQuery, clear the keyboard
  end
```

## Worker Pool

```mermaid
flowchart LR
  Q[("FIFO queue")] --> S{"for each queued run, in order"}
  S --> K{"job:id / recording:name<br/>held by a running run?"}
  K -- yes --> W["stays queued"]
  K -- no --> P{"a free worker it may use?<br/>(its pin, else unreserved)"}
  P -- no --> W
  P -- yes --> R{"day room after<br/>the running runs' reservations?"}
  R -- no --> W
  R -- yes --> RUN["reserve its budget, run on w&lt;i&gt;<br/>report with worker"]
  RUN --> F["release slot and reservation,<br/>session_max_runs of w&lt;i&gt;"]
  F --> S
```

