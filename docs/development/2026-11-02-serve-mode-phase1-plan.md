# План реализации Фазы 1: replay (запись, воспроизведение, перенацеливание, бюджеты)

Статус: Approved (2026-11-02). Дополнительный прогон `agent_loop_v2` после шагов 6 и 9 и
usage в `goal.*` одобрены; строка `gate.yml` для `playback_v1` не добавляется.
Дата: 2026-11-02
Ветка: `feat/serve-mode`
Дизайн: [serve mode design](2026-11-02-serve-mode-design.md) (§4–§6, §9.1, §9.3, §13, §14).
ADR: [2026-11-02 serve mode and cost ladder](../decisions/2026-11-02-serve-mode-and-cost-ladder.md)
(Accepted).

Фаза 1 даёт replay обоим режимам: запись удачного прохода, воспроизведение без модели (ступень 1),
перенацеливание одного разошедшегося шага дешёвой моделью (ступень 2), передачу агенту (ступень 3),
бюджеты задачи и учёт стоимости. `serve`, проба (ступень 0), headless, каскад и Telegram — фазы 3–4.

---

## 0. Что выяснилось в коде (уточнения к дизайну)

Код сверен с эскизами §4.2–§5 дизайна. Ниже расхождения и решения по ним. Все они аддитивны и не
меняют решений ADR.

| # | Наблюдение | Где | Решение в реализации |
| --- | --- | --- | --- |
| U1 | `steps_without_plan_advance` в engine никто не увеличивает, а `current_step` меняется только в `_run_plan` (0) и в `done_response` (len). Риска `no_plan_advance` при длинной записи, который описан в §4.3, в текущем коде нет. | `guards.py:139`, `observation.py:250`, `loop.py:1117` | `plan_step` из записи двигает только `current_step` (для `plan.updated` и TUI). Отдельной заморозки счётчика не нужно. Тест фиксирует, что запись на 10 шагов не упирается в `max_steps_without_plan_advance`. |
| U2 | Повтор-guard `_guard_tool_request` (тот же вызов 3 раза подряд) возвращает `decision="replan"`. На ступени 1 это вызвало бы `_run_plan`, то есть платный вызов модели. | `guards.py:263`, `loop.py:1034` | В стадии `playback`/`retarget` replan от guard'а превращается в `StepHandoff(kind="repeat")`: агент получает заметку, а план модель строит обычным ходом. В `StepHandoff.kind` добавляется значение `repeat`. |
| U3 | В реальном выводе Playwright MCP префикс фрейма есть и у главного фрейма (`[ref=f3e2]`) и меняется между загрузками страницы. | `~/.autobrowser/sessions/*/events.jsonl` | `rebind` сопоставляет строки по `role`+`name`+контексту, **не** по префиксу. `frame` в дескрипторе только разрешает неоднозначность. Фикстуры снимков берутся из реальных сессий (обрезанные, без личных данных). |
| U4 | Источник шагов не может эмитить события: `model.requested/responded{phase="retarget"}` должны идти из engine-эмиттера, иначе нарушится `goal_id`/`task_id` и порядок `sequence`. | `loop.py:216` | `StepSourceFactory.build(...)` получает нейтральный колбэк `emit(event_type, payload)`, который engine привязывает к своему `_emit`. В `src/contracts.py` это обычный `Callable`. |
| U5 | Источнику нужны остаток бюджета (перед `Retargeter`) и потолок лестницы (можно ли на ступень 2). | §5.4, §6.2 | `StepView` получает `usage: TaskUsage`; `build(...)` получает `max_stage` и `budget`. |
| U6 | Классификатор подтверждений (`approval_judge`) эмитит `model.*{phase="approval"}` без usage. Консолидация памяти идёт после `goal_end`, вне задачи. | `loop.py:769`, `session.py:1245` | `TaskUsage.model_calls` считает и вызовы классификатора; их токены неизвестны, поэтому при включённом классификаторе и заданных ценах `cost=None` (честно, как требует §6.2). Консолидация памяти в бюджет задачи не входит — это записано в руководстве. Оба механизма выключены по умолчанию. |
| U7 | `/record` требует метода на `ControllerPort` (дизайн говорил, что порт не меняется). | `src/ui/tui/ports.py:36` | Один аддитивный метод `arm_recording(name: str \| None)` и поле `SessionView.recording_armed`. |
| U8 | `scripts/run_evals.py` читает один каталог сценариев и сравнивает с одним baseline; новый сценарий в `tests/evals/scenarios/` изменил бы `agent_loop_v2`. | `tests/evals/runner.py`, `scripts/run_evals.py:86` | Сценарии playback живут в `tests/evals/playback_scenarios/` со своим baseline `playback_v1.json`; `run_evals.py` получает `--scenarios DIR`. `agent_loop_v2.json` не трогается. Строка в `.github/workflows/gate.yml` — только с вашего согласия (в плане не делаю). |
| U9 | Engine вызывает `resources.llm` напрямую в `_run_plan` и `_agent_step`, а цены и метка модели нужны для `TaskUsage`. | `loop.py:335`, `loop.py:1108` | В шаге 9 `EngineResources.models: ModelTiers \| None`. Без него engine строит один уровень из `resources.llm` без цены, поэтому evals и тесты ведут себя как сейчас. `build_agent` всегда передаёт один уровень `(llm, config.model, llm.prices.get(config.model))`. Каскад (несколько уровней) — фаза 4. |
| U10 | `TaskRequest` идёт в engine через `Agent.run → AgentLoopEngine.run(...)` отдельными аргументами. | `loop.py:1174` | `AgentLoopEngine.run` получает keyword `profile: RunProfile \| None = None`; `Agent.run` передаёт `request.profile`. |

Принятые ответы Q1–Q15 действуют без изменений. Для Фазы 1 важны Q2 (черновик промпта в PR),
Q4 (замена записи только после прогона со ступенью ≥2), Q6 (отредактированные шаги не
воспроизводятся), Q8 (без R1), Q9 (имена) и Q15 (`-p --recording` и `/record`, без replay в TUI).

---

## 1. Правила партии

- Один шаг — один коммит (скилл `commit`, английский, scope из `.claude/conventions/commit.md`).
  Тесты пишутся в том же коммите, что и код. На каждом шаге гоняются только тесты этого шага;
  полный `uv run pytest` и evals — один раз в конце (§3).
- После шага 6 и шага 9 дополнительно прогоняется `uv run scripts/run_evals.py --baseline
  tests\evals\baselines\agent_loop_v2.json`. Это дешёвая проверка неизменности baseline после
  правок engine, чтобы не копить регрессию до конца партии. Если не нужно — уберу.
- Новое поведение выключено: `replay.enabled=false`, бюджеты `None`, `RunProfile()` по умолчанию.
  Без профиля engine не строит источник, не эмитит новых событий и делает столько же вызовов
  модели.
- Name-free: разбор `[ref=…]`, строки элемента, `- Page URL:` и отпечаток структуры — только в
  `src/browser/playback.py`. `src/agent_loop/execution/` не импортирует `src/browser/`,
  `src/harness/playback.py`, `src/harness/recorder.py`, `src/harness/recording_store.py`.
- Каждый тюнабл — поле `src/config.py` + `.env.example` + `config.example.yaml` +
  `tests/test_config.py`.
- `PLAYBACK_RETARGET_PROMPT` — черновик в шаге 8, финальную правку делаете вы.
- Не пушу и не мержу.

---

## 2. Шаги

### Шаг 1. Контракты без поведения

**Коммит:** `feat(agent-loop): add run profile, step source and usage contracts`

`src/contracts.py` (нейтральный лист, только stdlib):
- `LadderStage`, `LADDER_ORDER`, `PlaybackAnswer`, `StepHandoffKind` (виды §5.5 плюс `answer`,
  `exhausted`, `repeat` — U2);
- `StopReason += "budget_exhausted", "ladder_cap", "playback_completed"`;
- `ApprovalVia += "api", "channel"` (для фазы 3, сейчас только тип);
- `RunBudget`, `PlaybackRequest`, `RunProfile`, `TaskUsage` (+ `ModelCallRecord{phase, model,
  usage: Mapping[str, int]}` вместо `ModelUsage`, потому что `src/llm.py` импортирует
  `src/contracts.py`, а не наоборот);
- `StepView` (+ `usage`, U5), `StepProposal`, `StepFinal`, `StepHandoff`, `StepOutcome`,
  `StepSource`, `StepSourceFactory` (`build(request, *, task, budget, max_stage, emit)`, U4/U5),
  `Binding`, `BindResult`, `PlaybackAdapter`;
- `TaskRequest.profile: RunProfile = field(default_factory=RunProfile)`.

`src/llm.py`: `ModelPrice` (`cost(usage) -> float | None`), `ModelTier`, `ModelTiers`
(`default(llm)` — один уровень без цены).

`src/agent_loop/execution/state.py`: task-local поля `ladder_stage="agent"`, `model_tier=0`,
`usage: dict = {}`, `retargets=0`. Они не попадают в `to_session_state`.

`src/harness/session.py`: те же ключи в `TASK_BOUNDARY_RESETS`.

`src/agent_loop/events.py`: `EventType += ladder.escalated, playback.started, playback.step,
playback.diverged, recording.saved, recording.skipped`; новые типы движка — в
`AGENT_TRACE_EVENT_TYPES`.

**Тесты:** `tests/test_contracts.py` (дефолты `RunProfile()`/`TaskRequest` эквивалентны
сегодняшним, `LADDER_ORDER`, `ModelPrice.cost` с `None`-полями, frozen),
`tests/test_harness_session.py` (новые ключи сбрасываются на границе задачи),
`tests/test_engine_boundaries.py` (контракты не импортируют слои).

### Шаг 2. Телеметрия под рекордер

**Коммит:** `feat(agent-loop): report server and read-only on tool.started`

- `loop.py::_run_tool_turn`: `tool.started` → `{tool_request, server, read_only}`;
  `action.proposed` → `{tool_request, source: "model"}`. Источник `playback`/`retarget`
  появится в шаге 6.
- `src/harness/otel_spans.py`: `server` → атрибут `execute_tool` span'а, если он ещё не
  ставится. Проверю и не дублирую.
- `src/agent_loop/replay.py` и экспорт трейсов: новые поля только читаются, формат строк не
  меняется.

**Тесты:** `tests/test_agent_loop_events.py` (или ближайший тест событий engine): поля есть,
`read_only` берётся из `readOnlyHint`; `tests/test_otel_spans.py`.

### Шаг 3. Секция `replay`, формат записи, хранилище

**Коммит:** `feat(harness): add the replay settings and the recording store`

- `src/config.py`: `ReplaySettings` — все поля §9.1 плюс `text_shingle_words` (int, `3`,
  `1..10`) для отпечатка `TextPlaybackAdapter`; `Settings.replay`. Это 16-я секция.
  `.env.example`, `config.example.yaml`, `tests/test_config.py`, список секций в `CLAUDE.md`
  (там же, где перечислены пятнадцать).
- `src/agent_loop/recordings.py` (чистый модуль, как `replay.py`): `Recording`, `RecordedStep`,
  `Checkpoint`, `RECORDING_FORMAT = "autobrowser.recording/1"`, `RECORDING_NAME` (regex имени —
  лексика формата, как у скиллов), `schema_digest(input_schema)` (sha256 канонического JSON),
  `to_json`/`from_json` со строгой валидацией и понятной `RecordingFormatError`.
- `src/harness/recording_store.py`: `RecordingStore(root, settings)` — `load(name)`,
  `save(recording)` (атомарно: временный файл + `os.replace`; `previous.json` при
  `keep_previous`), `list()`, `delete(name)`, `record_replay(name, stats)` → `stats.json`.
  Имя проверяется до любого пути.

**Тесты:** `tests/test_recording_store.py` — roundtrip, чужая версия формата, повреждённый JSON,
имя с `..`/`/`/заглавными, атомарность (сбой записи не портит прежний файл), `previous.json`,
`stats.json` не трогает `recording.json`; `tests/test_config.py`.

### Шаг 4. Браузерный адаптер

**Коммит:** `feat(browser): rebind recorded refs by element descriptor`

- `src/browser/playback.py` — `BrowserPlaybackAdapter` (R2, §5.3):
  - `describe`: обходит строковые значения аргументов на любой глубине. Значение — ссылка, если
    в `page_texts` есть `[ref=V]`. Дескриптор: `role`, `name` (переиспользует разбор строки
    `snapshot_element`, вынесенный в общий хелпер в `src/browser/pages.py` без изменения
    поведения резолвера), `nth`, `context` (до `describe_context_lines` предков по отступу),
    `frame`;
  - `rebind`: поиск по `role`+`name` → сужение по `context` → по `nth`; префикс фрейма не
    обязателен (U3). Режим `verify` для шага 8 проверяет, что значения на путях привязок — живые
    ref'ы текущей страницы;
  - `checkpoint`: `url` (хост+путь из `page_url`, режим `checkpoint_url`), `structure` —
    мультимножество `role`+`name` интерактивных строк без ref'ов, чисел и `[cursor=…]`;
  - `similarity`: несовпадение пути → 0, иначе Жаккар мультимножеств;
  - `region`: `select`/`ignore`, нормализация пробелов, удаление ref'ов.
- `src/harness/playback.py` (только `TextPlaybackAdapter` в этом шаге): без привязок, отпечаток —
  шинглы по `text_shingle_words` слов. `PlaybackSource` появится в шаге 7.
- Фикстуры `tests/fixtures/snapshots/*.txt`: реальные ответы Playwright MCP из локальных сессий,
  обрезанные и без личных данных — главный фрейм с `f3e…`, iframe, одинаковые кнопки, смена цены,
  смена пути.

**Тесты:** `tests/test_browser_playback.py` — уникальный, отсутствующий и неоднозначный элемент,
`nth`, контекст, смена префикса фрейма, `[cursor=pointer]`, цена изменилась → сходство ≥ порога,
путь изменился → 0, `region` с `select`/`ignore`. `tests/test_engine_boundaries.py` уже ловит
`browser_*` в новых модулях; добавлю проверку, что `[ref=`/`Page URL` встречаются только в
`src/browser/`.

### Шаг 5. Рекордер, `run_task(profile)`, CLI записи

**Коммит:** `feat(session): record completed runs as replayable recordings`

- `src/harness/recorder.py`: `Recorder` — session-scoped `EventSink` над сырыми `EventRecord`
  (до редактирования):
  - `arm(task_id, name)` / `finish(task_id, goal_status, result) -> Recording | None`;
  - шаг — пара `tool.started` / `tool.finished` со статусом `success`, без служебных серверов
    (набор `memory`/`skills` передаёт композиция, а не литерал в рекордере);
  - зеркало «последний вывод + снимки по серверам» по правилам `observation.py:196-200` → текст
    страницы до шага → `adapter.describe`;
  - `redact_json_safe` на аргументах, шаг с отредактированным значением помечается
    `redacted: true`;
  - план — из последнего `plan.updated` до первого шага; `plan_step` — текущий шаг плана;
  - каталог — `schema_digest` и `read_only` использованных инструментов, читается из реестра
    сессии;
  - сохранение — только `completed` и не больше `replay.max_steps` шагов. Иначе
    `recording.skipped{reason}`.
- `src/harness/session.py`:
  - `run_task(task, *, profile=None)` кладёт профиль в `TaskRequest`;
  - `record_as` при `replay.enabled` → `recorder.arm` до `GoalRunner.run`, `recorder.finish`
    после — рядом с `_update_memory`, сбой записи даёт `recording.skipped` и никогда не валит
    задачу;
  - `carry_context` в Фазе 1 только передаётся; поведение `False` — шаг 12 Фазы 3.
- `src/ui/controller.py`: `submit(text, *, profile=None)`, `run_outcome(text, *, profile=None)`,
  `arm_recording(name)`: профиль следующей задачи с `record_as`; `SessionView.recording_armed`.
  `src/ui/tui/ports.py`: метод в `ControllerPort` (U7).
- `src/ui/tui/commands.py`: `/record <name>` взводит запись следующей задачи, `/record off`
  снимает. Без `replay.enabled` — понятное сообщение.
- CLI: `--record <name>` для начальной задачи (`--task`, `-p`); подкоманда
  `autobrowser recordings list | show <name> | rm <name>` в `src/cli/subcommands.py`.

**Тесты:** `tests/test_recorder.py` — синтетические потоки событий: пропуск `error`, шаги без
`tool.started` (блоки guard/hook/permission), служебные серверы, редактирование, зеркало снимков
совпадает с `LoopState.browser.snapshots` на одной последовательности, `max_steps`.
`tests/test_harness_session.py` — запись после `goal_end`, сбой записи не валит задачу, без
`replay.enabled` ничего не пишется. `tests/test_ui_controller.py`, `tests/ui/…` (команда
`/record`), `tests/test_cli_entry.py` (`recordings`).

### Шаг 6. Engine: источник шагов

**Коммит:** `feat(agent-loop): drive turns from a step source before the model`

- `resources.py`: `EngineResources.steps: StepSourceFactory | None = None` (без Null-фабрики:
  `None` = источник не строится).
- `loop.py`:
  - `AgentLoopEngine.run(..., profile=None)`; `Agent.run` передаёт `request.profile` (U10);
  - при `profile.playback` и `resources.steps`: `steps.build(...)` → `playback.started`;
    `source.plan()` → план без вызова №0, `ladder_stage="playback"`. Без плана в записи — обычный
    `_run_plan`;
  - `TurnController._agent_step`: в стадии `playback`/`retarget` спрашивает `source.next(view)`
    раньше модели:
    - `StepProposal` → `tool_request_update` (синтетический assistant tool call),
      `action.proposed{source}`, `current_step = plan_step` (U1);
    - `StepFinal` → `done_response(stop_reason="playback_completed")`; `stop` hooks
      применяются, как к ответу модели;
    - `StepHandoff` → при `max_stage < agent` `blocked(ladder_cap)`; иначе `[harness]` заметка,
      `ladder.escalated{from,to,reason,kind,step}`, `playback.diverged`, `ladder_stage="agent"` и
      обычный ход модели **в этом же ходе**;
  - после `_run_tool_turn` в стадии источника: `source.observe(...)` и `playback.step{step, stage,
    binding, similarity?}`. Отказ hook'а или permission доходит до `observe` как результат
    `blocked`, и источник отвечает `StepHandoff(kind="denied")`. Отказ в подтверждении, как и
    сейчас, терминален;
  - replan от повтор-guard'а в стадии источника → `StepHandoff(kind="repeat")` (U2);
  - `StepView` строится из `LoopState` и каталога инструментов; хелпер `tool_catalog(tools)` в
    `execution/tools.py` использует ту же семантику `server`/`read_only`, что `ToolBroker`.

**Тесты:** `tests/test_agent_loop_playback.py` с фейковым `StepSource` и фейковыми инструментами:
- план без вызова №0, `StepFinal` — 0 вызовов модели;
- `StepHandoff` — модель видит историю и заметку, вызов №0 не делается;
- deny на шаге → передача, не терминал; отказ в подтверждении → `blocked`;
- `max_stage="playback"` → `ladder_cap`;
- запись на 10 шагов не даёт `no_plan_advance` (U1);
- три одинаковых шага → `repeat`, а не replan (U2);
- `stop` hook отклоняет `StepFinal` → ход модели;
- без профиля поток событий и число вызовов модели байт-в-байт как раньше (эталон —
  существующий сценарий).

Плюс прогон `agent_loop_v2` (§1).

### Шаг 7. `PlaybackSource` (ступень 1) и `-p --recording`

**Коммит:** `feat(harness): replay recordings without the model`

- `src/harness/playback.py`:
  - `PlaybackFactory(store, adapter, settings, retargeter=None)` — реализация
    `StepSourceFactory`;
  - `PlaybackSource` — каталог → `redacted` → `rebind` до вызова → `StepProposal`; `observe` —
    `tool_error`, `checkpoint`, `denied`;
  - конец шагов по `answer`: `agent` → `StepHandoff(kind="answer")`, `output` →
    `StepFinal(region(...))`, `summary` → фиксированная сводка;
  - ступень 2 здесь — передача агенту: `Retargeter` подключается в шаге 8;
  - после задачи `stats.json`: прогон без ступени ≥2 запись не переписывает (Q4).
- `SessionContext.build_agent()`: `steps=PlaybackFactory(...)` при `replay.enabled`. Адаптер —
  `BrowserPlaybackAdapter`, если сконфигурирован браузерный сервер (тот же признак, что
  `_capabilities`), иначе `TextPlaybackAdapter`.
- `Recorder` (из шага 5): при `record_as` и прогоне со ступенью ≥2 заменяет запись,
  старая уходит в `previous.json`.
- CLI: `-p --recording <name> [--answer agent|output|summary]`. Без `replay.enabled` или с
  неизвестной записью — `USAGE_EXIT_CODE` и понятный текст. `--output-format json` получает
  `stop_reason` и `stage`. `TaskOutcome.stop_reason/stage` добавляются здесь, `usage` — в шаге 9.
- Тестовый MCP-сервер `tests/mcp_fixtures/fake_page_server.py`: настоящий stdio MCP с
  `snapshot` (`readOnlyHint`), `navigate`, `click`; печатает `- Page URL:` и `[ref=…]`, ref'ы
  меняются между снимками, а режим «изменённая вёрстка» включается переменной окружения.

**Тесты:** `tests/test_playback_source.py` — каждый вид расхождения, `answer` во всех режимах,
`stats.json`. `tests/test_print_mode.py` — `--recording`. `tests/test_playback_integration.py` —
запись (scripted model) → replay без модели (0 вызовов) → изменённая вёрстка → передача агенту
(`FakeChatModel`) поверх `fake_page_server.py`.

### Шаг 8. `Retargeter` (ступень 2) и черновик промпта

**Коммит:** `feat(harness): retarget a diverged step with one model call`

- `src/harness/playback.py`: `Retargeter(llm, adapter, settings)`:
  - один вызов `ChatModel.complete(messages, tools=[ToolDef записанного инструмента])` с
    `retarget_timeout_seconds`;
  - сообщения по §5.4, текст страницы обрезан до `retarget_context_chars`;
  - ответ принимается, только если вызван ровно один инструмент, это записанный инструмент и
    `adapter.rebind(verify=True)` подтвердил все пути привязок. Иначе
    `StepHandoff(kind="binding")`;
  - перед вызовом проверяются `max_stage ≥ retarget`, `retarget_enabled`,
    `max_retargets_per_run` и остаток бюджета из `StepView.usage` (U5);
  - события `model.requested/responded{phase="retarget"}` идут через колбэк `emit` (U4); usage
    попадает в `source.usage`.
- `src/agent_loop/prompts.py`: **черновик** `PLAYBACK_RETARGET_PROMPT` по требованиям §5.4.
  Финальный текст ваш. `tests/test_prompts.py` дополняется и запускается.
- Retarget на `tool_error` повторяет шаг один раз, на `checkpoint` не применяется (§5.5).

**Тесты:** `tests/test_retargeter.py` с `FakeChatModel` — принят; чужой инструмент; нет вызова
(текст); два вызова; выдуманный ref; таймаут; лимит перенацеливаний; бюджет исчерпан;
`max_stage="playback"`. `tests/test_prompts.py`.

### Шаг 9. Бюджеты и цены

**Коммит:** `feat(agent-loop): account task usage and enforce run budgets`

- `src/config.py`:
  - `llm.prices: dict[str, ModelPriceSpec]` (`input_per_mtok`, `output_per_mtok`,
    `cached_input_per_mtok?`, все `ge=0`, `extra="forbid"`) и `llm.currency="USD"`;
  - `loop.max_task_model_calls`, `max_task_tokens`, `max_task_cost` (`None`, границы §9.3);
  - `.env.example`, `config.example.yaml`, `tests/test_config.py`.
- `resources.py`: `models: ModelTiers | None` (U9). Engine вызывает
  `tiers[state.model_tier].model` — при одном уровне это `resources.llm`.
- `loop.py`:
  - каждый `model.responded` (plan, agent, retarget через `source.usage`, approval — только
    счётчик, U6) прибавляется к `state.usage` по ступени и модели;
  - `model.*` получают `tier`;
  - действующий бюджет — поле профиля, иначе `loop.max_task_*`;
  - `CompletionController(budget=...)` проверяет его в `pre_turn_terminal` и перед каждым
    `_run_plan` → `blocked(budget_exhausted)`. Денежный лимит при `cost=None` не проверяется.
- `AgentLoopResult.usage: TaskUsage | None` (с `stage_reached`). `GoalRunner` кладёт `usage` и
  `stage_reached` в терминальные `goal.*` всегда: это аддитивное поле payload, новых событий нет.
  Аналитика бенчмарка нужна и для прогонов без replay.
- `SessionContext.build_agent()` передаёт один уровень `ModelTier(llm, config.model,
  price)`. `TaskOutcome.usage`, `print_mode --output-format json` → `usage`.

**Тесты:** `tests/test_agent_loop_budget.py`:
- лимит вызовов срабатывает до вызова, а не после;
- токены и деньги;
- `cost=None` без usage или без цены;
- бюджет профиля важнее настроек;
- retarget учитывается в `by_stage`;
- без бюджетов поведение прежнее.

Плюс `tests/test_config.py`, `tests/test_goal_runner.py` и прогон `agent_loop_v2` (§1).

### Шаг 10. Метрики для бенчмарка и evals playback

**Коммит:** `feat(evals): report ladder stage and usage per run and add playback evals`

- `src/agent_loop/batch.py`: в строке `run_index.jsonl` — `stage_reached` и `usage` каждой
  задачи (из `result.usage`; у чужих результатов — `None`). `SCHEMA_VERSION` поднимается, только
  если этого требует существующий читатель (`compare_batches`, Langfuse-мост). Проверю, что
  новые ключи их не ломают.
- `src/agent_loop/evals.py`: сценарий получает необязательный блок `recording:` (встроенная
  запись) и `profile:`. `run_scenario` строит `PlaybackFactory` над in-memory store и
  `_FakeBrowserTools`. Метрики сценария: `model_calls`, `stage_reached`.
- `tests/evals/playback_scenarios/`:
  - чистый replay (0 вызовов модели);
  - смена ref'ов (перепривязка);
  - пропавший элемент → retarget;
  - смена пути → передача агенту;
  - `answer=output`;
  - бюджет исчерпан.

  Baseline `tests/evals/baselines/playback_v1.json` пишется через `--write-baseline`.
- `scripts/run_evals.py`: `--scenarios DIR`, по умолчанию прежний каталог (U8).

**Тесты:** `tests/test_agent_loop_batch.py`, `tests/test_agent_loop_evals.py`,
`tests/test_eval_comparison.py` (если затронут).

### Шаг 10b. Документация

**Коммит:** `docs(agent-loop): document recordings, playback and run budgets`

- `docs/development/replay.md` — руководство: включить, записать, воспроизвести, бюджеты,
  отладка расхождений.
- Диаграммы `agent-runtime-flow.md` (ветка источника, пропуск вызова №0),
  `harness-boundaries.md` (`EngineResources.steps/models`), `session-runtime-sequence.md`
  (`Recorder`).
- `CLAUDE.md` и `AGENTS.md`: секция `replay`, команды, границы; `docs/glossary.md` — термины §11.
- Статус дизайна: «Фаза 1 реализована».

---

## 3. Проверка в конце партии

1. `uv run pytest` — полный набор.
2. `uv run scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v2.json --memory-seed
   tests\evals\memory_seed` — без изменений baseline.
3. `uv run scripts/run_evals.py --scenarios tests\evals\playback_scenarios --baseline
   tests\evals\baselines\playback_v1.json`.
4. `uv run main.py --no-mcp --task "inspect page"` — дымовой прогон: интерактивный путь не
   изменился.
5. `code-reviewer` один раз над всей партией. Подтверждённые замечания исправляю отдельным
   коммитом `fix(...)`, о неисправленных пишу с причиной. Повторный review — только после
   нетривиальных исправлений.
6. Отчёт: коммиты, результаты 1–4, замечания review, что осталось на вас (финальный текст
   `PLAYBACK_RETARGET_PROMPT`, строка в `gate.yml` для `playback_v1`).

Живой прогон с настоящим Chrome (`-m live`) в Фазе 1 не планируется. Если хотите, после партии
сделаю ручной сценарий: запись на реальном сайте → `-p --recording`.

---

## 4. Что не входит в Фазу 1

- `carry_context=False`, `allowed_domains`, `browser.launch/headless/extra_args`, `serve`, проба
  (ступень 0), `croniter` — Фаза 3.
- Каскад моделей (`cascade`, `tier_escalation`), Telegram, пул worker'ов — Фаза 4.
- Replay в TUI (кроме `/record`) — по Q15 не в v1.
- R1 (локатор из codegen) — по Q8 только после бенчмарка.
- Параметризация записей и `{{secret:NAME}}` — по Q6 позже.

---

## 5. Риски плана

| Риск | Смягчение |
| --- | --- |
| Правка `_agent_step` меняет поток событий без профиля. | Ветка источника полностью под `if source is not None`. Тест на байт-в-байт поток событий и прогон `agent_loop_v2` после шагов 6 и 9. |
| Формат реальных снимков богаче, чем фикстуры (вложенные `/url`, `text:` узлы, длинные списки). | Фикстуры из реальных сессий, включая GitHub-подобную страницу с `f3e…`. `rebind` консервативен: неоднозначность — это расхождение, а не угадывание. |
| Recorder и engine расходятся в правилах снимков. | Контрактный тест «зеркало рекордера == `LoopState.browser.snapshots`» на одной последовательности (§5.1 дизайна). |
| Usage в `goal.*` раздувает события. | Это несколько чисел. `by_stage`/`by_model` ограничены числом ступеней и моделей. |
| Шаг 5 большой (рекордер + сессия + UI + CLI). | Если коммит выйдет больше ~800 строк, разделю на 5a (рекордер и сессия) и 5b (UI и CLI). |
