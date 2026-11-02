# Serve Mode

Accepted design (2026-11-02). This diagram shows the design, not the code that exists today. It covers the stand
process (`autobrowser serve`), the cost ladder and replay (playback of a recording) inside the
engine.
Design: [serve mode design](../development/2026-11-02-serve-mode-design.md);
ADR: [2026-11-02 serve mode and cost ladder](../decisions/2026-11-02-serve-mode-and-cost-ladder.md).

## Components and Boundaries

`src/serve/` is a client of `SessionController`, as the terminal UI is. The engine sees only
the neutral protocols in `src/contracts.py`. Playback lives in `src/harness/`, page-format
knowledge lives in `src/browser/`, and vendor channels live in `src/channels/`. Only
`src/cli/` wires these pieces together.

```mermaid
flowchart TB
  subgraph CLI["src/cli (composition only)"]
    BOOT["run_serve_cli: models, sessions, brokers, channels"]
  end
  subgraph SERVE["src/serve"]
    HTTP["ServeApp (starlette + uvicorn)<br/>/healthz, /v1/*, webhooks"]
    SCH["Scheduler: interval, cron"]
    DIS["Dispatcher<br/>queue, coalesce, cooldown, day budget"]
    POOL["WorkerPool (1 worker until phase 4)"]
    LED[("serve/ledger.jsonl<br/>RunReport")]
    NOTE["NotificationChannel: log, webhook-out"]
  end
  TG["src/channels/telegram<br/>(phase 4, long polling)"]
  subgraph WORKER["worker w0"]
    CTL["SessionController"]
    SR["SessionRuntime / SessionContext"]
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
  BOOT -.-> TG
  HTTP --> DIS
  SCH --> DIS
  TG -. "/run, approvals" .-> DIS
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
  DIS -.-> TG
  AB -. "tickets" .-> TG
```

Import fences (tests):

- `src/serve` does not import `src.agent_loop.execution`, `src.browser`, `src.cli`,
  `src.providers` or `src.channels`.
- `src/agent_loop` and `src/harness` do not import `src.serve` or `src.channels`.
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
  Agent --> AgentStrong: tier_escalation (phase 4)
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
