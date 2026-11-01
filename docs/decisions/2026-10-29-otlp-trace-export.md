# ADR-2026-10-29: Neutral OTLP Trace Export, Langfuse as the Primary Receiver

Status: Accepted
Date: 2026-10-29

## Context

[ADR-2026-10-19](2026-10-19-langfuse-observability.md) kept diagnostics local and deferred a
neutral OpenTelemetry export "until there is a consumer of traces". The consumer now exists: the
user wants to look at AutoBrowser runs in a trace UI — first of all in **Langfuse**, and possibly
in a local Arize Phoenix or Jaeger — without reading `events.jsonl` by hand.

The forces:

- `events.jsonl` already holds everything a trace needs (goal, model and tool event pairs with
  timestamps, token usage, decisions), redacted by `redact_json_safe`.
- `src/` must stay vendor-neutral: no vendor names or URLs, no logic keyed on tool or site names.
- Page text, task text and tool output are private; nothing may leave the machine unless the
  user asks for it.
- A batch builds one session per scenario, so a per-session exporter must not leak threads.
- Langfuse v4 ingests OTLP/HTTP at `/api/public/otel/v1/traces`, reads `session.id`,
  `gen_ai.*`, `input.value`/`output.value` and `deployment.environment.name` without vendor
  keys, but reads trace-filterable metadata and the release only from `langfuse.*` keys; a span
  must be sent exactly once (see the Langfuse integration design §2).

## Decision

1. **A neutral OTLP exporter is introduced**, the deferred option of ADR-2026-10-19. A session
   event sink (`src/harness/otel_sink.py`) turns each task's events into one trace; a pure
   builder (`src/harness/otel_spans.py`) computes the span tree without the SDK, with stable
   span keys from the opening events' `event_id` and an OTLP/JSON encoder for later reuse.
2. **Span model**: a root `invoke_agent autobrowser` per task (`gen_ai.agent.name`), `chat
   {model}` children paired by model phase, `execute_tool {tool}` children in FIFO order;
   decisions and plan updates are span events on the root. OpenTelemetry GenAI attribute names
   where one exists, `autobrowser.*` otherwise. `session.id`, the session labels (batch id,
   scenario id, commit) and `deployment.environment.name` sit on every span.
3. **Each span is exported exactly once**: a task's spans are built and ended at its terminal
   event; spans left open end there with `autobrowser.incomplete=true`; `session.closed` exports
   an unfinished task, flushes within `telemetry.otlp_timeout_seconds` and shuts the provider
   down.
4. **Off by default, content opt-in.** `telemetry.otlp_enabled=false`;
   `telemetry.capture_content="none"` sends only ids, names, counts, statuses and durations.
   `"redacted"` adds the task text, final answer, tool arguments/results and decision reasons,
   taken only from the redacted, truncated event records.
5. **The backend is configuration.** Endpoint and headers (`telemetry.otlp_headers`, secret) pick
   the receiver. Backend-specific keys come from `telemetry.attribute_aliases` (copy attribute A
   also under key B), so the Langfuse keys (`langfuse.trace.metadata.*`, `langfuse.release`)
   live only in the templates and docs. Langfuse is the documented primary receiver, with the
   `x-langfuse-ingestion-version: 4` header; Phoenix and Jaeger are documented alternatives.
6. **No Langfuse SDK.** The exporter is the plain `opentelemetry-exporter-otlp-proto-http`,
   pinned to the SDK version already installed, in an optional `otel` dependency group; enabling
   export without it fails session start with an install hint. `SessionConfig.otlp_export` lets
   a run opt out of the live export (the dataset/experiment bridge of the Proposed
   [ADR-2026-10-31](2026-10-31-langfuse-platform-integration.md) will send batch runs as
   experiment traces instead).
7. **Fences**: `src/agent_loop/` imports neither the export modules nor OpenTelemetry; no file
   under `src/` names a trace backend (`tests/test_engine_boundaries.py`).

## Consequences

- Runs are visible in Langfuse as sessions of traces with generations (model, tokens), tool
  observations, error levels and the `batch` environment; batch runs can be filtered by batch,
  scenario and release once the aliases are configured.
- The local verdict stays authoritative: `failure_kind`, grades and comparisons are computed
  after the run and are not on the spans; a backend user reaches them through the batch and
  scenario ids.
- A task's spans appear when the task ends, not while it runs, and a killed process loses its
  in-flight task. In exchange no span is ever exported twice.
- The model name does not travel in the events: the session hands the sink its model identity
  at each task start. An offline bridge must take it from the batch provenance.
- The GenAI semantic conventions are still in development; the attribute names live in one
  module (`otel_spans.py`), so a rename touches one place.
- Follow-up: the Langfuse dataset, experiment and score bridge (`feat/langfuse-sync`) reuses
  `task_span_specs` and `encode_otlp_json`.

## Alternatives Considered

- **Langfuse Python SDK.** Rejected: v5 requires OpenTelemetry ≥ 1.45 (the project pins 1.39.1),
  three major versions in 16 months, and it would put a vendor into `src/`.
- **Vendor keys in code** (`langfuse.*` set by the sink). Rejected: the aliases give the same
  result from configuration and keep `src/` neutral.
- **Streaming spans as events arrive.** Rejected: a span re-sent on update may duplicate in
  Langfuse v4, and open spans would need a second export; per-task export keeps one export per
  span.
- **Phoenix-first default endpoint.** Rejected: the default is the standard local OTLP port
  (`http://localhost:4318/v1/traces`); hosted URLs belong in the templates and docs.

## Related

- Extends the deferred option of
  [ADR-2026-10-19: Langfuse не вводим; нейтральный OTLP-экспорт как отложенная опция](2026-10-19-langfuse-observability.md).
- Proposed follow-up: [ADR-2026-10-31: Langfuse platform integration](2026-10-31-langfuse-platform-integration.md)
  and its design, amendments PB-1..PB-8:
  [docs/development/2026-10-31-langfuse-integration-design.md](../development/2026-10-31-langfuse-integration-design.md) §5.8.
- Plan, Part B: [docs/development/2026-10-29-batch-analytics-and-otlp-export-plan.md](../development/2026-10-29-batch-analytics-and-otlp-export-plan.md).
- [ADR-2026-10-19: Autonomous test pipeline](2026-10-19-autonomous-test-pipeline.md).
- Guide: [docs/development/telemetry.md](../development/telemetry.md); diagram:
  [docs/diagrams/trace-export.md](../diagrams/trace-export.md).
- Code: `src/harness/otel_spans.py`, `src/harness/otel_sink.py`, `src/harness/session.py`
  (`_telemetry_sinks`), `src/config.py` (`TelemetrySettings`), `src/cli/subcommands.py`
  (`doctor` telemetry line), `scripts/run_batch.py` (the `batch` environment label).
