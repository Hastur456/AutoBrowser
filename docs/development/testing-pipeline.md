# Testing Pipeline Guide

How to check AutoBrowser for regressions: the offline gate that runs on every pull request, and
the live Golden Set run of the agent on real sites. The design is the
[Autonomous Test Pipeline Design](2026-10-19-autonomous-test-pipeline-design.md) (§4.3 regression,
§4.3.4 graders); the decision record is
[ADR-2026-10-19: Autonomous Test Pipeline](../decisions/2026-10-19-autonomous-test-pipeline.md).
Terms such as [Golden Set](../glossary.md), [Grader](../glossary.md) and
[Eval baseline](../glossary.md) are in the glossary.

## Levels at a Glance

| Level | What it checks | Browser and model | Command | Where it runs |
|---|---|---|---|---|
| Unit and contract tests | contracts, layer boundaries, state, policy, hooks, memory, UI | none | `uv run pytest` | CI gate on every PR and push to `main` |
| Scripted evals | the real engine on 57 scripted scenarios, compared with the v2 baseline | fake browser, scripted model | `uv run scripts/run_evals.py` | CI gate |
| Golden Set | the agent on real tasks on real sites, graded by its final answer | real Chrome, Playwright MCP, real model | `uv run scripts/run_batch.py … --grade` | by hand (nightly is planned) |

The offline levels are deterministic and need no network. The Golden Set depends on live sites
(anti-bot pages, layout changes, geo), so a failure there needs a look at the trace before it is
called a regression.

## Offline Gate

`.github/workflows/gate.yml` runs these on `windows-latest`; run the same locally before a push:

```powershell
uv sync --locked
uv run pytest
uv run scripts/run_evals.py --baseline tests/evals/baselines/agent_loop_v2.json --memory-seed tests/evals/memory_seed
```

- `uv run pytest` deselects tests marked `live` and `bench`; select them with `-m live` or
  `-m bench`.
- `run_evals.py` runs every scenario of `tests/evals/scenarios/` and prints one line per finding
  (a changed counter, a request size past the tolerance, a missing or orphan baseline row, a
  failed scenario assertion). Exit code `0` means no failing finding, `1` a regression, `2` a
  missing or malformed baseline.
- The run is hermetic: your `config.yaml`, `.env` and `AUTOBROWSER_*` variables are hidden, so
  the metrics are those of the code defaults on every machine. Only
  `AUTOBROWSER_PIPELINE__REQUEST_CHARS_TOLERANCE` (default `0.1`) is read from the environment.

### Updating the eval baseline

A change that deliberately alters what a scenario does (more or fewer model calls, other tool
arguments, a larger prompt) shows up as findings. When the new behavior is intended, rewrite the
baseline explicitly and commit it with the change, so the review sees the baseline move:

```powershell
uv run scripts/run_evals.py --memory-seed tests/evals/memory_seed --write-baseline
```

It prints the findings against the current file first and refuses while a scenario assertion
fails. CI never writes a baseline.

### Adding eval scenarios

- Hand-written scenarios are YAML files in `tests/evals/scenarios/`; every one sets
  `strict: true` (a model call past the script, or an unused scripted response, fails it).
- The generated scenarios are defined in `scripts/generate_eval_scenarios.py`. Change them there,
  then run `uv run scripts/generate_eval_scenarios.py`;
  `tests/test_generate_eval_scenarios.py` fails when a YAML file drifts from the generator.
- After adding or changing scenarios, rewrite the baseline as above.

## Golden Set: the Agent on Real Tasks

The Golden Set is `tests/golden/tasks.jsonl`: one scenario per line, each a list of tasks run in
one fresh session, with an `expected` mapping of checks of the final answer.

```json
{"scenario_id":"wikipedia-python","tasks":["Открой Википедию.","Найди статью про язык программирования Python.","Открой статью.","Выведи первый абзац статьи."],"expected":{"contains":"Python"},"tags":["wikipedia","extract"]}
```

The set has 18 scenarios. Each one exercises a capability of the agent:

| Group | Scenarios | Sites | Stability |
|---|---|---|---|
| Navigation, extraction, comparison, lists | `books-*` | `books.toscrape.com` (scraping sandbox) | stable content, no anti-bot |
| Pagination, JavaScript-rendered page, tag filter, login form | `quotes-*` | `quotes.toscrape.com` (sandbox; any login works) | stable |
| Form fill and submit | `httpbin-form-submit` | `httpbin.org/forms/post` (echoes the posted fields) | stable |
| Search, facts, a follow-up task in the same session, English | `wikipedia-*` | Wikipedia | stable facts, layout may change |
| Repository facts | `github-*` | GitHub | stable facts, rate limits possible |
| Shopping search, filters, sorting, comparison tables | `ozon-*`, `compare-rtx5070` | Ozon | live catalog: anti-bot, prices and results change |

The sandbox scenarios are the reference: a failure there points at the agent. A failure on Ozon
or GitHub needs a look at the trace first.

### 1. Prerequisites

Check the environment first:

```powershell
uv run main.py doctor --llm
```

It must find Chrome, `node` and `npx` (Playwright MCP is started through `npx`), and list the
models of the configured provider. The run uses your normal settings (`config.yaml`, `.env`,
`AUTOBROWSER_*`): the model, its API key (`AUTOBROWSER_LLM__API_KEY` or `llm.api_key`), the Chrome
path and profile. See [Development Setup](setup.md#configuration) and
[Model provider](setup.md#model-provider).

Nobody answers approval prompts in a batch: a configured `default` permission mode runs as
`dont_ask`, so a rule that asks becomes a deny the model can read.

### 2. Run

```powershell
uv run scripts/run_batch.py --tasks tests\golden\tasks.jsonl --continue-on-error --grade
```

- `--grade` grades the final answer of every scenario's last task against its `expected` and
  exits `1` when a grade or a scenario fails.
- `--continue-on-error` keeps going after a scenario that raised, so every scenario gets a grade.
  Without it the first such scenario stops the batch with a traceback (exit `1`, no summary).
- Do not pass `--no-mcp`: without MCP there is no browser.
- Useful options: `--model NAME` (another model for this run), `--turn-cap N`,
  `--chrome-path`, `--cdp-port`. Each scenario gets its own fresh session, the next free CDP
  port and a fresh Chrome profile in `<home>/batches/<batch_id>/profiles/scenario-N`, so no
  cookie or login carries over; the profiles are deleted when the batch ends. Your own
  `browser.user_data_dir` profile is never used by a batch.
- To run some scenarios only, copy their lines into another JSONL file and pass it to `--tasks`.
- `--no-history` keeps an experiment out of the run history (see
  [Comparing runs](#6-comparing-runs)).

A full run takes a while: the Ozon scenarios are multi-step tasks on a heavy site.

### 3. Where the results land

The console prints a summary, and the files go to the AutoBrowser home (`~/.autobrowser`, or
`AUTOBROWSER_HOME`, or your `storage.root_dir`). The summary names the exact paths and adds up
the run:

```json
{
  "batch_id": "batch-…",
  "batch_dir": "C:\\Users\\you\\.autobrowser\\batches\\batch-…",
  "report_path": "…\\report.md",
  "run_index_path": "…\\run_index.jsonl",
  "status": "completed",
  "scenario_count": 18,
  "task_count": 61,
  "grades": {"passed": 14, "failed": 3, "ungraded": 1},
  "metrics": {"duration_ms": 1520000, "tasks": 61, "model_calls": 420, "input_tokens": 3100000,
              "output_tokens": 61000, "model_ms": 980000, "tool_calls": 260, "tool_ms": 310000,
              "tool_errors": 9, "policy_blocks": 0}
}
```

Open **`report.md`** first. Under the header it states the provenance of the run (commit with a
`(dirty)` mark, model, a short settings fingerprint and the settings sections that differ from
the code defaults) and splits the failures: "Agent failures: N · Infra failures: M · Passed: K".
Then a table with one row per scenario (grade, failure kind, tasks done, time, model calls,
tokens in and out, model time, tool calls, tool time, tool errors, permission blocks), a total
row, and every failed scenario grouped by failure kind, with its failed checks, its final answer
and links to its trace. Infra kinds come last, with the advice to rerun them.

`<home>/batches/<batch_id>/` holds everything about the run, traces included, so it can be
archived or shared on its own:

| File | What is in it |
|---|---|
| `report.md` | the readable report described above |
| `run_index.jsonl` | one row per scenario (contract below) |
| `batch.json` | run metadata and provenance (contract below) |
| `tasks.jsonl` | a copy of the input set |
| `traces/<name>.events.jsonl` | a copy of the scenario's full trace |
| `traces/<name>.txt` | the same trace, readable: per task its text, its outcome (`status`, `stop_reason`, `failure_kind`) and its action sequence |
| `compare.md` | written by `scripts/compare_batches.py` into the newer of two compared runs |

`<name>` is the scenario id with every character outside `[A-Za-z0-9._-]` replaced by `_`
(`-2`, `-3` on a collision). A scenario that raised before its session wrote a trace has no
trace files.

`run_index.jsonl` row, `schema_version` 1:

| Key | Meaning |
|---|---|
| `schema_version` | `1` |
| `batch_id`, `scenario_id`, `session_id`, `session_dir` | identifiers |
| `status` | `completed`, or `failed` when the scenario raised |
| `tasks`, `expected`, `tags` | the scenario as written in the tasks file |
| `task_statuses` | `done`/`blocked`/`cancelled` per task that returned |
| `final_answer` | the answer of the last task |
| `grade` | with `--grade`: `status` and one check per `expected` key |
| `failure_kind` | one word for the scenario ([Failure kinds](#failure-kinds)) |
| `task_outcomes` | per task: `task_id`, `status`, `stop_reason`, `error_type`, `permission_denies`, `failure_kind` |
| `metrics` | figures from the trace (table below) |
| `error` | `{type, message}` when the scenario raised |
| `started_at`, `finished_at` | ISO timestamps |
| `trace_path`, `trace_text_path` | `traces/<name>.events.jsonl` and `traces/<name>.txt`, relative to the batch directory; `null` without a trace |

`batch.json`, `schema_version` 1: `batch_id`, `started_at`/`finished_at`, `status`, `config`
(the CLI options), `tasks_path`, `scenario_count`, `task_count` and `provenance`:

| Key | What makes two runs comparable |
|---|---|
| `git.commit`, `git.branch`, `git.dirty` | the checkout the run used (`null` without git) |
| `model`, `provider` | the chat model of the run |
| `tasks_sha256` | sha256 of the tasks file with CRLF normalized to LF: the version of the Golden Set |
| `settings_sha256` | sha256 of the redacted effective settings (no secret reaches it) |
| `non_default_sections` | settings sections that differ from the code defaults, e.g. `["llm", "permissions"]` |
| `python`, `platform` | interpreter version and `sys.platform` |

Every session of a batch carries the labels `autobrowser.batch.id`, `autobrowser.scenario.id`
and `vcs.ref.head.revision` (the commit) in its `session.started` event.

`<home>/sessions/<session_id>/` (one session per scenario):

| File | What is in it |
|---|---|
| `events.jsonl` | the full trace: every model call (`model.responded`, with token counts and durations in `usage`), every tool call and result, permission decisions, observations, goal events |
| `tasks.json` | the session's tasks with their answers and statuses |
| `session.json`, `workspace/` | session metadata and files the agent wrote |

Replay one scenario's trace as a compact action sequence:

```powershell
uv run scripts/replay_trace.py <session_dir>\events.jsonl
```

The `metrics` of a row come from the scenario's `events.jsonl` (`src/agent_loop/benchmarks.py`):

| Metric | Meaning |
|---|---|
| `duration_ms` | wall time of the scenario, session start included |
| `tasks` | tasks that reached the agent |
| `model_calls`, `model_ms` | model calls (plan, agent and approval phases) and their summed latency |
| `input_tokens`, `output_tokens` | summed token counts the provider reported; `null` (`—` in the report) when it reported none |
| `tool_calls`, `tool_ms` | tool calls and their summed latency |
| `tool_errors`, `policy_blocks` | failed tool calls; calls a permission rule, hook or policy blocked |
| `time_to_first_tool_ms` | of the first task: from the task start to the first tool call |

Traces written before token counts were exempted from redaction hold `[REDACTED]` instead of the
counts, so older runs show no tokens. Not there yet (later steps of the plan): latency
percentiles, cost in money.

#### Failure kinds

"Failed" mixes unrelated causes: a provider outage is not an agent regression. Every task and
scenario gets one `failure_kind` (`src/agent_loop/failures.py`), derived from its goal events,
the `stop_reason` they carry, permission denies and the grade, never from the site:

| Kind | When |
|---|---|
| `passed` | every task ended `done` and the grade passed |
| `provider_error` | infra: the model provider failed (`ModelProviderError`) |
| `timeout` | infra: the task or the progress watchdog timed out |
| `permission_denied` | an approval was refused, or a permission deny made an unfinished task fail |
| `guard_limit` | a loop limit ended the task: turn cap, replans, consecutive failures, a stalled plan |
| `agent_stopped` | the model gave up (`stop`); why is in its answer: an anti-bot page, a login wall |
| `grade_failed` | every task ended `done`, but the answer failed a check |
| `error` | any other exception, a missing terminal event, or a cancellation from outside |
| `ungraded` | every task ended `done` and nothing was checked |

The first task whose kind is not `passed` decides the scenario; when all passed, the grade does.
A scenario that raised is classified by its exception unless a task already failed. The engine's
`stop_reason` values: `answered`, `model_stop`, `turn_cap`, `max_replans`,
`consecutive_failures`, `no_plan_advance`, `approval_denied`, `approval_hook_denied`,
`goal_hook_denied`, `no_action`, `invalid_decision`, `cancelled`.

### 4. Reading the grades

A row's `grade` has a `status` and one check per `expected` key:

```json
{"status": "failed", "checks": [
  {"key": "items", "status": "passed", "message": ""},
  {"key": "regex", "status": "failed", "message": "no match for ['(?si)(?:(?:₽|…).*){3}']"},
  {"key": "numbers_at_most", "status": "passed", "message": ""}
]}
```

| Grade | Meaning |
|---|---|
| `passed` | no check failed |
| `failed` with a `run` check | the scenario raised, or a task did not end `done` (a `blocked` answer quotes the page it stopped on, so its text is never graded) |
| `failed` with an `expected` check | `expected` is not a mapping of checks (a typo such as `"expected": "Python"`) |
| `failed` otherwise | the answer misses a check; the `message` says which and why |
| `ungraded` | no `expected`, or only keys without a grader (`fields`) |

Before calling a failure a regression, open the scenario's `final_answer` and its trace: on live
sites a CAPTCHA, a login wall or a changed page is the usual cause.

### 5. Writing `expected`

The graders (`src/agent_loop/graders.py`) look at the text of the final answer only, never at a
site:

| Key | Check |
|---|---|
| `contains` | every string is in the answer (case-insensitive); a string or a list |
| `not_contains` | none is (case-insensitive) |
| `regex` | every pattern matches (`re.search`; `(?i)` for case, `(?s)` across lines) |
| `items`, `min_items`, `max_items` | the number of items: a numbered answer counts its numbering 1, 2, 3… (`1.`, `1)`, `**1.**`, `### 1.`); otherwise the largest bullet list; markdown table body rows count too |
| `numbers_at_most`, `numbers_at_least` | every number in the answer is within the bound, and there is at least one; `120 000`, `1 299,99` and `1,299.99` are one number; URLs and list ordinals are skipped |
| any other key | `ungraded`: reported, never a failure |

Known limits, and how the set works around them:

- Unnumbered bullet attributes of one item look like items. Pair an item count with a per-item
  check, as the Ozon scenarios do with a price that must appear N times:
  `"regex": "(?si)(?:(?:₽|\\bруб(?:\\.|л|\\b)|\\bр\\.|\\brub\\b).*){3}"`.
- `numbers_*` see every number, old prices and product codes included.
- Whether every item has a title and a price (`fields`) cannot be checked deterministically. It
  stays `ungraded` until an LLM judge is decided (open question Q7 of the design).

### 6. Comparing runs

Every batch appends one row per scenario to `<home>/batches/history.jsonl`: `schema_version`,
`batch_id`, `started_at`, `git` (`commit`, `dirty`), `model`, `tasks_sha256`, `settings_sha256`,
`scenario_id`, `grade_status`, `failure_kind` and the metrics as top-level keys. The file keeps
the newest `pipeline.history_max_rows` rows (default 50 000); `--no-history` skips the append.

```powershell
uv run scripts/compare_batches.py                      # latest run vs the previous comparable one
uv run scripts/compare_batches.py <batch_a> <batch_b>  # ids or directories; b is the newer
uv run scripts/compare_batches.py --json               # machine output
uv run scripts/compare_batches.py --perf-gate          # also fail on slower/costlier scenarios
```

Two runs are comparable when they ran the same Golden Set (`tasks_sha256`); a different settings
fingerprint or model is printed as a warning. Per scenario present in both runs:

| Before → after | Finding |
|---|---|
| `passed` → an agent kind | `regression` |
| an agent kind → `passed` | `fixed` |
| anything → `provider_error`/`timeout` | `inconclusive`: rerun it |
| any other change | `changed` |
| in one run only | `added` / `removed` |

For scenarios passed in both, growth of `duration_ms` (`slower`), `model_calls` or
`input_tokens` (`costlier`) past `pipeline.batch_metric_tolerance` (default 0.25, i.e. +25 %) is
reported. A single live run is noisy, so these fail the command only with `--perf-gate`.

Exit codes: `0` no regression; `1` at least one `regression`; `2` unreadable input, different
Golden Sets or no previous comparable run; `3` only slower/costlier findings under `--perf-gate`.
The findings also go to `compare.md` in the newer batch directory.

The rows are plain JSONL, so DuckDB queries them directly:

```sql
SELECT scenario_id, failure_kind, metrics.duration_ms
FROM read_json_auto('~/.autobrowser/batches/*/run_index.jsonl')
WHERE failure_kind NOT IN ('passed', 'provider_error', 'timeout');

SELECT scenario_id, count(*) AS runs, count_if(failure_kind = 'passed') AS passed
FROM read_json_auto('~/.autobrowser/batches/history.jsonl')
GROUP BY scenario_id ORDER BY passed;
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `run_evals.py` exits `2` | the baseline file is missing, is a v1 file, or has a malformed value |
| `run_evals.py` reports `request_chars: regression` | the prompt or the tool schemas grew past the tolerance; rewrite the baseline if intended |
| every Golden Set scenario fails at once | the model is not reachable (`doctor --llm`) or Chrome/Playwright MCP did not start |
| one Ozon scenario `failed` with `run` | a task ended `blocked`: open the trace for a CAPTCHA, an anti-bot page or a guard limit |
| many scenarios `provider_error` or `timeout` | the provider is overloaded or down; rerun them, `compare_batches.py` calls them `inconclusive` |
| `compare_batches.py` exits `2` | the runs used different tasks files, or there is no earlier run of the same set |
| `items` fails on an answer that looks right | the answer's shape (a list split by bold names, a transposed table); see the limits above |
