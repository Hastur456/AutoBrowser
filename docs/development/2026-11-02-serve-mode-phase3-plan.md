# План реализации Фазы 3: стенд (`autobrowser serve`)

Статус: Implemented (2026-11-09), руководство — [serve-mode.md](serve-mode.md). Approved (2026-11-04): вопросы P1–P3 приняты по дефолтам.
P1 разрешился сам: `croniter` тянет `python-dateutil` 4, а тот — `tzdata`, поэтому IANA-пояса работают и на Windows без прямой зависимости.
Дата: 2026-11-04
Ветка: `feat/serve-mode`
Дизайн: [serve mode design](2026-11-02-serve-mode-design.md) (§4.4, §4.5, §6.1–§6.5, §7, §8, §9.3–§9.4,
§13, §14 шаги 11–17).
ADR: [2026-11-02 serve mode and cost ladder](../decisions/2026-11-02-serve-mode-and-cost-ladder.md)
(Accepted). Фаза 1: [план](2026-11-02-serve-mode-phase1-plan.md), [руководство](replay.md).

Фаза 3 даёт минимальный стенд: headless-браузер или браузер MCP-сервера, allowlist доменов
задачи, процесс `autobrowser serve` с очередью, бюджетами дня, расписанием, HTTP API, пробой
(ступень 0), исходящим webhook и Docker-образом. Каскад моделей, Telegram и пул worker'ов
остаются в Фазе 4.

---

## 0. Что выяснилось в коде (уточнения к дизайну)

Код сверен с §4.4–§4.5, §6.3–§6.5, §7, §8, §9.4 дизайна. Ниже расхождения и решения по ним.
Все они аддитивны и не меняют решений ADR. Нумерация продолжает U1–U10 Фазы 1.

| # | Наблюдение | Где | Решение в реализации |
| --- | --- | --- | --- |
| U11 | YAML-источник не подставляет `${VAR}`: пример `token: ${AUTOBROWSER_SERVE_TOKEN}` из §7.1 не работает. | `src/config.py:2046` `_YamlFileSettingsSource` | Интерполяцию не добавляю. Токен задаётся через `AUTOBROWSER_SERVE__TOKEN`: если YAML не называет поле, его решает env (глубокое слияние источников). Секрет триггера — `secret` (секрет) **или** `secret_env` (имя переменной окружения, читается при старте; пусто или нет переменной — ошибка старта). Секрет исходящего webhook — `AUTOBROWSER_SERVE__WEBHOOK_OUT__SECRET`. Так секреты не лежат в файле профиля. |
| U12 | `PlaybackRequest` знает только имя записи, которую фабрика грузит из `RecordingStore`. Анонимной записи пробы передать некуда. | `src/contracts.py:424`, `src/harness/playback.py:452` | В `src/contracts.py`: `ProbeStep{tool, args, server=""}` и `PlaybackRequest.steps: tuple[ProbeStep, ...] = ()`. При непустом `steps` `PlaybackFactory.build` строит анонимную `Recording` в памяти: без привязок, без контрольных точек, с пустым каталогом (проверяется только наличие инструмента), `answer="output"`. `recording` тогда — метка для событий (`probe:<job>`), её имя не валидируется как имя файла. `_record_playback` для встроенных шагов `stats.json` не трогает. |
| U13 | Источник шагов существует только при `replay.enabled` (`_step_sources`). | `src/harness/session.py:1022` | Задание с `probe` или `recording` при `replay.enabled=false` — ошибка старта `serve` с понятным текстом. Тихо включать replay за пользователя не буду. |
| U14 | В Фазе 3 ни один канал не умеет подтверждения (`log`, `webhook`), но в §7.3 есть `POST /v1/approvals/{ticket_id}`. | §7.3, §7.5 | Новое поле `serve.api_approvals: bool = false`. `true`: worker получает `ApprovalBroker`, режим прав не превращается в `dont_ask`, каждый тикет уходит в каналы как уведомление `approval` (id тикета, инструмент, причина), ответ — `POST /v1/approvals/{id}` (`via="api"`). `false`: `dont_ask`, эндпоинт отвечает `404`. `default_max_stage: human` имеет смысл только с `api_approvals`. |
| U15 | `start_chrome_cdp(path, dir, port)` внедряется с этой сигнатурой, фейки в 15+ тестах трёхаргументные. | `src/harness/chrome.py:19`, `session.py:608` | Сигнатура внедряемого колбэка не меняется. Боевой `start_chrome_cdp` получает keyword `extra_args: Sequence[str] \| None = None`; `None` — флаги из `get_settings().browser` через чистую `chrome_flags(BrowserSettings)` (`--headless=new` при `headless`, затем `extra_args`). `launch: external` решается в `SessionContext.initialize`: ни запуска Chrome, ни `wait_for_port`. |
| U16 | При `launch: external` дефолтный `mcp_servers` (Playwright через `--cdp-endpoint {cdp_endpoint}`) подключится к пустому порту. | `src/harness/mcp_setup.py:40-48` | Ошибка старта, name-free: при `external` сервер, в `command`/`args`/`url`/`env` которого остался `{cdp_port}` или `{cdp_endpoint}`, — «server X still connects over CDP; configure it to launch its own browser». Проверка — в `mcp_setup` по плейсхолдерам, а не по имени сервера. `doctor` печатает режим запуска. |
| U17 | `PermissionCheck` строит engine в `_authorize`, профиль задачи у `TurnController` уже есть (`self._profile`). | `loop.py:351`, `loop.py:940` | `PermissionCheck.allowed_domains: tuple[str, ...] = ()` заполняется из `profile.allowed_domains`; `PermissionSource += "task_scope"`. Проверка встаёт между deny-правилами и режимом `read_only` (§6.4). Совпадение домена — как у правил (`_in_domains`: равен или поддомен). Без разрешённого домена deny получает только не-read-only вызов; это касается и не-браузерных инструментов без URL (например, `memory_write`) — fail closed, записано в руководстве. |
| U18 | `RunReport.playback` (`replayed`, `retargeted`, `diverged_at`) и `recording_updated` нельзя получить из `TaskOutcome`. | §6.5, `src/ui/views.py:88` | `TaskOutcome` не расширяю. `src/serve/tally.py` — `RunTally`, `EventSink` стенда (импортирует только `src.agent_loop.events`); композиция передаёт его в `build_session(event_sinks=...)`. Он считает по `task_id` события `playback.started/step/diverged` и `recording.saved`. |
| U19 | `SessionController` не отдаёт каталог инструментов и не знает, есть ли запись. А `serve` должен проверить `probe.steps` при старте (§7.1) и выбрать «есть запись → playback, нет → агент» (§6.1). | `src/ui/controller.py` | Два аддитивных метода контроллера (не в `ControllerPort`): `async tool_names() -> tuple[str, ...]` и `has_recording(name) -> bool`. Проверка `probe.steps` идёт после `controller.start()`, когда MCP уже поднят. |
| U20 | V2 (пул worker'ов) — Фаза 4. Поле `workers` и `JobSpec.worker` в Фазе 3 ничего бы не делали. То же для `telegram`. | §9.4 | Не добавляю `workers`, `JobSpec.worker`, `serve.telegram`, триггер `telegram`, канал `telegram`: они появятся в Фазе 4 вместе с поведением. `profiles_dir` и `cdp_port_base` добавляю: worker `w0` берёт `profiles_dir/w0` и `cdp_port_base`. Секций станет 17 (`serve`); `cascade` — восемнадцатая, в Фазе 4. |
| U21 | На Windows у Python нет базы часовых поясов (`zoneinfo.available_timezones()` здесь пуст), `tzdata` не в зависимостях. | проверено локально | Без новой зависимости: `serve.timezone=""` — системный пояс; IANA-имя проверяется при загрузке настроек, а ошибка подсказывает поставить `tzdata`. В Docker-образе `tzdata` есть (apt). Тесты DST — на синтетическом `tzinfo` с фиксированными переходами плюс тест на реальном IANA-поясе, пропускаемый без базы. См. вопрос P1. |
| U22 | `EventType` — `Literal`; событий `serve.*` в нём нет. | `src/agent_loop/events.py:14` | Добавляю `serve.started`, `serve.stopped`, `serve.trigger.fired`, `serve.run.queued`, `serve.run.skipped`, `serve.run.finished`, `serve.notified`, `serve.notify.failed`, `serve.auth.rejected`. Пишет их свой `EventEmitter` в `<home>/serve/events.jsonl` (`JsonlEventSink`, то же редактирование), `session_id = serve-<hex8>` на процесс. |
| U23 | `bootstrap.py` уже ~400 строк; `serve` — отдельная композиция. | `src/cli/bootstrap.py` | `run_serve_cli` живёт в новом `src/cli/serve.py` и переиспользует `build_session`. Подкоманда `serve` импортирует его лениво. Аргументы сессии — `build_parser().parse_args([])` плюс поля worker'а (`user_data_dir`, `cdp_port`, `labels={"mode": "serve", "worker": "w0"}`, `interactive=api_approvals`). |
| U24 | Бюджет прогона — `min(задание, остаток дня)` (§6.2). Но `daily_max_runs` считает прогоны, а не вызовы модели. | §6.2 | `min` берётся по токенам и деньгам; вызовы модели — из бюджета задания или `default_run_budget`. Остаток ≤ 0 по любому дневному лимиту — `skipped{reason="budget"}`. |
| U25 | Свободные задачи (`POST /v1/tasks`) не имеют домена задания, а N5 требует allowlist у каждого задания. | §7.3, N5 | Поле `serve.free_task_domains: list[str] = []` (пусто — без ограничения, это видно в `/v1/status`). Свободная задача идёт с `default_run_budget`, `default_max_stage` и `carry_context=False`. См. вопрос P2. |
| U26 | Порог частоты cron (`min_interval_seconds`) нельзя честно проверить при валидации без выборки «на N срабатываний вперёд» (магическое число). | §9.4 | `interval.every_seconds` проверяется при валидации. Для cron действует ограничение в рантайме: `Scheduler` не срабатывает триггером раньше `min_interval_seconds` после его прошлого срабатывания (`skipped{reason="min_interval"}`). |
| U27 | `uvicorn` по умолчанию пишет access log с адресами клиентов. | §8, `serve.log_client_addresses` | `access_log=False`, `log_level="warning"`; адреса появляются только в событиях и только при `log_client_addresses`. Сигналы ловит `uvicorn` (SIGINT/SIGTERM, на Windows Ctrl+C); после выхода `serve()` стенд останавливается по §7.2. |

Принятые ответы Q1–Q15 действуют без изменений. Для Фазы 3 важны Q1 (`croniter>=6.2,<7`), Q3
(проба через engine), Q5 (окно подтверждения — через `loop.progress_timeout_seconds` профиля
стенда), Q7 (тело webhook не попадает в задачу), Q10 (`bypass` запрещён), Q12 (свободные задачи
выключены), Q14 (пересоздание сессии по числу прогонов).

### Вопросы к вам (дефолт — в скобках, по нему и пойду без ответа)

- **P1.** Добавлять ли `tzdata` как зависимость, чтобы IANA-пояса работали на Windows из коробки?
  Это нарушит правило «только `croniter`». *(Нет: системный пояс по умолчанию, подсказка в
  ошибке, `tzdata` в Docker-образе.)*
- **P2.** Нужен ли `serve.free_task_domains` (U25) или свободные задачи без allowlist допустимы,
  раз они и так выключены? *(Нужен: поле дешёвое, а N5 иначе не выполняется.)*
- **P3.** Подтверждения через API в Фазе 3 (U14) — делать или отложить всю ступень 4 стенда до
  Telegram? *(Делать: эндпоинт уже в дизайне, без поля он мёртвый.)*

---

## 1. Правила партии

- Один шаг — один коммит (скилл `commit`, английский, scope из `.claude/conventions/commit.md`;
  новый scope `serve`). Тесты — в том же коммите. Перед коммитом гоняются тесты шага, команды
  соединяются `&&` (`uv run pytest <файлы шага> && git commit ...`): коммит с падающим тестом
  не уходит. Полный `uv run pytest` и оба прогона evals — один раз в конце (§3).
- Новое поведение выключено: `browser.launch=cdp`, `headless=false`, `extra_args=[]`,
  `allowed_domains=()`; `serve` — отдельная команда, без неё ничего не запускается.
  Интерактивный режим и baseline `agent_loop_v2` не меняются.
- Границы: `src/serve/**` не импортирует `src.agent_loop.execution`, `src.browser`, `src.cli`,
  `src.providers`, `src.harness`. Разрешены `src.ui.controller`, `src.ui.views`, `src.ui.feed`, `src.contracts`,
  `src.config`, `src.agent_loop.events`, `src.agent_loop.recordings` (имя записи). Обратно —
  `src/agent_loop/**`, `src/harness/**`, `src/ui/**`, `src/browser/**` не импортируют
  `src.serve`. Ловит `tests/test_serve_boundaries.py`. Цепочка
  `SessionRuntime → GoalRunner → Agent → AgentLoopEngine` не меняется.
- Name-free: имена в `probe.steps` — данные пользователя. Аргументы Playwright
  (`--headless`, `--user-data-dir`, `--allowed-origins`) — только в `config.example.yaml` и
  `compose`-примере. Проверка `external` (U16) — по плейсхолдерам, не по имени сервера.
- Тюнаблы: каждое поле — `src/config.py` + `.env.example` + `config.example.yaml` +
  `tests/test_config.py`. Константы — только лексика (имена файлов, заголовки, префиксы).
- Безопасность (§8): bind `127.0.0.1`; нелокальный `host` — только при `allow_remote: true` и
  непустом токене; `hmac.compare_digest`; токены не попадают в логи, события и `doctor`;
  `free_tasks=false`; тело webhook не читается в задачу; без `api_approvals` — `dont_ask`;
  `permissions.mode: bypass` — ошибка старта.
- Зависимость — только `croniter>=6.2,<7` (шаг 14). `uv add`, затем
  `uv export --format requirements.txt --no-hashes --no-annotate --no-header --no-emit-project
  -o requirements.txt`.
- Не пушу и не мержу.

---

## 2. Шаги

### Шаг 11. Браузер для сервера

**Коммит:** `feat(harness): launch Chrome headless or leave the browser to the MCP server`

`src/config.py`, `BrowserSettings` (§9.3):
- `launch: Literal["cdp", "external"] = "cdp"`;
- `headless: bool = False` — `--headless=new` при `cdp`;
- `extra_args: list[str] = []` — дополнительные аргументы Chrome при `cdp`.

`src/harness/chrome.py`:
- `chrome_flags(settings: BrowserSettings) -> list[str]` — чистая функция;
- `start_chrome_cdp(chrome_path, user_data_dir, port, *, extra_args: Sequence[str] | None = None)`
  — `None` берёт `chrome_flags(get_settings().browser)` (U15).

`src/harness/session.py::SessionContext.initialize`: при `launch == "external"` нет
`start_chrome_cdp` и `wait_for_port`; `chrome_process = None`; MCP стартует сразу.

`src/harness/mcp_setup.py`: `cdp_placeholder_servers(servers) -> list[str]` — серверы, в
конфигурации которых остались `{cdp_port}`/`{cdp_endpoint}`; при `external` непустой список —
`RuntimeError` до запуска MCP (U16).

`src/cli/subcommands.py::_doctor`: строка `browser launch: cdp|external (headless)`; при
`external` Chrome и порт не проверяются.

`config.example.yaml`: закомментированный пример `external` с
`mcp_servers.playwright.args: [-y, "@playwright/mcp@<версия>", --headless, --browser, chromium,
--user-data-dir, /data/serve/profiles/w0]`.

**Тесты:** `tests/test_main_cli.py` (флаги `--headless=new` и `extra_args` в командной строке
Chrome; порядок; без флагов — как сейчас), `tests/test_harness_session.py` (`external` не
вызывает ни запуск, ни ожидание порта; сервер с `{cdp_endpoint}` при `external` — ошибка до
MCP; `cdp` по умолчанию — как сейчас), `tests/test_config.py`.

### Шаг 12. Allowlist доменов задачи

**Коммит:** `feat(permissions): deny calls outside the task's allowed domains`

`carry_context=False` уже сделан в Фазе 1 (`session.py:454`); здесь только тест, что он
сочетается с allowlist.

- `src/contracts.py`: `PermissionCheck.allowed_domains: tuple[str, ...] = ()`;
  `PermissionSource += "task_scope"`.
- `loop.py::_authorize`: `allowed_domains=self._profile.allowed_domains`.
- `src/harness/permissions.py::_evaluate` (U17), после deny-правил и до `read_only`:
  - домен разрешён и не входит в список → `deny`, `source="task_scope"`, причина без
    аргументов: «Not executed: <domain> is outside the domains this task may use.»;
  - домен не разрешён и вызов не read-only → `deny`, `task_scope`;
  - иначе — дальше по обычному порядку. Hook этот deny не снимает (он после hooks).
- `RunProfile.allowed_domains` нормализуется `normalize_domain` там, где его строит клиент
  (`serve`, шаг 13); engine принимает уже нормализованные значения.
- `docs/development/permissions.md`: абзац о task scope.

**Тесты:** `tests/test_permissions.py` — внутри, поддомен, вне списка, без домена read-only
(разрешён) и state-changing (запрет), deny-правило по-прежнему первое, `allow`-правило не
снимает task scope, `bypass` и грант тоже. `tests/test_agent_loop_permissions.py` — профиль
доходит до проверки, deny не терминален, модель читает причину, `permission.decided{source:
"task_scope"}`. `tests/test_harness_session.py` — `carry_context=False` + `allowed_domains`.

### Шаг 13a. Секция `serve`, ledger и состояние заданий

**Коммит:** `feat(serve): add the serve settings, the run ledger and job state`

`src/config.py` — `ServeSettings` (17-я секция) по §9.4 с поправками U14, U20, U25:

| Поле | Тип | Дефолт | Границы |
| --- | --- | --- | --- |
| `host` | str | `127.0.0.1` | `min_length=1` |
| `port` | int | `8765` | `1..65535` |
| `token` | `SecretStr \| None` | `None` | — |
| `allow_remote` | bool | `false` | — |
| `dir` | str | `serve` | `min_length=1`; относительно `storage.root_dir` |
| `profiles_dir` | str | `serve/profiles` | `min_length=1` |
| `cdp_port_base` | int | `9300` | `1..65535` |
| `queue_size` | int | `20` | `ge=1` |
| `coalesce` | `drop \| queue_one` | `drop` | — |
| `cooldown_seconds` | float | `0.0` | `ge=0` |
| `min_interval_seconds` | float | `60.0` | `ge=1` |
| `catch_up` | `none \| one` | `none` | — |
| `timezone` | str | `""` | IANA или пусто (U21) |
| `daily_max_runs` | int \| None | `None` | `ge=1` |
| `daily_max_tokens` | int \| None | `None` | `ge=1` |
| `daily_max_cost` | float \| None | `None` | `gt=0` |
| `default_run_budget` | `RunBudgetSpec` | `{max_model_calls: 30}` | `max_model_calls` обязателен |
| `default_max_stage` | `LadderStage` | `agent` | — |
| `session_max_runs` | int | `200` | `ge=1` |
| `shutdown_grace_seconds` | float | `60.0` | `gt=0` |
| `rate_limit_per_minute` | int | `60` | `ge=1` |
| `max_body_bytes` | int | `65536` | `ge=1024` |
| `idempotency_window_seconds` | float | `3600.0` | `ge=0` |
| `free_tasks` | bool | `false` | — |
| `free_task_domains` | list[str] | `[]` | `normalize_domain` (U25) |
| `api_approvals` | bool | `false` | — (U14) |
| `log_client_addresses` | bool | `false` | — |
| `channels` | list[`log \| webhook`] | `[log]` | — (U20) |
| `notify_max_chars` | int | `1500` | `ge=100` |
| `webhook_out` | `WebhookOutSettings \| None` | `None` | `{url, secret, timeout_seconds=10.0 (gt=0)}` |
| `jobs` | dict[str, `JobSpec`] | `{}` | имя — как у записи |

Подмодели (`extra="forbid"`):
- `RunBudgetSpec{max_model_calls, max_tokens, max_cost}` → `to_budget() -> RunBudget`;
- `JobSpec{task, enabled=True, triggers, probe=None, recording=None, record_on_success=False,
  max_stage=None, budget=None, domains=[], notify_on=[change, failure, blocked, budget],
  coalesce=None, cooldown_seconds=None}`; `None` — дефолт секции;
- `TriggerSpec{id, type: interval | webhook, every_seconds=None, secret=None, secret_env=None,
  auth: bearer | hmac_sha256 = bearer}` (`cron` — шаг 14); валидатор: `interval` требует
  `every_seconds >= min_interval_seconds` (проверка на уровне секции), `webhook` — ровно
  один из `secret`/`secret_env`; `id` уникален в задании;
- `ProbeSpec{steps: list[ProbeStepSpec{server="", tool, args={}}], select=None, ignore=[]}` —
  регулярные выражения компилируются при валидации.

Валидаторы секции:
- нелокальный `host` без `allow_remote` или без токена — ошибка (`127.0.0.1`, `::1`,
  `localhost` — локальные);
- `domains` и `free_task_domains` нормализуются `normalize_domain`;
- `recording` — по `validate_recording_name` (лексика формата, импорт `src.agent_loop.recordings`
  в config недопустим, поэтому regex имени выносится в `src/contracts.py` как
  `RECORDING_NAME_PATTERN` и используется обоими).

`src/serve/` (новый пакет):
- `clock.py` — `Clock` (протокол: `now() -> datetime` aware, `async sleep(seconds)`),
  `SystemClock(tz)`; `resolve_timezone(name) -> tzinfo` (U21);
- `reports.py` — `RunReport`, `TaskSummary`, `RunState = Literal["queued", "coalesced",
  "skipped"]`, `to_json()/from_json()` (§6.5);
- `ledger.py` — `Ledger(path, tz)`: `append(report)`, `day_totals(day) -> DayTotals(runs,
  tokens, cost)` (восстанавливается из файла при старте), `find(run_id)`, поломанные строки
  пропускаются с предупреждением;
- `state.py` — `JobState{last_finished_at, last_fired: dict[trigger, datetime], last_digest,
  last_region, counters: dict[str, int]}` и `JobStateStore(dir)` — атомарная запись
  `<serve.dir>/jobs/<id>/state.json`; `ServeState{budget_notified_day}` в `<serve.dir>/state.json`.

`tests/test_serve_boundaries.py` — ограждения импорта (§1) по AST всех файлов пакетов.

**Тесты:** `tests/test_config.py` (секция, дефолты, границы, `extra="forbid"` подмоделей,
нелокальный host, `secret`/`secret_env`, regex пробы, нормализация доменов, примеры),
`tests/serve/test_ledger.py` (roundtrip, итоги дня в поясе, смена дня, повреждённая строка,
`find`), `tests/serve/test_state.py`, `tests/test_serve_boundaries.py`.

### Шаг 13b. `Dispatcher`, `LogChannel` и команда `autobrowser serve`

**Коммит:** `feat(serve): run configured jobs through a queue with day budgets`

`src/serve/worker.py`:
- `WorkerPort` (протокол над `SessionController`): `start`, `run_outcome(text, *, profile)`,
  `cancel`, `reset`, `reconnect(server)`, `session_view`, `tool_names`, `has_recording`,
  `shutdown(timeout)`, `answer(ticket, answer, *, via)`;
- `ensure_healthy(worker)` — перед прогоном: сервер в `failed`/`disconnected` → `reconnect`, при
  неудаче `reset` (§7.2).

`src/serve/profiles.py` — чистые функции:
- `main_profile(job, settings, *, day_left, labels, has_recording) -> RunProfile`
  (`carry_context=False`, `max_stage`, бюджет по U24, `allowed_domains`, `playback` при
  существующей записи, `record_as` при `record_on_success`);
- `free_task_profile(settings, labels) -> RunProfile`.

`src/serve/tally.py` — `RunTally` (U18).

`src/serve/notify.py` — `NotifyKind`, `Notification` (тело санитизируется тем же
`sanitize_text` из `src.ui.feed`, режется до
`notify_max_chars`), `NotificationChannel` (протокол: `name`, `async send(item)`), `LogChannel`
(событие `serve.notified` и строка в stdout), `Notifier` (рассылает по `channels`, сбой канала —
`serve.notify.failed`, никогда не валит прогон).

`src/serve/dispatcher.py` — `Dispatcher(settings, worker, ledger, states, notifier, events,
tally, clock)`:
- `fire(job_id, trigger, *, idempotency_key=None) -> FireResult(run_id, state, reason)` —
  синхронно: задание выключено, `coalesce` (`drop` | `queue_one`), `cooldown`, дневные лимиты,
  `queue_full`; каждое решение — `serve.trigger.fired` и `serve.run.queued` /
  `serve.run.skipped{reason}`; пропуски — в `JobState.counters`;
- `async run()` — один прогон за раз (V1): `ensure_healthy` → основная задача
  (`run_outcome`) → `RunReport` → `ledger.append` → `serve.run.finished` → уведомления по
  `notify_on` → `JobState`; одно уведомление `budget` в день (`ServeState`);
  `session_max_runs` → `worker.reset()` между прогонами;
- `async stop(grace)` — §7.2: новые срабатывания отклоняются, очередь — `skipped{shutdown}`,
  работающему прогону `grace`, затем `cancel`;
- `status() -> Mapping` — для `/v1/status` (без секретов);
- `report(run_id) -> RunReport | None`.

`src/cli/serve.py` — `run_serve_cli(args) -> int` (U23):
- проверки старта: `permissions.mode == "bypass"` — ошибка; `probe`/`recording` без
  `replay.enabled` (U13); `max_cost` (в задании, `default_run_budget` или дневной) без цены
  модели в `llm.prices` — ошибка (§6.2);
- `build_session` для `w0`: `user_data_dir = <root>/<profiles_dir>/w0`, `cdp_port =
  cdp_port_base`, `interactive = api_approvals`, `event_sinks = (tally,)` (+ брокер при
  `api_approvals`); без `api_approvals` режим `default` → `dont_ask` (существующий
  `effective_permission_mode`);
- `controller.start()` → проверка `probe.steps` по `tool_names()` (U19);
- свой `EventEmitter` → `<serve.dir>/events.jsonl` (U22), `serve.started{jobs, host, port}` —
  без токена;
- на одном asyncio-цикле: `Dispatcher.run()`; в этом шаге триггеров ещё нет, процесс ждёт
  Ctrl+C;
- `--check`: проверить конфигурацию без запуска браузера и выйти (`0` / `2`).

`src/cli/subcommands.py`: `serve` в `SUBCOMMANDS`, ленивый импорт `src.cli.serve`.
`src/ui/controller.py`: `tool_names()`, `has_recording(name)` (U19).
`src/agent_loop/events.py`: `EventType += serve.*` (U22).

**Тесты:** `tests/serve/test_dispatcher.py` на фейковом worker'е и фейковых часах:
- `drop` и `queue_one`, cooldown, `queue_full`, выключенное задание;
- дневной бюджет: исчерпан → skip, смена дня сбрасывает, итоги восстанавливаются из ledger,
  уведомление `budget` одно в день и после перезапуска;
- профиль: `carry_context=False`, домены, `min(задание, остаток дня)`, запись есть / нет,
  `record_as`;
- сбой задачи → `failed` в отчёте, уведомление `failure`; `blocked` → `blocked`;
- `session_max_runs` → `reset`; упавший сервер → `reconnect`, затем `reset`;
- остановка: очередь `shutdown`, grace, затем `cancel`.

`tests/serve/test_notify.py` (санитизация, лимит, сбой канала не валит прогон),
`tests/serve/test_profiles.py`, `tests/test_cli_entry.py` (`serve --check`: `bypass`, replay
выключен, нет цены, нелокальный host без токена — код `2` и текст; токен не печатается),
`tests/test_ui_controller.py` (`tool_names`, `has_recording`).

### Шаг 14. `Scheduler`: interval и cron

**Коммит:** `feat(serve): fire jobs on interval and cron schedules`

- Зависимость `croniter>=6.2,<7` (Q1): `uv add`, `uv.lock`, `requirements.txt` через
  `uv export` (§1).
- `src/config.py`: `TriggerSpec.type += "cron"`, `schedule: str | None`; валидатор —
  `croniter.is_valid(schedule)` (ленивый импорт внутри валидатора).
- `src/serve/scheduler.py` — `Scheduler(jobs, settings, fire, clock, states)`:
  - `next_fire(trigger, after) -> datetime` — `interval`: `last + every`; `cron`:
    `croniter(schedule, after_local).get_next(datetime)` в `serve.timezone`;
  - `async run()` — спит до ближайшего срабатывания, вызывает `Dispatcher.fire`, хранит
    `JobState.last_fired[trigger]`;
  - `catch_up`: `none` — пропущенное за простой не догоняется; `one` — один прогон, если
    прошлое расписанное время позже `last_fired`;
  - U26: срабатывание раньше `min_interval_seconds` после прошлого — `skipped{min_interval}`;
  - `stop()`.
- `run_serve_cli` запускает `Scheduler` рядом с `Dispatcher`.

**Тесты:** `tests/serve/test_scheduler.py` на фейковых часах: interval, cron, `catch_up`
none/one, перезапуск не повторяет срабатывание, `min_interval`, DST — синтетический `tzinfo`
(переход вперёд и назад: нет двойного и пропущенного срабатывания в пределах
семантики `croniter`) и реальный IANA-пояс (пропуск без базы, U21), `stop`.
`tests/test_config.py` (неверный cron).

### Шаг 15. HTTP API и webhook

**Коммит:** `feat(serve): expose the job API and webhook triggers over HTTP`

`src/serve/auth.py`:
- `bearer_ok(header, expected: SecretStr | None) -> bool` — `hmac.compare_digest`; пустой
  ожидаемый токен — всегда `False` для `/v1/*`;
- `signature_ok(body, header, secret) -> bool` — `X-AutoBrowser-Signature: sha256=<hex>`
  (HMAC-SHA256 тела, `compare_digest`);
- `RateLimiter(per_minute, clock)` — скользящее окно по ключу: токен API, триггер webhook,
  а неудачная авторизация — по адресу клиента (в памяти, в события не пишется);
- `IdempotencyCache(window, clock)` — `(path, key) -> FireResult`.

`src/serve/app.py` — `build_app(api: ServeApi, settings) -> Starlette`:

| Путь | Доступ | Ответ |
| --- | --- | --- |
| `GET /healthz` | без токена | `{"status": "ok"}` |
| `GET /v1/status` | bearer | `Dispatcher.status()` + день против бюджета + открытые тикеты (при `api_approvals`) |
| `POST /v1/jobs/{job_id}/runs` | bearer | `202 {run_id, state}`; неизвестное задание — `404` |
| `POST /v1/hooks/{job_id}/{trigger_id}` | секрет триггера | `202 {run_id, state}`; тело читается только для лимита и подписи |
| `GET /v1/runs/{run_id}` | bearer | `RunReport` или `404` |
| `POST /v1/approvals/{ticket_id}` | bearer + `api_approvals` | `{"answer": once\|session\|deny}` → `worker.answer(..., via="api")`; иначе `404` |
| `POST /v1/tasks` | bearer + `free_tasks` | `{"task": str}` → прогон со свободным профилем (U25); иначе `404` |

- Лимит тела: `Content-Length > max_body_bytes` → `413` сразу; поток читается не дальше
  `max_body_bytes + 1`.
- `401` без тела и `serve.auth.rejected{path, reason}`; адрес — только при
  `log_client_addresses`; `429` при лимите частоты.
- `Idempotency-Key` для `runs` и `hooks`.
- Тело webhook в задачу не попадает (Q7): `Dispatcher.fire` не получает тела вообще.
- `run_serve_cli`: `uvicorn.Server(Config(app, host, port, access_log=False,
  log_level="warning"))` на том же цикле (U27); выход `serve()` запускает остановку §7.2.

**Тесты:** `tests/serve/test_app.py` (Starlette `TestClient`, фейковый `Dispatcher`):
- `/healthz` без токена; `/v1/*` без токена, с неверным, с пустым настроенным — `401`;
- токен не встречается ни в одном событии и ответе;
- bearer и HMAC webhook: верная, неверная, без подписи; `secret_env`;
- rate limit → `429`, окно сдвигается по фейковым часам;
- `Idempotency-Key` повтор → тот же `run_id`; после окна — новый;
- `413` по заголовку и по потоку без заголовка;
- тело webhook с текстом «ignore previous instructions» не появляется ни в задаче, ни в профиле;
- `free_tasks=false` → `404`; `true` → свободный профиль с доменами;
- `/v1/approvals` без `api_approvals` → `404`, с ним → `answer(via="api")`.

`tests/test_cli_entry.py` — нелокальный host без `allow_remote`/токена — ошибка старта.

### Шаг 16. Проба (ступень 0)

**Коммит:** `feat(serve): skip unchanged runs with a model-free probe`

- `src/contracts.py`: `ProbeStep`, `PlaybackRequest.steps` (U12).
- `src/harness/playback.py`: `PlaybackFactory.build` при непустом `steps` строит анонимную
  `Recording` (U12); `src/harness/session.py::_record_playback` — без `stats.json` для неё.
- `src/serve/profiles.py`: `probe_profile(job, labels) -> RunProfile` —
  `PlaybackRequest(recording=f"probe:{job}", steps=…, answer="output", select, ignore)`,
  `max_stage="playback"`, `carry_context=False`, `allowed_domains=domains`.
- `src/serve/dispatcher.py`: прогон с пробой (§6.1):
  - проба не `completed` → `failed`, уведомление `failure`, основной задачи нет;
  - `sha256(final_answer)` == `JobState.last_digest` → `unchanged`, 0 вызовов модели, без
    уведомления;
  - иначе — основная задача; уведомление `change` с «было / стало» (`last_region`,
    обрезка до `notify_max_chars`); первая проба — всегда изменение;
  - `RunReport.tasks` — обе задачи (`kind: probe | main`), `changed`.

**Тесты:** `tests/test_playback_source.py` (встроенные шаги: без каталога, отсутствующий
инструмент — `catalog`, регион, пустой регион), `tests/serve/test_probe.py` — без изменений:
0 вызовов модели, основной задачи нет, уведомления нет; изменилось: основная задача и `change`;
проба упала: `failed`; первая проба. Интеграция: `tests/serve/test_probe_integration.py` —
настоящая сессия на `tests/mcp_fixtures/fake_page_server.py`, модель — `FakeChatModel`, которая падает при вызове: два тика без изменений → 0 вызовов модели.

### Шаг 17. `WebhookOutChannel`, Docker, руководство, диаграммы

**Коммит 17a:** `feat(serve): notify an outgoing webhook with signed JSON`
- `src/serve/notify.py`: `WebhookOutChannel(settings, client)` — POST JSON уведомления на
  `webhook_out.url`, `X-AutoBrowser-Signature: sha256=<hex>`, `timeout_seconds`, без
  повторов; при `api_approvals` уведомления `approval` несут id тикета.
- `run_serve_cli`: канал `webhook` без `webhook_out.url` — ошибка старта.
- **Тесты:** `tests/serve/test_webhook_out.py` на `httpx.MockTransport` — подпись, тайм-аут
  и `5xx` → `serve.notify.failed`, секрет не в событиях.

**Коммит 17b:** `chore(serve): add the Docker image and compose example`
- `Dockerfile`: `python:3.12-slim`; Node.js и `tzdata` из apt; `@playwright/mcp` с
  закреплённой версией и `npx playwright install --with-deps chromium`; AutoBrowser —
  `uv build` + `uv tool install` из wheel; пользователь не root; `AUTOBROWSER_HOME=/data`,
  `AUTOBROWSER_CONFIG_FILE=/data/serve.yaml`; `HEALTHCHECK` на `/healthz`.
- `compose.yaml`: том `/data`, `ports: ["127.0.0.1:8765:8765"]`, токен из `.env`.
- `docker/serve.example.yaml`: `browser.launch: external`, `mcp_servers.playwright` с
  `--headless --browser chromium --user-data-dir /data/serve/profiles/w0`, `serve.host:
  0.0.0.0`, `allow_remote: true`, одно задание с пробой.
- `.dockerignore`.
- **Тесты:** `tests/pipeline/test_docker_smoke.py` (`-m live`, пропуск без `docker`): сборка,
  `/healthz`, один прогон задания по `POST /v1/jobs/.../runs` до `RunReport`.

**Коммит 17c:** `docs(serve): document the stand, its API and Docker image`
- `docs/development/serve-mode.md` — руководство: включить, задания, триггеры, проба,
  бюджеты, API, безопасность, подтверждения через API, Docker, отладка (`serve/events.jsonl`,
  `ledger.jsonl`).
- `docs/diagrams/serve-mode.md` — из «дизайна» в «код»: один worker, без Telegram;
  `docs/diagrams/session-runtime-sequence.md` (`carry_context`, allowlist),
  `harness-boundaries.md` (`src/serve` как клиент), `index.md`.
- `docs/development/permissions.md` (если не сделано в шаге 12), `docs/glossary.md` (прогон,
  проба, стенд).
- `CLAUDE.md`, `AGENTS.md`: 17 секций, `autobrowser serve`, границы `src/serve`, команды
  тестов `tests/serve`.
- Статус дизайна: «Фаза 3 реализована», ссылка на этот план; статус плана — Implemented.

---

## 3. Проверка в конце партии

1. `uv run pytest` — полный набор.
2. `uv run scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v2.json --memory-seed
   tests\evals\memory_seed` — baseline без изменений.
3. `uv run scripts/run_evals.py --scenarios tests\evals\playback_scenarios --baseline
   tests\evals\baselines\playback_v1.json` — совпадает.
4. `uv run main.py --no-mcp --task "inspect page"` — интерактивный путь не изменился.
5. `uv run main.py serve --check` с примером профиля — дымовая проверка конфигурации.
6. `uv run pytest -m live tests/pipeline/test_docker_smoke.py` — если Docker доступен
   (здесь `27.5.1` есть); результат — в отчёт, не в gate.
7. `code-reviewer` один раз над всей партией. Подтверждённые замечания — отдельный коммит
   `fix(...)`; о неисправленных — с причиной. Повтор review — только после нетривиальных
   исправлений.
8. Отчёт: коммиты, результаты 1–6, замечания review, что осталось на вас.

---

## 4. Что не входит в Фазу 3

- Каскад моделей, `cascade`, `tier_escalation` — Фаза 4 (шаг 18).
- Telegram, `serve.telegram`, триггер и канал `telegram` — Фаза 4 (шаг 19).
- Пул worker'ов, `serve.workers`, `JobSpec.worker`, блокировки L4 — Фаза 4 (шаг 20, Q13).
- Параметры webhook в задаче — не в v1 (Q7).
- Веб-UI стенда, задания через API — вне скоупа (§2.3).
- Публикация образа — после решения по LICENSE (§2.4); образ собирается локально.
- Правка `PLAYBACK_RETARGET_*`, строка `playback_v1` в `gate.yml`, merge и push — за вами.

---

## 5. Риски плана

| Риск | Смягчение |
| --- | --- |
| Шаг 13 большой (секция + ledger + dispatcher + CLI). | Заранее разделён на 13a и 13b; 17 — на 17a–17c. Если 13b выйдет больше ~800 строк, `src/cli/serve.py` уйдёт отдельным коммитом. |
| `croniter` и DST на `zoneinfo`/синтетическом `tzinfo` ведут себя не так, как ждут тесты. | Тест фиксирует наблюдаемое поведение `croniter` (без двойного срабатывания), а не идеал; расхождение — в руководство. Реальный IANA-тест в Docker-smoke. |
| `uvicorn` и сессия на одном цикле: долгая синхронная работа сессии блокирует API. | Сессия асинхронна (как в print mode); тяжёлые места (`to_thread`) уже есть. Тест API идёт на фейковом диспетчере; smoke в Docker — на настоящем. |
| `GoalRunner` watchdog обрывает задачу, пока тикет ждёт ответа через API. | Q5: окно ответа = `progress_timeout × approval_timeout_ratio`; руководство советует поднять `loop.progress_timeout_seconds` в профиле стенда. |
| Allowlist ломает инструменты без URL (state-changing, без домена). | Fail closed по дизайну §6.4; поведение описано в руководстве; тест фиксирует. |
| Образ тяжёлый, `npx playwright install --with-deps` меняется между версиями. | Версия `@playwright/mcp` закреплена в `Dockerfile`; smoke — `-m live`, вне gate. |

---

## 6. Review Focus

Входы, которые дизайн подразумевает, но легко упустить. Каждый закрыт тестом в своём шаге.

1. **Токен и секреты в любом выводе** — ни в `serve/events.jsonl`, ни в ответах API, ни в
   `serve --check`/`doctor`, ни в `RunReport` (шаги 13b, 15, 17a: поиск значения токена в
   записанных событиях и выводе).
2. **Пустой токен при локальном host** — `/v1/*` должен отвечать `401`, а не пускать всех
   (шаг 15).
3. **Повторное срабатывание во время работающего прогона того же задания** — coalescing, а не
   второй параллельный прогон и не потерянное срабатывание при `queue_one` (шаг 13b).
4. **Перезапуск процесса в середине дня** — дневные итоги и «одно уведомление о бюджете»
   переживают рестарт; cron не стреляет повторно за уже отработанный слот (шаги 13b, 14).
5. **Webhook с огромным телом без `Content-Length`** (chunked) — `413` без чтения всего тела
   в память (шаг 15).
