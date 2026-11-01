# Trace Export

How a session's events reach a trace backend over OTLP, and the span tree of one task. See the
[Trace Export Guide](../development/telemetry.md) and
[ADR-2026-10-29: Neutral OTLP Trace Export](../decisions/2026-10-29-otlp-trace-export.md).

## Events to Sinks to the Backend

```mermaid
flowchart LR
  Engine["AgentLoopEngine / GoalRunner<br/>(src/agent_loop)"] -->|EventRecord| Emitter[EventEmitter]
  Session["SessionContext<br/>session.started / session.closed"] -->|EventRecord| Emitter
  Emitter --> Composite[CompositeEventSink]
  Composite --> Jsonl["JsonlEventSink<br/>events.jsonl"]
  Composite --> Trace["AgentTraceSink<br/>agent_trace.jsonl"]
  Composite --> Extra["extra sinks<br/>(EventBroadcaster for the UI)"]
  Composite -->|"telemetry.otlp_enabled<br/>and config.otlp_export"| Otlp[OtlpEventSink]
  Otlp --> Buffer["task buffer<br/>goal.started .. terminal"]
  Buffer --> Specs["task_span_specs<br/>(otel_spans.py, pure)"]
  Specs --> Provider["TracerProvider<br/>+ BatchSpanProcessor"]
  Provider -->|"OTLP/HTTP<br/>endpoint + secret headers"| Backend{{"Langfuse (primary)<br/>Phoenix / Jaeger"}}
  Session -.->|"session.closed: export unfinished task,<br/>force_flush, shutdown"| Otlp
```

`src/agent_loop/` only emits events: it imports neither the sink nor OpenTelemetry
(`tests/test_engine_boundaries.py`). `capture_content`, the environment and the attribute
aliases are applied in `task_span_specs`; the SDK only materializes the specs, each span once.

## Span Tree of One Task

```mermaid
flowchart TD
  Root["invoke_agent autobrowser<br/>task id, goal status, stop_reason<br/>events: plan.updated, gate decisions"]
  Root --> Plan["chat {model}<br/>phase plan, usage"]
  Root --> Agent1["chat {model}<br/>phase agent, usage"]
  Root --> Tool1["execute_tool {tool}<br/>server, call id, status"]
  Root --> Judge["chat {judge model}<br/>phase approval (classifier on)"]
  Root --> Agent2["chat {model}<br/>phase agent"]
  Common["on every span: session.id, labels<br/>(batch id, scenario id, commit),<br/>deployment.environment.name, aliases"]
  Common -.-> Root
```
