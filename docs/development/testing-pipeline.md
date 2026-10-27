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
  `--chrome-path`, `--user-data-dir`, `--cdp-port`. Each scenario gets its own fresh session, its
  own Chrome profile (`<user-data-dir>-scenario-N`) and the next free CDP port.
- To run some scenarios only, copy their lines into another JSONL file and pass it to `--tasks`.

A full run takes a while: the Ozon scenarios are multi-step tasks on a heavy site.

### 3. Where the results land

The console prints a summary, and the files go to the AutoBrowser home (`~/.autobrowser`, or
`AUTOBROWSER_HOME`, or your `storage.root_dir`). The summary names the exact paths:

```json
{
  "batch_id": "batch-…",
  "batch_dir": "C:\\Users\\you\\.autobrowser\\batches\\batch-…",
  "run_index_path": "…\\run_index.jsonl",
  "status": "completed",
  "scenario_count": 5,
  "task_count": 27,
  "grades": {"passed": 3, "failed": 1, "ungraded": 1}
}
```

`<home>/batches/<batch_id>/`:

| File | What is in it |
|---|---|
| `run_index.jsonl` | one row per scenario: `scenario_id`, `status` (`completed` or `failed` when it raised), `task_statuses` (`done`/`blocked`/`cancelled` per task), `final_answer` of the last task, `grade` with every check, `error`, `started_at`/`finished_at`, `session_id`/`session_dir` |
| `batch.json` | run metadata: the model and options, start and end time, overall status |
| `tasks.jsonl` | a copy of the input set |

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

There is no aggregated metrics report per run yet (total tokens, latency by phase, cost): that is
step 6 of the pipeline plan. Today the per-call token counts and durations are in `events.jsonl`,
and the scenario time is `finished_at - started_at` in `run_index.jsonl`.

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `run_evals.py` exits `2` | the baseline file is missing, is a v1 file, or has a malformed value |
| `run_evals.py` reports `request_chars: regression` | the prompt or the tool schemas grew past the tolerance; rewrite the baseline if intended |
| every Golden Set scenario fails at once | the model is not reachable (`doctor --llm`) or Chrome/Playwright MCP did not start |
| one Ozon scenario `failed` with `run` | a task ended `blocked`: open the trace for a CAPTCHA, an anti-bot page or a guard limit |
| `items` fails on an answer that looks right | the answer's shape (a list split by bold names, a transposed table); see the limits above |
