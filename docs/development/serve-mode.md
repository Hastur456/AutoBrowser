# Serve Mode Guide

`autobrowser serve` is the stand: a long-running process that runs configured browser jobs on
triggers — a schedule, an HTTP call or a webhook — without a terminal, on a server or in
Docker. It is built for repeated jobs that should cost almost nothing when nothing changed.
The decision record is [ADR-2026-11-02: Serve Mode, Cost Ladder and Replay](../decisions/2026-11-02-serve-mode-and-cost-ladder.md);
the design is the [serve mode design](2026-11-02-serve-mode-design.md) (§6–§8) and the
rollout is the [phase 3 plan](2026-11-02-serve-mode-phase3-plan.md). Replay itself is in the
[replay guide](replay.md).

The stand is a client of the session like the terminal UI: one worker = one session = one
browser profile, one run at a time. A pool of workers, model cascades and Telegram are phase 4.

## Quick Start

```yaml
# serve.yaml (point AUTOBROWSER_CONFIG_FILE at it, or use <home>/config.yaml)
replay:
  enabled: true
serve:
  jobs:
    price-watch:
      task: "Open https://shop.example/item/1 and report the current price."
      triggers:
        - {id: hourly, type: cron, schedule: "0 * * * *"}
      probe:
        steps:
          - {tool: browser_navigate, args: {url: "https://shop.example/item/1"}}
          - {tool: browser_snapshot}
        select: "(?s)Price.*?USD"
      recording: price-watch
      record_on_success: true
      domains: [shop.example]
```

```powershell
$env:AUTOBROWSER_SERVE__TOKEN = "<a long random string>"
uv run main.py serve --check      # validate the configuration, start no browser
uv run main.py serve              # run until Ctrl+C (SIGTERM in Docker)
```

`--check` prints one summary line (never the token) or the reasons the stand refuses to start,
with exit code 2.

## A Run, Stage by Stage

Each firing of a job is one **run** with a `run_id`. It climbs the cost ladder only as far as
it must:

1. **Gate.** The dispatcher decides at once: `queued`, `coalesced` (the job is busy), or
   `skipped` with a reason — `disabled`, `cooldown`, `budget` (a day cap is reached),
   `queue_full`, `shutdown`, or the scheduler's `min_interval`. Every decision is an event, and
   skips are counted in the job state.
2. **Probe (stage 0)**, when the job has one: its `steps` are replayed without the model, the
   region `select` of the last output (minus `ignore`) is fingerprinted. The same fingerprint
   as last time ends the run as `unchanged`: zero model calls, no main task, no notification.
   A failed probe fails the run.
3. **Main task.** With a stored `recording` it replays first (stage 1, then retarget and the
   agent on divergence — see the replay guide); without one the agent solves the task, and
   `record_on_success` saves the recording. A job capped below `agent` (`max_stage: playback`)
   with no recording yet is `blocked` (`ladder_cap`) without running a task.
4. **Report.** One `RunReport` line in the ledger, notifications, the job state.

Every task of a run is isolated: `carry_context=False` (no history from earlier runs), the
job's `domains` as the task scope, the job's ladder cap and budget. The fingerprint moves only
when the main task completed, so a failed run is retried at the next tick.

### Probes and Page Formats

The region of a probe is read by the session's playback adapter. With the browser server
(Playwright MCP) it drops the volatile `[ref=…]` markers, so an unchanged page keeps its
fingerprint. Other servers use the plain-text adapter, which knows no such markers: drop
anything volatile yourself with `ignore` (e.g. `'\[ref=[^\]]+\]'`, timestamps, counters).
Probe tool names are your data: the stand checks at start that every probe tool exists.

## Triggers

| Type | Fields | Fired by |
| --- | --- | --- |
| `interval` | `every_seconds` (≥ `serve.min_interval_seconds`) | the scheduler; a trigger that never fired runs when the stand starts |
| `cron` | `schedule` (five fields, `serve.timezone`) | the scheduler |
| `webhook` | `secret` or `secret_env`, `auth: bearer \| hmac_sha256` | `POST /v1/hooks/{job}/{trigger}` |

When each trigger last fired is kept in the job state: a restart does not repeat a slot, and
missed slots are not caught up unless `serve.catch_up: one` (one catch-up firing). Each
wall-clock slot fires once — a daily cron does not fire twice when the clocks go back. A
trigger never fires sooner than `min_interval_seconds` after its previous firing.

`serve.timezone` is an IANA zone (`Europe/Berlin`) or empty for the system zone (a fixed
offset). The time zone database comes with the dependencies, also on Windows.

## Budgets

| Level | Setting | When it runs out |
| --- | --- | --- |
| Run | the job's `budget`, else `serve.default_run_budget` (`max_model_calls` is required) | the task ends `blocked`, `budget_exhausted` |
| Day | `serve.daily_max_runs`, `daily_max_tokens`, `daily_max_cost` | triggers are skipped (`budget`) and one `budget` notification is sent that day |

A run's token and money caps are the smaller of its budget and what is left of the day when
the run starts (a free task too). The day caps and the job's cooldown are checked again when a
queued run starts: a run queued behind another waits out the cooldown. The
day is counted in `serve.timezone` from the ledger, so a restart keeps the totals. An
`unchanged` probe run costs nothing and is not counted as a run. A money cap needs
`llm.prices` for the model (the stand refuses to start otherwise); a call without reported
tokens makes the cost unknown, and the call cap still applies.

## HTTP API

| Method and path | Access | Answer |
| --- | --- | --- |
| `GET /healthz` | open | `{"status": "ok"}` |
| `GET /v1/status` | `serve.token` | running run, queue, day totals against the caps, jobs, open approvals |
| `POST /v1/jobs/{job}/runs` | token | `202 {run_id, state[, reason]}`; `404` for an unknown job |
| `GET /v1/runs/{run_id}` | token | the `RunReport`, or `{state: queued \| running}` |
| `POST /v1/hooks/{job}/{trigger}` | the trigger's secret | `202 {run_id, state}` |
| `POST /v1/approvals/{ticket}` | token + `serve.api_approvals` | `{"answer": "once" \| "session" \| "deny"}` |
| `POST /v1/tasks` | token + `serve.free_tasks` | `{"task": "..."}` → a free task, `202` |

```powershell
$h = @{ Authorization = "Bearer $env:AUTOBROWSER_SERVE__TOKEN" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8765/v1/jobs/price-watch/runs
```

- **Idempotency.** An `Idempotency-Key` header on `runs` and `hooks` returns the first answer
  for `serve.idempotency_window_seconds`.
- **Signed webhooks.** With `auth: hmac_sha256` the caller sends
  `X-AutoBrowser-Signature: sha256=<hex HMAC-SHA256 of the raw body>`; with `bearer`,
  `Authorization: Bearer <secret>`. A wrong secret, an unknown job and an unknown trigger all
  answer the same bodyless `401`.
- **The body of a webhook never reaches the job**: it is only measured and signature-checked.
  The task is the job's `task`, always.
- **Limits.** `serve.max_body_bytes` (`413`, also for a chunked body), and
  `serve.rate_limit_per_minute` per token, per webhook trigger and per failing client address
  (`429`): an address that keeps sending bad secrets gets `429` instead of `401` for a minute,
  but a request with a valid credential is never refused for its address (behind a reverse
  proxy every caller shares one).

## Notifications

`notify_on` of a job picks the outcomes that go out: `change` (the new value first, then the old),
`success`, `failure`, `blocked`, `budget`. Channels (`serve.channels`):

- `log` — a `serve.notified` event and one stdout line (the container log);
- `webhook` — a POST of `{kind, job_id, run_id, title, body, cost, ticket_id}` as JSON to
  `serve.webhook_out.url`, signed with `X-AutoBrowser-Signature` when
  `AUTOBROWSER_SERVE__WEBHOOK_OUT__SECRET` is set; no retries.

Bodies are page data: control sequences are stripped and they are cut to
`serve.notify_max_chars`. Treat them as untrusted text. A failing channel is a
`serve.notify.failed` event (error type only) and never fails the run.

## Approvals

Without `serve.api_approvals` (the default) nobody can answer: a configured `default`
permission mode becomes `dont_ask`, so an approval is a non-terminal deny and the model reads
the reason. With `api_approvals: true` each ticket goes to every channel as an `approval`
notification with its `ticket_id`, and `POST /v1/approvals/{ticket}` answers it. An unanswered
ticket is denied after `loop.progress_timeout_seconds × permissions.approval_timeout_ratio`;
raise `loop.progress_timeout_seconds` in the stand profile to give a human more time.

## Security

- The API listens on `127.0.0.1`. Any other `serve.host` needs `serve.allow_remote: true` and
  a non-empty token, or the stand does not start. Without a token `/v1/*` is closed.
- Tokens and secrets are compared in constant time and never written to events, reports or
  `--check`; an invalid configuration is reported by field and reason, never by value. The YAML file does not expand `${VAR}`: put the token in
  `AUTOBROWSER_SERVE__TOKEN` and webhook secrets in variables named by `secret_env`.
- `permissions.mode: bypass` is refused. Jobs come from the configuration only; the API runs
  existing jobs; free tasks are off (`serve.free_tasks`), and when on they run within
  `serve.free_task_domains`.
- A job's `domains` deny calls on any other domain, and state-changing calls on an unknown
  domain (that includes tools without a URL, such as `memory_write`); see the
  [permissions guide](permissions.md). No rule, grant or mode lifts it.
- uvicorn's access log is off; client addresses reach the events only with
  `serve.log_client_addresses`.
- The worker browser profile is the stand's own: `<home>/<serve.profiles_dir>/w0`.

## Docker

```bash
cp docker/serve.example.yaml data/serve.yaml          # edit the jobs
echo "AUTOBROWSER_SERVE_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
docker compose up -d --build
curl -s http://127.0.0.1:8765/healthz
```

The image (`Dockerfile`): `python:3.12-slim`, Node.js and `@playwright/mcp` (pinned) with
headless Chromium, AutoBrowser installed with `uv tool install` from the wheel, a non-root
user, `/data` as the AutoBrowser home and `/data/serve.yaml` as the profile, a healthcheck on
`/healthz`. The example profile sets `browser.launch: external`: the session starts no
browser, Playwright MCP launches Chromium with `--headless --user-data-dir /data/serve/profiles/w0`.
Inside the container `serve.host` is `0.0.0.0`; `compose.yaml` publishes `127.0.0.1:8765`
only — put a TLS reverse proxy in front to reach it from elsewhere.

`browser.headless` and `browser.extra_args` are for the other mode, `launch: cdp`, where the
session starts Chrome itself.

Two things to know when you change the image or the profile:

- stdio MCP servers start with a reduced environment (the MCP SDK passes only a safe subset
  such as `PATH` and `HOME`). A variable a server needs goes into its `mcp_servers.<name>.env`;
  the image links the user's Playwright cache to `/ms-playwright` for that reason;
- the healthcheck probes port 8765; with another `serve.port` override `HEALTHCHECK`.

The smoke test builds the image and runs a probe-only job in it:
`uv run pytest -m live tests\pipeline\test_docker_smoke.py` (set
`AUTOBROWSER_SMOKE_IMAGE` to reuse a built image).

## Files and Events

Under `<storage.root_dir>/<serve.dir>/` (default `<home>/serve/`):

| File | What |
| --- | --- |
| `ledger.jsonl` | one `RunReport` per run: trigger, status, stop reason, stage, tasks, `changed`, playback, usage, budget, notified channels |
| `events.jsonl` | the stand's own events, redacted like session events: `serve.started`, `serve.trigger.fired`, `serve.run.queued`, `serve.run.skipped{reason}`, `serve.run.finished`, `serve.notified`, `serve.notify.failed`, `serve.auth.rejected`, `serve.stopped` |
| `jobs/<id>/state.json` | last run, last firing per trigger, the probe's fingerprint and region, skip counters |
| `state.json` | the day the budget notification went out |

The worker's tasks are a normal session under `<home>/sessions/` (labels `mode: serve`,
`worker: w0`). After `serve.session_max_runs` runs the session is recreated (browser and MCP
restart); a failed MCP server is reconnected before the next run, else the session is
recreated.

On SIGTERM or Ctrl+C the stand takes no new triggers, drops the queue (`skipped{shutdown}`),
gives the running run `serve.shutdown_grace_seconds`, then cancels it; a probe that ends during
the shutdown starts no main task. If the dispatcher or the scheduler dies, the stand stops with
exit code 1 rather than answer `/healthz` while nothing runs (`restart: unless-stopped` in
`compose.yaml` brings it back); a single failed firing is only logged.

## Code Map

| Piece | Where |
| --- | --- |
| Settings (`ServeSettings`, `JobSpec`, `TriggerSpec`, `ProbeSpec`) | `src/config.py` |
| Dispatcher (gate, runs, reports) | `src/serve/dispatcher.py` |
| Scheduler | `src/serve/scheduler.py` |
| HTTP API, auth helpers | `src/serve/app.py`, `src/serve/auth.py` |
| Run profiles, probe profile | `src/serve/profiles.py` |
| Ledger, job state, reports | `src/serve/ledger.py`, `src/serve/state.py`, `src/serve/reports.py` |
| Channels, approvals bridge | `src/serve/notify.py`, `src/serve/approvals.py` |
| Start checks, runtime | `src/serve/checks.py`, `src/serve/runtime.py` |
| Composition | `src/cli/serve.py` |
| Task scope | `src/harness/permissions.py` |
| Probe steps (inline recording) | `src/harness/playback.py` |
| Image | `Dockerfile`, `compose.yaml`, `docker/serve.example.yaml` |

`src/serve` never imports the engine, the browser layer, the harness, the CLI or the providers
(`tests/test_serve_boundaries.py`).
