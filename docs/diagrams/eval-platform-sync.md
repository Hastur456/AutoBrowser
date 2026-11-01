# Eval Platform Sync

How a finished batch folder becomes a Langfuse dataset version, experiment and scores, and the
states of one experiment trace. See the [Eval Platform Guide](../development/eval-platform.md) and
[ADR-2026-10-31](../decisions/2026-10-31-langfuse-platform-integration.md).

## Components and Boundaries

```mermaid
flowchart TB
    subgraph SRC["src/ (no vendor names)"]
        CFG["config.py: EvalPlatformSettings (eval_platform)"]
        REC["agent_loop/platform_records.py<br/>dataset items, score records, definitions"]
        SPANS["harness/otel_spans.py<br/>task span tree, OTLP/JSON"]
        BATCH["agent_loop: batch, failures, graders, batch_compare"]
    end
    subgraph SCRIPTS["scripts/ (developer tools)"]
        RB["run_batch.py: sync after the batch"]
        CB["compare_batches.py: experiment links"]
        CLI["langfuse_sync.py"]
        subgraph BR["langfuse_bridge/"]
            CL["client.py: REST + OTLP/JSON, retries, rate limit"]
            DS["datasets.py"]
            EX["experiments.py: item traces"]
            SC["scores.py: configs, scores, queue, pull"]
            LG["ledger.py: platform_sync.json + lock"]
            SY["sync.py: push_batch()"]
        end
    end
    FILES[("batch folder<br/>batch.json · run_index.jsonl · traces/ · tasks.jsonl")]
    LF[("Langfuse<br/>datasets · experiments · scores · queue")]
    RB --> BATCH --> FILES
    RB --> SY
    CLI --> SY & DS & SC
    SY --> DS & EX & SC & LG
    EX --> SPANS
    DS & SC --> REC
    SC --> BATCH
    SY --> FILES
    DS & EX & SC --> CL --> LF
    CB --> FILES
```

## One Sync

```mermaid
sequenceDiagram
    autonumber
    participant RB as run_batch.py / langfuse_sync.py
    participant S as push_batch()
    participant L as platform_sync.json
    participant LF as Langfuse
    RB->>S: batch folder, budget
    S->>L: lock (O_EXCL), read state
    S->>LF: GET health, projects
    alt unreachable or rejected
        S->>L: pending, last_error
    else reachable
        S->>LF: dataset push (only changed items) when the batch ran on the current set
        loop every scenario not exported
            S->>L: sending
            S->>LF: POST /otel/v1/traces (item trace)
            S->>L: exported, or unknown on a timeout after sending
        end
        S->>LF: POST /scores (fixed ids; 207 recorded, not retried)
        S->>LF: annotation queue items (new failures)
        S->>L: complete, experiment url
    end
    S-->>RB: report (the batch's exit code is unchanged)
```

## Experiment Trace States

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> sending: start of the request
    sending --> exported: 2xx
    sending --> unknown: timeout after sending
    sending --> pending: refused / 4xx / 5xx (retries spent)
    unknown --> exported: observations API finds it
    unknown --> pending: missing after verify_grace_seconds
    exported --> [*]
```
