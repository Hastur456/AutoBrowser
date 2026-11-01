# Trace Export Guide (OTLP)

How to look at AutoBrowser runs in a trace UI: what is exported, how to switch it on, how to
send the traces to **Langfuse** (the primary receiver) or to a local Arize Phoenix or Jaeger,
what leaves the machine, and how to find a batch scenario. The decision record is
[ADR-2026-10-29: Neutral OTLP Trace Export](../decisions/2026-10-29-otlp-trace-export.md); the
data flow is drawn in [Trace Export](../diagrams/trace-export.md).

The export is **off by default**. The local files (`events.jsonl`, `agent_trace.jsonl`, batch
reports) stay the source of truth either way: grades, `failure_kind` and run comparisons are
computed locally and are not on the spans.

## What Is Exported

Every task of a session becomes **one trace**, built when the task ends and sent once:

| Span | Opened by → closed by | Key attributes |
|---|---|---|
| `invoke_agent autobrowser` (root) | `goal.started` → the terminal goal event | `gen_ai.agent.name`, `gen_ai.conversation.id`, `autobrowser.task.id`, `autobrowser.goal.status`, `autobrowser.stop_reason`, `autobrowser.turns`, `error.type` |
| `chat {model}` | `model.requested` → `model.responded` of the same `phase` | `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.usage.input_tokens`/`output_tokens`, `autobrowser.model.phase` (`plan`/`agent`/`approval`), `autobrowser.usage.cached_input_tokens`/`reasoning_tokens`, `autobrowser.model.provider_ms`/`load_ms` |
| `execute_tool {tool}` | `tool.started` → `tool.finished` (FIFO) | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `autobrowser.tool.server`, `error.type` |

- **Span events on the root**: `plan.updated` (step counts), `permission.decided` other than
  `allow`, `approval.requested`/`answered`/`resolved`, `hook.decided` deny/ask,
  `policy.decided` blocked — with `decision`, `source`, `rule_id`, `tool` and similar ids.
- **On every span**: `session.id`, the session labels (a batch adds `autobrowser.batch.id`,
  `autobrowser.scenario.id`, `vcs.ref.head.revision`) and `deployment.environment.name`.
- **Errors**: a failed, blocked or cancelled task and a failed tool call are spans with status
  ERROR. A span still open when the task ends (a cancelled tool call) ends at the terminal event
  with `autobrowser.incomplete=true`; a task the session closed before it finished likewise.
- Resource attributes: `service.name` (`telemetry.service_name`) and `service.version`.

## Enabling

1. Install the exporter (an optional dependency group, not installed by `uv sync`):

   ```powershell
   uv sync --group otel
   ```

2. Configure the `telemetry` section (env, `<home>/.env`, or `config.yaml`; keep the headers
   in `<home>/.env`, they are secrets):

   | Setting | Default | Meaning |
   |---|---|---|
   | `otlp_enabled` | `false` | export every session's tasks |
   | `otlp_endpoint` | `http://localhost:4318/v1/traces` | OTLP/HTTP traces URL of the backend |
   | `otlp_headers` | `{}` | request headers (JSON object in env); secret, never logged or printed |
   | `otlp_timeout_seconds` | `10.0` | one export request, and the flush at session end |
   | `capture_content` | `none` | `none` or `redacted` (see [Content and privacy](#content-and-privacy)) |
   | `service_name` | `autobrowser` | `service.name` of the spans |
   | `environment` | unset | `deployment.environment.name` (e.g. `interactive`); batch runs set `batch` |
   | `attribute_aliases` | `{}` | copy attribute A also under key B, for backend-specific keys |

3. Check it: `uv run main.py doctor` prints a `telemetry` line — `off`, or the endpoint without
   credentials, the content mode, the number of headers and whether the exporter is installed.
   A missing exporter is a failure (`xx`), and a session with export enabled refuses to start
   with the hint `uv sync --group otel`.

## Langfuse

Langfuse ingests OTLP/HTTP (protobuf or JSON, no gRPC) and maps the attributes above itself:
the root becomes the trace (named `invoke_agent autobrowser`), `chat` spans become generations
with model and token usage, `session.id` groups the tasks of one session, `deployment.environment.name`
is the environment, ERROR spans get level ERROR.

**Where.** Langfuse Cloud EU `https://cloud.langfuse.com`, US `https://us.cloud.langfuse.com`, or
a self-hosted Langfuse **v4.59.0 or newer** (older v3 servers accept OTLP from 3.22, but not the
v4 data model this export targets). The endpoint is `<host>/api/public/otel/v1/traces`.

**Keys.** In the Langfuse UI: project → Settings → API Keys → create a key pair (`pk-lf-…`,
`sk-lf-…`). The `Authorization` header is `Basic` and the Base64 of `public:secret`. In
PowerShell, without echoing the secret into the history:

```powershell
$public = Read-Host "Langfuse public key"
$secret = Read-Host "Langfuse secret key" -AsSecureString
$plain = [Net.NetworkCredential]::new("", $secret).Password
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${public}:${plain}"))
```

**`<home>/.env`** (`~/.autobrowser/.env`; never commit it). Both headers are needed: without
`x-langfuse-ingestion-version: 4` a direct OTLP exporter's data shows up only after a delay of
10–15 minutes.

```dotenv
AUTOBROWSER_TELEMETRY__OTLP_ENABLED=true
AUTOBROWSER_TELEMETRY__OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel/v1/traces
AUTOBROWSER_TELEMETRY__OTLP_HEADERS={"Authorization": "Basic <base64 of pk-lf-…:sk-lf-…>", "x-langfuse-ingestion-version": "4"}
AUTOBROWSER_TELEMETRY__ENVIRONMENT=interactive
AUTOBROWSER_TELEMETRY__ATTRIBUTE_ALIASES={"autobrowser.batch.id": "langfuse.trace.metadata.batch_id", "autobrowser.scenario.id": "langfuse.trace.metadata.scenario_id", "vcs.ref.head.revision": "langfuse.release"}
```

**Aliases.** Langfuse filters traces by metadata and sets the release only from its own
`langfuse.*` keys. The aliases above copy the neutral labels into them on every span, so the
code never names the vendor: `batch_id` and `scenario_id` become filterable trace metadata, the
batch's git commit becomes the release. Leave them out and the labels are still on the spans as
ordinary attributes.

**Finding a run.**

- *Sessions*: one session per AutoBrowser session (`session.id`); a multi-task scenario or a
  REPL session is one session with one trace per task.
- *Traces*: filter by environment (`batch` for `scripts/run_batch.py`, your `environment` for
  interactive runs), by release (the commit), or by metadata `batch_id` / `scenario_id` with the
  aliases on. The batch and scenario ids are the ones in `batch.json` and `run_index.jsonl`.
- The local verdict (`grade_status`, `failure_kind`, `compare.md`) stays in the batch folder.

**What you see** with `capture_content=none`: the tree, timings, model names, token counts,
tool names, statuses, stop reasons, decision kinds and ids — no task text, page text, answers,
arguments, results or reasons. With `redacted`: also the task (`input.value` on the root), the
final answer (`output.value`), tool arguments and results (`gen_ai.tool.call.arguments`/`.result`)
and decision reasons, after the event redaction (secret-named keys masked) and truncation
(`events.max_string_chars`). **Page content then leaves the machine** — tool results are page
text; turn it on for a cloud project only if that is acceptable for every site the agent visits.

**Limits of Cloud Hobby** (as of 2026-10-31): data is kept 30 days; the general public REST API
allows 30 requests a minute (OTLP ingestion has its own, higher limit). Scripts that read traces
back through `GET /api/public/traces` must stay under that.

## Arize Phoenix (local)

```powershell
uvx arize-phoenix serve        # or: docker run -p 6006:6006 arizephoenix/phoenix
```

Endpoint `http://localhost:6006/v1/traces`, no headers. Phoenix reads the same `input.value`/
`output.value` keys. Find a scenario by filtering spans on `autobrowser.scenario.id`.

## Jaeger (local)

```powershell
docker run -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
```

The default endpoint `http://localhost:4318/v1/traces` already points at it; the UI is at
`http://localhost:16686`, service `autobrowser`. Search by tag, e.g.
`autobrowser.batch.id=batch-…`.

## Content and Privacy

- `none` (default) is safe for a hosted backend: only ids, names, counts, statuses and
  durations. Error descriptions are types (`tool_error`, the exception type, the stop reason),
  never messages.
- `redacted` exports text, always from the redacted and truncated event records, never from raw
  payloads. A secret that sits under a neutral key name, or inside page text, is **not** masked.
- Header values are never written to span attributes, logs or the `doctor` output.

## Batch Runs

`scripts/run_batch.py` labels every scenario session with `deployment.environment.name=batch`,
`autobrowser.batch.id`, `autobrowser.scenario.id` and (in a git checkout)
`vcs.ref.head.revision`; the label beats `telemetry.environment`. Each scenario session flushes
and shuts its exporter down when it closes, so a long batch does not pile up exporter threads.
`SessionConfig.otlp_export=False` keeps a session out of the live export even when it is
enabled — the hook for the planned Langfuse experiment bridge, which will send batch runs as
experiment traces instead (Proposed
[ADR-2026-10-31](../decisions/2026-10-31-langfuse-platform-integration.md)).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| session start fails with `uv sync --group otel` | export enabled, exporter group not installed |
| `doctor` shows `telemetry … exporter missing` | same; run `uv sync --group otel` |
| `malformed header <name>` (doctor or session start) | a header name or value with surrounding spaces, a line break or a control character, often a copy-paste slip; the value is never printed |
| session close waits up to `otlp_timeout_seconds` | the collector is slow or unreachable; a late flush finishes in the background |
| nothing in Langfuse for minutes, then everything | the `x-langfuse-ingestion-version: 4` header is missing |
| `OTLP trace export failed (…)` once in the log | the sink hit an error; later errors are dropped, the session runs on |
| exporter warnings about 401/403 | wrong keys or region (EU vs US host) in the endpoint |
| a task has spans marked `autobrowser.incomplete` | it was cancelled mid-call or the session closed before it ended |
| no `batch_id` filter in Langfuse | `attribute_aliases` not configured |

## Tests

`tests/test_otel_spans.py` checks the span tree on synthetic events without the SDK (pairing,
keys, usage, statuses, attributes on every span, aliases, `none`/`redacted`, OTLP/JSON);
`tests/test_otel_sink.py` materializes spans into the SDK's `InMemorySpanExporter` (flush and
shutdown, no span exported twice, failures never propagate, wiring, the missing-package error).
Neither needs the `otel` group or a network.
