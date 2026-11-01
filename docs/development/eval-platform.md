# Eval Platform Guide (Langfuse)

How AutoBrowser's local batch results become Langfuse datasets, experiments and scores, and how
to read them there. The decision record is
[ADR-2026-10-31: Langfuse as the Dataset, Experiment and Score Platform](../decisions/2026-10-31-langfuse-platform-integration.md);
the design is [docs/development/2026-10-31-langfuse-integration-design.md](2026-10-31-langfuse-integration-design.md);
the flow is drawn in [Eval Platform Sync](../diagrams/eval-platform-sync.md).

**The local batch folder stays the source of truth and the only source of the regression
verdict.** `compare_batches.py` decides, with its exit codes, without a network. Langfuse is a
second interface over the same files: dataset versions, experiment comparison in a UI, scores,
an LLM judge for what no grader checks, and an annotation queue. Everything there can be
rebuilt from the batch folders.

The bridge lives outside the package, in `scripts/langfuse_bridge/` and
`scripts/langfuse_sync.py`; `src/` only holds neutral records
(`src/agent_loop/platform_records.py`, `src/harness/otel_spans.py`) and names no vendor. No
Langfuse SDK is used: a small `httpx` REST client plus OTLP/JSON.

## Connecting (Cloud and self-host)

- **Langfuse Cloud** (the default choice): EU `https://cloud.langfuse.com`, US
  `https://us.cloud.langfuse.com`. The Hobby plan (50k units a month) holds about 45 Golden Set
  runs a month: a run is about 760 observations and 360 scores; the REST API allows 30 requests a minute (the
  bridge keeps to `requests_per_minute`, default 25), data is kept 30 days — the batch folders
  keep everything for longer, and `scores pull` saves the judge's and reviewers' scores.
- **Self-hosted Langfuse**: version **4.59.0 or newer** (the v4 data model: experiments over
  OTLP, observation-level judges). Only `eval_platform.host` changes.

Create a separate project for AutoBrowser (Settings → API Keys → a key pair `pk-lf-…`/`sk-lf-…`)
and put the keys in `<home>/.env` (`~/.autobrowser/.env`), never in a committed file:

```dotenv
AUTOBROWSER_EVAL_PLATFORM__ENABLED=true
AUTOBROWSER_EVAL_PLATFORM__HOST=https://cloud.langfuse.com
AUTOBROWSER_EVAL_PLATFORM__PUBLIC_KEY=pk-lf-...
AUTOBROWSER_EVAL_PLATFORM__SECRET_KEY=sk-lf-...
```

Then:

```powershell
uv run main.py doctor                       # offline: "eval platform  enabled · host … · keys set · content none"
uv run scripts/langfuse_sync.py doctor      # network: server version, keys/project, score configs
uv run scripts/langfuse_sync.py setup       # score configs (and the annotation queue, when named)
```

All settings (section `eval_platform`, see `config.example.yaml`):

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | sync batch results at all |
| `host` | unset | base URL, no trailing slash; no default in the code |
| `public_key`, `secret_key` | unset | project keys; secret, never logged or written to a ledger |
| `capture_content` | `none` | `none` / `answers` / `redacted` (below) |
| `dataset_prefix` | `autobrowser/` | dataset name prefix (a folder in the UI) |
| `environment` | `batch` | environment of experiment traces and scores |
| `auto_sync` | `true` | `run_batch.py` syncs right after the batch |
| `sync_timeout_seconds` | `300` | budget of that sync; the rest is caught up later |
| `timeout_seconds`, `max_retries`, `retry_backoff_seconds` | `30`, `3`, `2.0` | one request; retries on 429/5xx/network |
| `requests_per_minute` | `25` | client-side REST rate limit |
| `score_batch_size` | `100` | scores per request |
| `max_request_bytes` | `4 000 000` | a larger experiment trace is split into several requests |
| `verify_grace_seconds` | `60` | wait before an experiment trace of unknown fate may be re-sent |
| `triage_queue` | unset | annotation queue name; unset: no queueing |
| `triage_kinds` | agent kinds + `ungraded` | which failure kinds are queued |
| `triage_max_items` | `50` | most queued scenarios per batch |

## Keys, Content Capture and Privacy

Always sent: repository data (task texts, `expected`, `reference`, tags), ids, statuses, failure
kinds, stop reasons, metrics, token usage and scores. What a run produced is opt-in:

| `capture_content` | Item root output | Task spans | Model and tool spans |
|---|---|---|---|
| `none` (default) | `{"failure_kind", "grade", "task_statuses"}` | no task text, no answer | no arguments, results or messages |
| `answers` | the last answer | task text and its answer | as `none` |
| `redacted` | as `answers` | as `answers` | tool arguments and results |

Answers and tool results can quote the pages the agent visited. Everything sent passes the
event redaction (secret-named keys masked, strings truncated to `events.max_string_chars`), but a
secret under a neutral key or inside page text is not masked. The judge and the side-by-side
answer comparison need at least `answers`: set it in your personal `config.yaml` if that is
acceptable for every site of the Golden Set.

The live trace export of interactive sessions is a separate switch (`telemetry.*`, the
[Trace Export Guide](telemetry.md)); a batch that is synced turns it off for its own sessions,
so the run is not sent twice.

## Datasets and Versions

`uv run scripts/langfuse_sync.py dataset push` turns `tests/golden/tasks.jsonl` into the dataset
`autobrowser/golden`:

- **Item** per scenario: id `ab-` + a hash of the dataset name and `scenario_id` (stable when the
  scenario changes, so Langfuse versions it), input `{"tasks", "scenario_id"}`, expected output
  `{"checks": <expected as is>, "reference": <rubric or null>}`, metadata with a
  `content_sha256`.
- **Only changed items are sent**; each upsert is a new Langfuse version, so a push without
  changes writes nothing. A removed scenario is **archived**, never deleted (old experiments keep
  their links), and comes back under the same id. Renaming a `scenario_id` is a removal plus an
  addition: keep ids stable.
- **The repository is the source of truth.** Do not edit the dataset in the UI: the next push of
  a change overwrites it, and `dataset status` lists such edits.
- **Versions.** The canonical version is the file's `tasks_sha256` with the git commit as its
  label; Langfuse's version is the timestamp of the newest item. Each push appends a row to
  `<home>/batches/platform_datasets.jsonl` mapping one to the other.
- `dataset push` refuses a tasks file that differs from `HEAD` (`--allow-dirty` overrides and
  journals it as dirty); `--dry-run` only reads the file.

## Experiments

Every synced batch is one experiment on the dataset, built **after** the run from the batch
folder (`batch.json`, `run_index.jsonl`, `traces/`), with the events' own timestamps:

- one **item trace per scenario**: a root `scenario {scenario_id}` (observation type agent:
  input, output, the expected output of the batch's own copy of the set) over the
  `invoke_agent autobrowser` task trees with their `chat` generations and `execute_tool`
  observations — the same tree as the live export;
- name `golden · 2026-10-31 14:05 · a1b2c3d* · gemma4:31b-cloud · batch-1a2b3c` (set, start in
  UTC, commit with `*` when dirty, model, batch); the description and metadata carry the
  provenance (commit, branch, model, provider, `tasks_sha256`, `settings_sha256`, non-default
  sections, CLI options, baseline batch);
- the item trace is ERROR when the scenario failed (`failure_kind` other than `passed`/
  `ungraded`); a scenario that left no trace is a lone root with the error type.

A batch on the repository's current set pushes the dataset first and records the item version
(`dataset_version_matched=true`). A batch on an older set is still synced — same item ids, the
expectations of its own copy — with `dataset_version_matched=false`.

In the UI: **Datasets → autobrowser/golden → Experiments** lists the runs with pass rate,
verdict, latency and cost; **Compare** shows them side by side per scenario; an item opens its
trace.

## Scores

Built from the batch by `platform_records.batch_score_records` (names and categories come from
the code's own vocabularies; `setup` creates their configs):

| Score | Type | On |
|---|---|---|
| `passed`, `infra_failure` | boolean | item |
| `failure_kind`, `grade`, `check.<key>` (graded keys, `run`, `expected`; message as comment) | categorical | item |
| `regression` (the scenario's finding against the previous comparable batch) | categorical | item |
| `model_calls`, `tool_calls`, `tool_errors`, `policy_blocks`, `time_to_first_tool_ms` | numeric | item |
| `task_failure_kind`, `stop_reason` | categorical | task observation |
| `pass_rate`, `agent_failures`, `infra_failures`, `regressions` | numeric | experiment |
| `verdict` (`no_regression`/`regression`/`incomparable`/`no_baseline`) | categorical | experiment |

The verdict is the local one — `compare_batches()` against the previous comparable batch —
sent, never recomputed. Score ids are fixed (experiment + target + name) and the timestamp is the
batch's end, so sending again updates instead of duplicating. A partly rejected score batch
(HTTP 207) is recorded in the ledger and **not** retried automatically: fix the cause (often
`setup` after a vocabulary change), then `experiment push --batch X --retry-rejected`.

## LLM Judge

Keys no grader checks (`fields`, …) grade `ungraded` locally. A Langfuse managed evaluator can
judge them; it is informational only — never part of `failure_kind`, `compare_batches` or a gate.

1. Needs `capture_content` `answers` or `redacted` (otherwise it has no answer to read).
2. Settings → LLM Connections: an OpenAI-compatible connection to a model **with structured
   outputs** (a separate, cheap model; check whether `gemma4` qualifies before relying on it).
3. Evaluators → new LLM-as-a-judge on **observations**, filtered to the dataset
   `autobrowser/golden` (experiments), target the item's root observation; variables `{{input}}`
   (tasks), `{{output}}` (the answer), `{{expected_output}}` (checks and `reference`).
4. Score `judge.fields` (boolean) or `judge.quality` (0–1). A starting prompt:

   > You check an answer of a browser agent. Task(s): {{input}}. Expected checks and rubric:
   > {{expected_output}}. Answer: {{output}}. Judge only the parts of the expectation no
   > automatic check covers (for example `fields`: every listed item has each named field) and
   > the rubric in `reference`. Ignore formatting. Reply true only if every requirement holds.

`reference` is an optional scenario field for this rubric (graders ignore it); the Golden Set
has short ones for the scenarios with `fields`.

## Annotation Queue

Set `eval_platform.triage_queue` (Hobby allows one queue) and run `setup`: it creates the queue
with `human.verdict` (`agent_error`, `site_changed`, `anti_bot`, `grader_wrong`,
`expected_outdated`, `ok`) and `human.comment`. Every sync then queues the item traces of
scenarios whose kind is in `triage_kinds` (default: the agent kinds and `ungraded`, not the infra
ones), at most `triage_max_items` per batch, each once.

`uv run scripts/langfuse_sync.py scores pull --batch X` writes the judge's and reviewers' scores
of a batch to `<batch_dir>/platform_scores.jsonl` (rewritten whole): a record that outlives the
30-day window, readable with DuckDB. Nothing in the verdict reads it; a `grader_wrong` is a hint
to fix `expected`.

## Commands

```powershell
uv run scripts/run_batch.py --tasks tests\golden\tasks.jsonl --continue-on-error --grade
    # enabled + auto_sync: synced right after the batch (summary on stderr); --no-platform-sync skips it
uv run scripts/compare_batches.py              # as before, plus "langfuse: <batch> <url>" for synced batches

uv run scripts/langfuse_sync.py doctor                         # server >= 4.59.0, keys, project, configs
uv run scripts/langfuse_sync.py setup                          # score configs (+ the annotation queue)
uv run scripts/langfuse_sync.py dataset push   [--suite golden] [--tasks PATH] [--allow-dirty] [--dry-run]
uv run scripts/langfuse_sync.py dataset status [--suite golden]
uv run scripts/langfuse_sync.py experiment push --batch ID|DIR [--new-attempt] [--retry-rejected] [--dry-run]
uv run scripts/langfuse_sync.py experiment push --pending      # every batch whose sync is unfinished
uv run scripts/langfuse_sync.py scores pull --batch ID|DIR
uv run scripts/langfuse_sync.py status [--batch ID] [--verify]
```

Exit codes of `langfuse_sync.py`: `0` synced or nothing to do; `1` partly (an outage, traces
pending or unknown, rejected scores) — rerun later; `2` a usage or configuration problem
(platform disabled, keys missing or rejected, server older than 4.59.0, a batch from before the
analytics contract or not finished, another project without `--new-attempt`, a dirty tasks
file). `run_batch.py`'s own exit code never depends on the sync.

## Recovery and Troubleshooting

Each synced batch has a ledger, `<batch_dir>/platform_sync.json` (ids and states only, no keys),
and a lock `platform_sync.lock` while a sync runs (taken over after `2 × sync_timeout_seconds`).

- **Every trace is sent exactly once.** Langfuse v4 does not deduplicate re-sent spans. A trace is
  marked `exported` after its request succeeded and is never sent again; when a request may have
  arrived (a timeout after sending) it is `unknown`, and after `verify_grace_seconds` the
  observations API decides whether it arrived before anything is re-sent. `status --verify`
  reports traces with more observations than spans.
- **Outage or budget.** The sync stops between scenarios; the rest stays `pending` and
  `experiment push --pending` (or the next nightly) catches up.
- **New project, or you deleted the experiment.** `experiment push --batch X --new-attempt`
  syncs it again as experiment `<batch_id>.2` with new trace and score ids.
- **Settings fingerprint.** The observer sections (`pipeline`, `telemetry`, `eval_platform`) are
  not part of `settings_sha256`, so switching the sync on keeps runs comparable. Batches from
  before this change get one "settings differ" warning when compared across it.
- **Golden Set changes.** Editing `tests/golden/tasks.jsonl` (the `reference` rubrics included)
  changes its hash: earlier batches are not comparable with later ones (`compare_batches` exit
  `2`) until a new baseline run.

| Symptom | Likely cause |
|---|---|
| `error: eval_platform.enabled is false` (exit 2) | the section is off in the profile the command reads |
| `HTTP 401 (check eval_platform.public_key/secret_key and the host's region)` | wrong keys, or EU keys against the US host |
| `the server reports version …` (exit 2) | self-hosted Langfuse older than 4.59.0 |
| `run_batch.py` prints `langfuse sync … is partial` | an outage or the budget: run `experiment push --pending` |
| scores `rejected` in `status` | a category the config does not have: `setup`, then `--retry-rejected` |
| no answers in the UI | `capture_content` is `none` |
| `was synced to project …; pass --new-attempt` | the keys now name another project |
