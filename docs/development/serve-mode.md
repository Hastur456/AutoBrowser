# Serve Mode Guide

`autobrowser serve` is the stand: a long-running process that runs configured browser jobs on
triggers — a schedule, an HTTP call or a webhook — without a terminal, on a server or in
Docker. It is built for repeated jobs that should cost almost nothing when nothing changed.
The decision record is [ADR-2026-11-02: Serve Mode, Cost Ladder and Replay](../decisions/2026-11-02-serve-mode-and-cost-ladder.md);
the design is the [serve mode design](2026-11-02-serve-mode-design.md) (§6–§8) and the
rollout is the [phase 3 plan](2026-11-02-serve-mode-phase3-plan.md). Replay itself is in the
[replay guide](replay.md).

The stand is a client of the session like the terminal UI: one worker = one session = one
browser profile, one run at a time per worker. A pool of workers runs jobs in parallel, a model
cascade lets a cheap model work and a strong one step in, and a Telegram channel takes
commands and approvals (phase 4, [design](2026-11-08-serve-mode-phase4-design.md),
[plan](2026-11-08-serve-mode-phase4-plan.md)).

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
| `telegram` | `id` only (needs the `telegram` channel) | `/run <job>` from an allowed chat |

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
  `AUTOBROWSER_SERVE__WEBHOOK_OUT__SECRET` is set; no retries;
- `telegram` — a plain-text message to every allowed chat (see [Telegram Channel](#telegram-channel)).

Bodies are page data: control sequences are stripped and they are cut to
`serve.notify_max_chars`. Treat them as untrusted text. A failing channel is a
`serve.notify.failed` event (error type only) and never fails the run.

## Approvals

Without an approval channel (the default) nobody can answer: a configured `default`
permission mode becomes `dont_ask`, so an approval is a non-terminal deny and the model reads
the reason. An approval channel is `api_approvals: true` or the Telegram channel with
`serve.telegram.approvals`. Then each ticket goes to every channel as an `approval`
notification with its `ticket_id`; `POST /v1/approvals/{ticket}` (with `api_approvals`) or a
Telegram button answers it. With a pool every worker has its own broker; one desk finds the
worker that holds the ticket. An unanswered
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
- The worker browser profiles are the stand's own: `<home>/<serve.profiles_dir>/w<i>`.

## Worker Pool

`serve.workers` (default 1, at most 8) runs jobs in parallel. Each worker is a whole session of
its own: browser, MCP processes, profile `<storage.root_dir>/<serve.profiles_dir>/w<i>`, CDP
port `serve.cdp_port_base + i` (with `browser.launch: cdp`), its own approval broker and the
label `worker: w<i>` on its session.

```yaml
memory:
  persistent_enabled: false      # required with more than one worker
serve:
  workers: 2
  jobs:
    shop-orders:
      task: "Open https://shop.example/orders and list the open orders."
      worker: 1                  # pin: w1's profile holds the shop login
      triggers: [{id: daily, type: cron, schedule: "0 8 * * *"}]
    price-watch: {task: "...", triggers: [{id: hourly, type: cron, schedule: "0 * * * *"}]}
```

- **Cancel.** `/cancel` (Telegram) reaches a queued run, a run waiting out its cooldown
  (skipped as `cancelled`) and a running one; a cancelled run starts no further task and is
  reported `cancelled`, never as a failure.
- **Order.** Queued runs start in queue order on the free worker with the lowest index. A run
  that cannot start yet stays queued, and a later one may start before it.
- **Exclusion.** Two runs of one job never run at once, nor two runs that replay or record one
  recording (`recording:`).
- **Pins.** `JobSpec.worker` pins a job to a worker. A worker with pinned jobs runs only those:
  a profile with logins never serves another job or a free task. The stand refuses a
  configuration where unpinned jobs or free tasks have no unreserved worker left.
- **Day budget.** A running run holds its token and money budget (already the smaller of its
  own budget and the rest of the day) out of the day until it ends; the day run cap counts
  running runs. Parallel runs together never get more than the day has; a run without room
  waits.
- **Sessions.** `serve.session_max_runs` counts per worker and recreates only that worker.
- **Memory.** `workers > 1` requires `memory.persistent_enabled: false`: the workers would share
  the memory files (locking comes later).
- **External browsers.** With `browser.launch: external` give each worker its own profile in
  the browser server: `args: [..., --user-data-dir, "{profile_dir}"]`. The session fills
  `{profile_dir}` with the worker's profile (in interactive mode, `browser.user_data_dir`). The
  stand warns when a pool runs on external browsers and no server names the placeholder.
- **Reports.** With a pool every `RunReport` names its `worker`, `/v1/status` lists `workers`
  (index, pinned jobs, running run) and `serve.started` carries `workers`. With one worker
  nothing changes.

Every worker needs memory: plan for one Chromium plus one MCP process per worker.

## Model Cascade

The cascade lets a cheap model do the work and a stronger one step in when the agent is stuck.
It works in both modes, the stand and the interactive session.

```yaml
llm:
  model: gemma4:31b-cloud        # tier 0: the session model (--model, /model)
  prices:
    gemma4:31b-cloud: {input_per_mtok: 0.2, output_per_mtok: 0.6}
    gpt-oss:120b-cloud: {input_per_mtok: 0.6, output_per_mtok: 2.4}
cascade:
  enabled: true
  tiers:                         # above tier 0, cheapest first, at most 3
    - {model: gpt-oss:120b-cloud, label: strong}
  escalate_after_failures: 2
  escalate_after_replans: 1
```

- **Tiers.** Tier 0 is always the session model; `cascade.tiers` are the tiers above it. A tier
  names its `model` and may override `provider`, `host`, `api_key`, `temperature` (`null`: no
  temperature), `reasoning_effort` and `timeout_seconds`; the rest comes from `llm`. Its
  `label` (default: the model id) names it in events and in `usage.by_model`, and must differ
  from the session model.
- **Escalation.** Before every turn the engine checks whether to go up one tier: an empty model
  answer (`escalate_on_no_action`: it no longer ends the task while a higher tier exists), a
  progress-guard block (`escalate_on_progress_block`), `escalate_after_failures` failed calls in
  a row, or `escalate_after_replans` replans since the last change. The tier never goes down
  within a task, and the new tier gets a fresh window of `loop.max_consecutive_failures`.
- **Calls.** The planner (call #0) runs on tier 0, a replan on the current tier, a replay
  retarget on `cascade.retarget_tier`. Escalation itself costs no call.
- **Events.** Each climb is a `ladder.escalated` with `from_tier`, `tier`, `model` and the
  `reason`; with several tiers `model.*` events carry `tier` and `model`, and the OTLP chat span
  is named by that model. `RunReport.tier_reached` says how high a run went.
- **Money.** A money cap (`loop.max_task_cost`, `serve.daily_max_cost`, a run budget's
  `max_cost`) needs a price for every tier: the stand refuses to start without one, the
  interactive session warns. Note for stands of phase 3: `loop.max_task_cost` now counts as a
  money cap too, so a stand profile with it and without `llm.prices` for the session model is
  refused until the price is added.
- `doctor` prints one line per tier (provider, host, model, key set, priced); `doctor --llm`
  probes every tier on another provider or host. Keep `escalate_after_failures` at most
  `loop.max_consecutive_failures` (the tier check runs before the failure stop), or failures
  end the task before the tier goes up (`doctor` and `serve --check` warn).

## Telegram Channel

The Telegram channel sends the notifications to your chats, takes a few commands and answers
approvals with buttons. It uses long polling: the stand opens no port for it.

### Set up

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Find your chat id (for example by sending the bot a message and reading
   `https://api.telegram.org/bot<token>/getUpdates` once, before the stand runs).
3. Configure it; the token goes into the environment, never into the profile:

   ```yaml
   serve:
     channels: [log, telegram]
     telegram:
       allowed_chat_ids: [123456789]
     jobs:
       price-watch:
         task: "..."
         triggers:
           - {id: hourly, type: cron, schedule: "0 * * * *"}
           - {id: chat, type: telegram}   # allows /run price-watch from the chat
   ```

   ```powershell
   $env:AUTOBROWSER_SERVE__TELEGRAM__BOT_TOKEN = "<the bot token>"
   uv run main.py serve --check    # offline: no call to Telegram
   uv run main.py doctor           # checks that the bot has no webhook
   ```

A bot with a webhook cannot be polled: the stand refuses to start (and `doctor` fails) until it
is removed with `deleteWebhook`. A token Telegram refuses stops the stand with exit code 1.

### Commands

| Command | What it does |
| --- | --- |
| `/help`, `/start` | the commands |
| `/jobs` | the jobs, whether the chat may run them, their last run (never their task text) |
| `/run <job>` | runs a job that has a trigger `type: telegram` |
| `/status` | running runs (per worker with a pool), the queue, today |
| `/budget` | today against the day caps |
| `/cancel [run\|job]` | drops a queued run or stops a running one; without a target, the one running run |
| `/task <text>` | a free task, only with `serve.free_tasks` (then within `serve.free_task_domains`) |

Plain text is never a task: it gets a hint to send `/help`.

### Approvals

With `serve.telegram.approvals` (the default) every approval ticket arrives as a card with
buttons: allow once, allow for the session (only when the ticket allows it), deny. A press
answers the ticket (`via: channel`) and the buttons disappear. An unanswered ticket is denied
after `loop.progress_timeout_seconds × permissions.approval_timeout_ratio`; raise
`loop.progress_timeout_seconds` in the stand profile to give yourself time. Approvals over the
API (`POST /v1/approvals`) still need `serve.api_approvals`.

### Security

- Only the chats in `allowed_chat_ids` are answered and notified. Any other chat gets no answer
  and is a `serve.auth.rejected` event with an opaque sender (an HMAC of the chat id under the
  token), never the raw id. In a group every member may give commands: prefer a private chat.
- The token is in every Bot API path, so it is kept out of errors, logs (the `httpx` loggers are
  filtered), events, `--check` and `doctor`. `api_base` (a self-hosted Bot API server) must be
  https, or http on localhost.
- Messages are plain text without link previews: page text cannot inject markup and Telegram
  fetches no link from a page.
- Notifications and approval cards (the arguments preview included) go to Telegram's servers.
  Choose `notify_on` with that in mind.
- The offset of the last update is saved before the updates are handled, and messages older
  than `max_update_age_seconds` are ignored: a command sent while the stand was down never runs
  later. Commands and rejection events are rate-limited per sender
  (`serve.rate_limit_per_minute`).

| Setting (`serve.telegram.*`) | Default | Meaning |
| --- | --- | --- |
| `bot_token` | — | the token (env `AUTOBROWSER_SERVE__TELEGRAM__BOT_TOKEN`) |
| `allowed_chat_ids` | `[]` | the chats; required with the channel |
| `api_base` | `""` | a self-hosted Bot API server; empty: the public one |
| `approvals` | `true` | approval buttons |
| `poll_timeout_seconds` | `30` | long-polling timeout (1..60) |
| `request_timeout_seconds` | `10.0` | other calls, and the slack above a poll |
| `poll_backoff_seconds`, `poll_backoff_max_seconds` | `1.0`, `60.0` | the pause after a failed poll, doubling up to the max |
| `send_retry_max_seconds` | `30.0` | the longest rate-limit wait a send sits out once (0: never) |
| `max_update_age_seconds` | `300.0` | older messages are ignored |

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
browser, Playwright MCP launches Chromium with `--headless --user-data-dir "{profile_dir}"`
(`/data/serve/profiles/w<i>`, one per worker).
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
| `events.jsonl` | the stand's own events, redacted like session events: `serve.started`, `serve.trigger.fired`, `serve.run.queued`, `serve.run.skipped{reason}` (also `cancelled`), `serve.run.finished`, `serve.notified`, `serve.notify.failed`, `serve.auth.rejected`, `serve.command`, `serve.channel.error`, `serve.stopped` |
| `jobs/<id>/state.json` | last run, last firing per trigger, the probe's fingerprint and region, skip counters |
| `state.json` | the day the budget notification went out |
| `channels/<name>.json` | where a chat channel stopped reading (its offset) |

Each worker's tasks are a normal session under `<home>/sessions/` (labels `mode: serve`,
`worker: w<i>`). After `serve.session_max_runs` runs the session is recreated (browser and MCP
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
| Worker pool | `src/serve/pool.py` |
| Chat command port | `src/serve/commands.py` |
| Telegram adapter | `src/channels/telegram.py` |
| Cascade tiers (provider registry) | `src/cli/models.py` |
| Tier escalation | `src/agent_loop/execution/guards.py` (`tier_escalation`), `loop.py` |
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

`src/serve` never imports the engine, the browser layer, the harness, the CLI, the providers
or the channels; `src/channels` imports only the neutral ports of `src/serve`, and only the
CLI imports it (`tests/test_serve_boundaries.py`).
