# План реализации Фазы 4: каскад моделей, Telegram, пул worker'ов

Статус: Approved (2026-11-09) — план одобрен, вопросы P4–P11 приняты по дефолтам (§0.2).
Дата: 2026-11-08
Ветка: `feat/serve-phase4` (от `feat/serve-mode`)
Системный дизайн: [Фаза 4](2026-11-08-serve-mode-phase4-design.md) — источник контрактов, потоков,
настроек и тестов этого плана.
Дизайн: [serve mode design](2026-11-02-serve-mode-design.md) (F15, §3.4, §4.3, §7.1, §7.5, §9.2,
§9.4, §10.1, Q5, Q11, Q13, §14 шаги 18–20).
ADR: [2026-11-02 serve mode and cost ladder](../decisions/2026-11-02-serve-mode-and-cost-ladder.md)
(Accepted). Предыдущие фазы: [план Фазы 1](2026-11-02-serve-mode-phase1-plan.md),
[план Фазы 3](2026-11-02-serve-mode-phase3-plan.md), руководства [replay.md](replay.md),
[serve-mode.md](serve-mode.md).

Фаза 4 добавляет три независимые вещи, каждая выключена по умолчанию:
1. каскад моделей (шаг 18) — в обоих режимах;
2. канал Telegram (шаг 19) — уведомления, команды, подтверждения кнопками, long polling;
3. пул worker'ов (шаг 20) — до 8 сессий параллельно, закрепление заданий.

---

## 0. Что выяснилось в коде (уточнения к дизайну)

### 0.1 Расхождения U28–U63

Полная таблица с `файл:строка` — в §10 системного дизайна. Ниже — решение одной строкой и шаг,
в котором оно реализуется. Ключевые утверждения перепроверены по коду при составлении плана
(`loop.py:376`, `loop.py:1473`, `model.py:71`, `contracts.py:76`, `approvals.py:94`,
`subcommands.py:164-168`, `checks.py:41-47`, `dispatcher.py:116-124`).

| # | Решение | Шаг |
| --- | --- | --- |
| U28 | `TaskUsage` не расширяется (иначе меняется каждый `goal.*`); уровни различаются меткой в `by_model`; `RunReport.tier_reached` считает `RunTally`. | 18b, 20b |
| U29 | Драйвер `ModelDriver` на каждый уровень (лениво); индекс — `state.model_tier` в `_model_step`, `_account`, `_run_plan`. | 18b |
| U30 | «Нет действия» = пустой ответ (нет ни текста, ни вызовов). При уровне выше и `escalate_on_no_action` — `continue` + `tier_signal="no_action"` вместо терминала. | 18b |
| U31 | Task-local поля `progress_blocks`, `tier_marks: TierMarks`, `tier_signal` в `TASK_BOUNDARY_RESETS`; эскалация считает события «с последней смены уровня». | 18b |
| U32 | `tier_escalation(state, settings, *, tier_count) -> (updates, reason)`; один вызов — в начале итерации `while` в `AgentLoopEngine.run`; вызов №0 — уровень 0, replan — текущий; в стадиях источника — no-op. | 18b |
| U33 | Уровень 0 — всегда модель сессии (`--model`, `/model`); `cascade.tiers` — до 3 уровней над ним (P4). | 18a, 18c |
| U34 | `src/cli/models.py` строит `CascadeFactory` через `registry.build_chat_model`; фабрика внедряется в `SessionRuntime`, вызывается на каждую сессию; `_model_price` → функция модуля `model_price`. | 18c |
| U35 | `Retargeter` получает `tiers.tier(retarget_tier)`; уже выпущенный ключ `tier` в retarget-событиях не меняется. | 18c |
| U36 | OTLP: `payload["model"]` выигрывает у `phase_models` при имени span'а. | 18c |
| U37 | Цена нужна каждому уровню при любом денежном лимите (включая `loop.max_task_cost`): стенд — ошибка старта, интерактивный режим — предупреждение. | 18c |
| U38 | `NotificationChannel` не меняется; новый нейтральный `src/serve/commands.py` (`ChatCommand`, `ChatReply`, `CommandPort`, `OffsetStore`, `parse_command`, `ServeCommands`). | 19a |
| U39 | Канал получает `CommandPort` в `run(commands)` (сервис `ServeRuntime`), а не в конструкторе — разрыв цикла композиции. | 19b, 19c |
| U40 | `serve.telegram.api_base = ""` — пусто = константа в `src/channels/telegram.py`; непустое — `https://` или `http://` на `LOCAL_HOSTS`; `api.telegram.org` — только в этом файле. | 19b |
| U41 | Токен в пути `/bot<token>/`: все ошибки → `TelegramError*` `from None`; фильтр токена на логгерах `httpx`/`httpcore`; тесты ищут токен во всех выходах. | 19b |
| U42 | `doctor` переходит на `config_error_lines` (переезжает в общий модуль `src/cli/`). | 19c |
| U43 | `callback_data = a:<ticket_id>:<o\|s\|d>` с полным id (45 байт ≤ 64); таблица коротких ключей не нужна. | 19b |
| U44 | `ApprovalsPort.answer(..., *, via="api")`, порт переезжает в `src/serve/approvals.py`. | 19a |
| U45 | `Notification.answers` — канал не предлагает `session`, если тикет её не допускает; тело webhook не меняется. | 19a |
| U46 | Брокер, контроллер и мост — свои на каждый worker; `ApprovalDesk` объединяет их за одним `ApprovalsPort`. | 19a, 20c |
| U47 | Мост берёт задание и прогон из `dispatcher.running_on(index)`; `running()` остаётся (наименьший индекс). | 20b |
| U48 | `Dispatcher` над `WorkerPool`: задача на слот, FIFO-скан очереди; ожидание cooldown внутри задачи слота; `session_max_runs` по слоту. | 20b |
| U49 | Резервы бюджета дня: идущий прогон вычитает свой `max_tokens`/`max_cost` из остатка для остальных; без места — ждёт в очереди. С одним worker'ом резервов нет. | 20b |
| U50 | Ключи исключения `job:<id>` и `recording:<name>`; один asyncio-цикл и синхронные записи; ограждение «нет потоков в `src/serve`, `src/channels`». | 20b |
| U51 | Worker с закреплёнными заданиями берёт только их (P5); конфигурация без свободного worker'а при незакреплённых заданиях или `free_tasks` — ошибка. | 20a, 20b |
| U52 | Плейсхолдер `{profile_dir}` = `SessionConfig.user_data_dir`; `partial` в `build_session`, сигнатура фабрики MCP и фейки не меняются; проверка `external` его не считает. | 20a |
| U53 | Порты пула и `JobSpec.worker` — в валидаторе `ServeSettings`; Q13 — в `startup_problems`. | 20a |
| U54 | `worker`, `workers`, `tier_reached` пишутся только при пуле > 1 или после подъёма уровня. | 20b |
| U55 | `/run <job>` из чата — только для заданий с триггером `type: telegram`. | 19a, 19b |
| U56 | Offset канала — `<serve.dir>/channels/telegram.json`, сохраняется до обработки (at-most-once); сообщения старше `max_update_age_seconds` игнорируются. | 19a, 19b |
| U57 | Лимиты Bot API (64 байта, 4096 символов) — константы протокола в канале; всё настраиваемое — в `TelegramSettings`. | 19b |
| U58 | Канал подтверждений = `api_approvals` или (`telegram` в `channels` и `telegram.approvals`); `POST /v1/approvals` по-прежнему только при `api_approvals`. | 19c |
| U59 | `--check` офлайн; `getWebhookInfo` — при реальном старте (до браузеров) и в `doctor`. | 19c |
| U60 | Новые события `serve.command`, `serve.channel.error`; `serve.run.skipped{reason: "cancelled"}`. | 19a |
| U61 | `doctor` и `summary()` показывают каскад, Telegram и пул без секретов. | 18c, 19c, 20c |
| U62 | `test_serve_boundaries.py`: `src.serve` не импортирует `src.channels`; `src/channels` — только разрешённые импорты; `src.channels` импортирует только `src/cli/`. | 19b |
| U63 | Подъём уровня — `ladder.escalated{from: agent, to: agent, reason, from_tier, tier, model}`. | 18b |

Решения, принятые при реализации (продолжение нумерации):

| # | Наблюдение | Решение | Шаг |
| --- | --- | --- | --- |
| U64 | Подъём по неудачам при дефолтах (`escalate_after_failures=2`, `loop.max_consecutive_failures=3`) оставлял новому уровню одну попытку до терминала `max_consecutive_failures`. | Подъём уровня сбрасывает `consecutive_failures` в 0: новый уровень получает полное окно неудач. Сброс ограничен числом уровней (≤ 3 подъёма за задачу), поэтому бесконечного цикла нет. `TierMarks` хранит только `replans` и `progress_blocks`. | 18b |

Уточнения плана к дизайну (не меняют решений):
- Scope коммитов — из `.claude/conventions/commit.md`: `agent-loop` (не `engine`), `config`, `cli`,
  `serve`, `evals`, `docs`; новый scope — `channels`.
- Тесты канала — в `tests/channels/` (новый каталог, как `tests/serve/`).

### 0.2 Вопросы к вам (дефолт — в скобках, по нему и пойду без ответа)

- **P4.** Уровень 0 — всегда модель сессии (`llm.model`, `--model`, `/model`), `cascade.tiers` — до
  3 уровней над ним? Буквальный §9.2 (`tiers[0]` заменяет модель сессии) ломает `/model` и
  `--model` при включённом каскаде. *(Уровень 0 — модель сессии.)*
- **P5.** Worker с закреплёнными заданиями резервируется только за ними (свободные задачи и чужие
  задания туда не попадают) — или закрепление лишь «предпочтение»? *(Резервировать: профиль с
  логинами не должен достаться свободной задаче из чата.)*
- **P6.** Подтверждения кнопками включены вместе с каналом Telegram (`serve.telegram.approvals:
  true`) или только явно? Без ответа всё равно `deny`, но тикет ждёт человека вместо
  немедленного отказа. *(`true`.)*
- **P7.** Свободная задача из чата — только `/task <текст>` (при `serve.free_tasks`), обычный текст
  получает подсказку `/help`? *(Только `/task`.)*
- **P8.** `workers > 1`, `launch: external`, а у браузерного сервера нет `{profile_dir}`:
  предупреждение или ошибка старта? Name-free код не знает, изолирует ли сервер профили сам.
  *(Предупреждение.)*
- **P9.** `doctor` сам вызывает `getWebhookInfo`, когда Telegram настроен, или только под флагом,
  как `--llm`? *(Сам.)*
- **P10.** Allowlist только по `chat.id` (группа — целиком), без allowlist пользователей и без
  запрета групп? *(Да; руководство рекомендует личный чат.)*
- **P11.** Evals каскада — `tests/evals/cascade_scenarios/` и baseline `cascade_v1.json`; строку в
  `.github/workflows/gate.yml` не добавлять? *(Каталог и baseline — да, `gate.yml` — нет.)*

---

## 1. Правила партии

- Один шаг — один коммит (скилл `commit`, английский; scope — §0.1). Тесты — в том же коммите,
  сначала падающий. Перед коммитом — тесты шага: `uv run pytest <файлы> && git commit -F <файл>`.
  Полный `uv run pytest` и evals — один раз в конце (§3).
- Безопасные дефолты: `cascade.enabled=false`, `serve.workers=1`, `telegram` нет в
  `serve.channels`, `serve.telegram=None`, `free_tasks=false`. Без них поведение байт-в-байт
  прежнее: события, ключи, порядок, число вызовов модели, строки ledger, `/v1/status`,
  `serve.started`.
- Границы:
  - цепочка `SessionRuntime → GoalRunner → Agent → AgentLoopEngine → TurnController` не меняется;
  - engine не вызывает `registry`/провайдеров — уровни приходят через `EngineResources.models`;
  - `src.providers` и `src.channels` импортирует только `src/cli/`;
  - `src/serve/**` не импортирует `src.agent_loop.execution`, `src.browser`, `src.harness`,
    `src.cli`, `src.providers`, `src.channels`;
  - `src/channels/telegram.py` импортирует только `src.config`, `src.contracts`,
    `src.serve.commands`, `src.serve.notify`, `src.serve.clock`, `httpx`, stdlib;
  - вендорные имена и URL — только в `src/channels/telegram.py`.
- Name-free: команды чата — фиксированная лексика стенда (`src/serve/commands.py`); `{profile_dir}`
  — плейсхолдер, а не знание о Playwright.
- Тюнаблы — `src/config.py` + `.env.example` + `config.example.yaml` + `tests/test_config.py`.
  Константы — только лексика и факты протокола (U57).
- Секреты: `bot_token` и `ModelTierSpec.api_key` — `SecretStr`; не в логах, событиях, `doctor`,
  `--check`, исключениях; ошибки конфигурации — без значений (`config_error_lines`).
- Новых зависимостей нет (`httpx` уже есть).
- Не пушу и не мержу.

---

## 2. Шаги

### Шаг 18a. Секция `cascade`

**Коммит:** `feat(config): add the cascade settings`

- `src/config.py`: `ModelTierSpec` (`model`, `label=""`, `provider`, `host`, `api_key: SecretStr`,
  `temperature` 0..2, `reasoning_effort`, `timeout_seconds` gt 0; `llm_overrides()` по
  `model_fields_set`) и `CascadeSettings` (`enabled=false`, `tiers` `max_length=3`,
  `retarget_tier` `0..len(tiers)`, `escalate_after_failures=2` ge 1, `escalate_after_replans=1`
  ge 1, `escalate_on_progress_block=true`, `escalate_on_no_action=true`); валидатор — диапазон
  `retarget_tier`, уникальность меток. 18-я секция в `Settings`.
- `.env.example`, `config.example.yaml` — закомментированный пример с двумя уровнями.
- `src/llm.py` — docstring `ModelTiers` (несколько уровней — норма).

**Тесты:** `tests/test_config.py` — дефолты, границы, `extra="forbid"`, `retarget_tier` вне
диапазона, повтор метки, явный `temperature: null` против отсутствующего поля, JSON в env
`AUTOBROWSER_CASCADE__TIERS`, примеры.

### Шаг 18b. Эскалация уровня в engine

**Коммит:** `feat(agent-loop): escalate the model tier when the agent is stuck`

- `state.py`: `TierMarks`, поля `progress_blocks`, `tier_marks`, `tier_signal`; в
  `TASK_BOUNDARY_RESETS` (`src/harness/session.py`), не в `to_session_state` (U31).
- `guards.py`: `TierReason`, `escalation_possible`, `tier_escalation` (U32) — чистые функции.
- `loop.py`:
  - драйвер на уровень, `_tier_fields(index)` → `{tier, model}` только при > 1 уровне (U29);
  - `_model_step`, `_account`, `_run_plan` на `state.model_tier`;
  - пустой ответ при уровне выше → `continue` + `tier_signal` (U30);
  - блок progress guard → `progress_blocks + 1`;
  - `_escalate_tier(state)` в начале итерации `while` → `ladder.escalated` (U63).

**Тесты:**
- `tests/test_tier_escalation.py` — каждый повод и порядок, «с последнего подъёма», сброс метки
  неудач успехом, верхний уровень, стадия `playback`, один уровень за проверку, очистка сигнала.
- `tests/test_model_tiers.py` — два `FakeChatModel`: вызов №0 на уровне 0; блок guard → ход на
  уровне 1 и событие; replan на текущем уровне; пустой ответ на 0 → ход на 1, на верхнем —
  `blocked`; `by_model` и цена по уровню; `budget_exhausted` до вызова любого уровня; **снимок
  `(type, payload)` с одним уровнем равен текущему**.
- `tests/test_harness_session.py` — новые поля сбрасываются на границе задачи.

### Шаг 18c. Уровни из CLI, retarget, цены, OTLP, doctor

**Коммит:** `feat(cli): build the cascade tiers with the provider registry`

- `src/cli/models.py` (новый): `tier_llm_settings`, `build_tier`, `cascade_factory(settings, *,
  provider)` → `None`, если каскад выключен или уровней нет (U34).
- `src/harness/session.py`: `CascadeFactory`; `SessionRuntime(..., cascade_factory=None)` →
  `SessionContext.initialize`; `cascade_tiers`; `model_tiers()` (уровень 0 — модель сессии,
  коллизия меток — `ValueError`); `build_agent` передаёт `models=tiers`; `model_price` — функция
  модуля; `_step_sources(tiers)` даёт `Retargeter` уровень `retarget_tier` (U35); предупреждение
  о цене в `start()` (U37).
- `src/cli/bootstrap.py::build_session`: боевой путь без `llm_factory` берёт
  `models.cascade_factory(get_settings(), provider=args.provider)`; тесты — только явно.
- `src/serve/checks.py`: цена каждого уровня при денежных лимитах, включая `loop.max_task_cost`;
  уровни валидируются офлайн (`tier_llm_settings`) в `--check` (U37).
- `src/harness/otel_spans.py`: `payload["model"]` выигрывает (U36).
- `src/cli/subcommands.py::_doctor`: строка `cascade` (метки, провайдер, публичный хост, цена;
  ключ — только «set»); с `--llm` — проба уровней с другим `(provider, host)`; предупреждения
  «пустые `tiers` при `enabled`» и `escalate_after_failures >= loop.max_consecutive_failures` (U61).

**Тесты:** `tests/test_cli_models.py` (наследование, `--provider`, `openai_compat` без хоста —
ошибка без ключа, monkeypatch `build_chat_model`, `api_key` не в `repr`/ошибках),
`tests/test_harness_session.py` (фабрика в `initialize`, уровень 0 — сессия, `/model` меняет только
0, коллизия, retarget-уровень, предупреждение о цене), `tests/test_otel_spans.py`,
`tests/serve/test_checks.py` и `tests/test_cli_entry.py` (`serve --check`: денежный лимит без цены
уровня — код 2, текст без ключа), `tests/test_doctor.py` (если есть; иначе `test_cli_entry.py`).

### Шаг 18d. Evals каскада

**Коммит:** `test(evals): add cascade scenarios and their baseline`

- `tests/evals/cascade_scenarios/` — сценарии с двумя скриптованными моделями: подъём по блоку
  guard, по replan, по пустому ответу; без подъёма — модель уровня 1 не вызывается.
- `scripts/run_evals.py`/`src/agent_loop/evals.py` — минимальная поддержка второго уровня в
  сценарии, если её нет (аддитивно, сценарии без неё идут как сейчас).
- `tests/evals/baselines/cascade_v1.json`. `gate.yml` не трогаю (P11).

**Тесты:** прогон `--scenarios tests/evals/cascade_scenarios --baseline
tests/evals/baselines/cascade_v1.json`; тест загрузки сценариев, если есть аналог для playback.

### Шаг 19a. Порт команд чата

**Коммит:** `feat(serve): add the chat command port`

- `src/serve/commands.py` (U38): `ChatCommandName`, `COMMAND_PREFIX`, `COMMAND_ALIASES`
  (`start` → `help`), `ChatCommand`, `ChatReply`, `parse_command`, `CommandPort`, `OffsetStore`,
  `ServeCommands` (команды `/help`, `/jobs`, `/run`, `/status`, `/budget`, `/cancel`, `/task` —
  §3.5 дизайна; rate limit по отправителю — существующий `RateLimiter`; ответы санитизируются и
  режутся до `notify_max_chars`).
- `src/serve/dispatcher.py`: `cancel(target)` (очередь → `skipped{cancelled}`, работающий →
  `worker.cancel()`), `submit_task` с `TriggerRef` канала.
- `src/serve/approvals.py`: `ApprovalsPort` с `via` (переезд из `app.py`), `ApprovalDesk`;
  мост заполняет `Notification.answers` (U44–U46).
- `src/serve/notify.py`: `Notification.answers` (U45).
- `src/serve/state.py`: `ChannelStateStore` (U56).
- `src/config.py`: `TriggerSpec.type += "telegram"` (только `id`; остальные поля запрещены) — здесь,
  потому что `/run` проверяет триггер (U55).
- `src/agent_loop/events.py`: `serve.command`, `serve.channel.error` (U60).

**Тесты:** `tests/serve/test_commands.py` (разбор, каждая команда, `/run` без триггера, `/task` без и
с `free_tasks`, `/cancel`, rate limit, `serve.command` без текста задачи, `rejected` с лимитом),
`tests/serve/test_approvals.py` (`ApprovalDesk`, `via`), `tests/serve/test_dispatcher.py`
(`cancel`), `tests/serve/test_state.py` (`ChannelStateStore`), `tests/test_config.py` (триггер).

### Шаг 19b. Канал Telegram

**Коммит:** `feat(channels): add the Telegram channel`

- `src/config.py`: `TelegramSettings` (§5.3 дизайна: `bot_token`, `allowed_chat_ids`, `api_base=""`,
  `approvals=true`, `poll_timeout_seconds=30` 1..60, `request_timeout_seconds=10`,
  `poll_backoff_seconds=1`, `poll_backoff_max_seconds=60`, `send_retry_max_seconds=30`,
  `max_update_age_seconds=300`); `ServeChannel += "telegram"`; `serve.telegram`; валидаторы:
  канал требует токен и непустой allowlist, триггер `telegram` требует канал, `api_base` — https
  или http на `LOCAL_HOSTS`; ошибки без значений.
- `src/channels/__init__.py`, `src/channels/telegram.py`: `BotApi` (`get_updates`, `send_message`
  plain text + `link_preview_options.is_disabled`, `answer_callback`, `clear_keyboard`,
  `get_webhook_info`), `TelegramError*` `from None`, фильтр токена на логгерах (U41),
  `TelegramChannel` (`check`, `send`, `run(commands)` — long polling, offset до обработки, возраст,
  allowlist, `@<bot>`, callback `a:<id>:<code>`, backoff удвоением, 409/401) (U39–U43, U56, U57).
- `tests/test_serve_boundaries.py` (U62): `src.channels` запрещён в `src/serve`; разрешённые
  импорты `src/channels/**`; `src.channels` импортирует только `src/cli/`; `api.telegram.org` —
  только в `telegram.py`; нет `to_thread`/`run_in_executor`/`threading` в `src/serve` и
  `src/channels`. `tests/test_engine_boundaries.py` — вендорный маркер, если там есть список.

**Тесты:** `tests/channels/test_telegram.py` на `httpx.MockTransport` и `FakeClock` — сценарии §8
дизайна (offset и рестарт, старые сообщения, чужой чат, `/run@Bot`, карточка и кнопки ≤ 64 байт,
нажатие, истёкший тикет, чужой callback, 429 с повтором и без, 5xx/обрыв и backoff, 409, 401,
`check()` при webhook, **токен отсутствует** в ошибках, `caplog` DEBUG, событиях; plain text,
`MESSAGE_MAX_CHARS`). `tests/test_config.py` (`TelegramSettings`, deep merge токена из env с YAML),
`tests/test_serve_boundaries.py`.

### Шаг 19c. Telegram в `serve` и `doctor`

**Коммит:** `feat(cli): wire Telegram into serve and doctor`

- `src/cli/serve.py`: канал `telegram` → `BotApi` + `TelegramChannel` в `Notifier`; `ServeCommands`
  над диспетчером и `ApprovalDesk`; сервис `channel.run(commands)` в `ServeRuntime`;
  `channel.check()` до запуска браузеров (webhook/401 → ошибка старта, код 2) (U59); брокер и
  `interactive` worker'а по U58; `summary()` — `telegram (N chats)` без токена (U61);
  `aclose` при остановке.
- `config_error_lines` → общий модуль `src/cli/` (`output.py`); `doctor` использует его (U42);
  строка `telegram` (token set, N chats, webhook none/set — без URL) (P9).

**Тесты:** `tests/test_cli_entry.py` (`serve --check` с Telegram: офлайн, токен не в выводе;
ошибка конфигурации без значения; старт с webhook — код 2), `tests/test_doctor.py`/`test_cli_entry.py`
(`doctor` с monkeypatch `BotApi`, сломанная секция без токена).

### Шаг 20a. Поле `workers`, закрепление, `{profile_dir}`

**Коммит:** `feat(serve): add workers, job pinning and the profile placeholder`

- `src/config.py`: `serve.workers` 1..8, `JobSpec.worker: int | None` ge 0; валидаторы: диапазон
  CDP-портов, `serve.port` вне него, `worker < workers`, хотя бы один незарезервированный worker
  при незакреплённых заданиях или `free_tasks` (U51, U53).
- `src/serve/checks.py`: Q13 — `workers > 1` и `memory.persistent_enabled` → ошибка старта;
  предупреждение пула при `external` без `{profile_dir}` (P8) — функция предупреждений рядом с
  `startup_problems`, данные о плейсхолдере — из настроек MCP-серверов (name-free).
- `src/harness/mcp_setup.py`: плейсхолдер `{profile_dir}`; `cdp_placeholder_servers` его не
  считает; `default_mcp_runtime_factory(..., *, profile_dir="")`.
- `src/harness/session.py` / `src/cli/bootstrap.py::build_session`: профиль через `partial` (U52).
- `.env.example`, `config.example.yaml`.

**Тесты:** `tests/test_config.py`, `tests/serve/test_checks.py` (Q13, предупреждение),
`tests/test_mcp_setup.py` (подстановка в args и env, `external`), `tests/test_harness_session.py`.

### Шаг 20b. `Dispatcher` над пулом

**Коммит:** `feat(serve): run jobs on a pool of workers`

- `src/serve/pool.py`: `WorkerSlot`, `WorkerPool` (`of`, `size`, `slots`, `slot_for`, `start`
  последовательно, `shutdown` параллельно, `tool_names`).
- `src/serve/dispatcher.py` (U47–U50): слоты и задача на слот; FIFO-скан с ключами исключения и
  резервами дня; `running_on(index)`; `running()` — наименьший индекс; `session_max_runs` по слоту;
  `cancel` и `stop(grace)` по всем слотам; `day_left(exclude=)`; `status()["workers"]` только при
  пуле.
- `src/serve/reports.py`: `worker`, `tier_reached` (ключи только при значении, U54).
- `src/serve/tally.py`: `ladder.escalated` с `tier` → `tier_reached` (U28, U63).
- `src/serve/approvals.py`: мост с `index` (U47).

**Тесты:** `tests/serve/test_worker_pool.py` — сценарии §8 дизайна (параллелизм, ожидание слота,
исключения по заданию и записи, закрепление и резерв, бюджет дня с резервами, `session_max_runs`
по слоту, `cancel`/`stop`, мост worker'а 1, файлы без переплетения, **снимки совместимости при
`workers=1`** — ledger, `status()`, `serve.started`). `tests/serve/test_dispatcher.py` — Rig на
`WorkerPool.of`, существующие тесты зелёные без изменений смысла. `tests/serve/test_tally.py`.

### Шаг 20c. Сессия на каждый worker

**Коммит:** `feat(cli): start one session per worker`

- `src/cli/serve.py`: `worker_args(serve, paths, index)` (порт `cdp_port_base + i`, профиль
  `profiles_dir/w<i>`, метки `w<i>`, `interactive` по U58); на каждый worker — сессия, брокер,
  `SessionController`, мост; `WorkerPool`; `ApprovalDesk` для API и Telegram; `serve.started{workers}`
  при пуле; `summary()` — `workers N`.
- `src/serve/runtime.py`: старт и остановка пула.
- `docker/serve.example.yaml`: `--user-data-dir {profile_dir}`.

**Тесты:** `tests/test_cli_entry.py` (`worker_args`, композиция пула на фейковой сессии),
`tests/serve/test_runtime.py`; `-m live` Docker smoke — по возможности (§3).

### Шаг 21. Документация

**Коммит:** `docs(serve): document the cascade, the Telegram channel and the worker pool`

- `docs/development/serve-mode.md`: каскад, пул (`workers`, закрепление, `{profile_dir}`, память на
  worker'а, Q13), раздел «Telegram channel» (бот через BotFather, токен в env, `allowed_chat_ids`,
  триггер `telegram`, команды, подтверждения кнопками, webhook должен быть снят, предупреждение о
  данных у третьей стороны, личный чат); `replay.md` (`retarget_tier`).
- Отдельное руководство по моделям не нужно: каскад — короткий раздел в `serve-mode.md` плюс
  абзац в `docs/development/` о моделях, если там есть такой файл.
- `docs/diagrams/serve-mode.md` (пул, Telegram), `agent-runtime-flow.md` (точка эскалации),
  `harness-boundaries.md` (`src/channels`, `CascadeFactory`).
- `docs/glossary.md`: каскад, уровень модели, эскалация уровня, worker, пул, закрепление, канал.
- `CLAUDE.md`, `AGENTS.md`: 18 секций (`cascade`), граница `src/channels`, команды тестов.
- Статусы: дизайн серии — «Фаза 4 реализована», системный дизайн и этот план — Implemented.
- Запись в ADR-реестр, если P4 расходится с §9.2 (уточнение, не замена решения).

---

## 3. Проверка в конце партии

1. `uv run pytest` — полный набор.
2. `uv run scripts/run_evals.py --baseline tests/evals/baselines/agent_loop_v2.json --memory-seed tests/evals/memory_seed` — без изменений.
3. `uv run scripts/run_evals.py --scenarios tests/evals/playback_scenarios --baseline tests/evals/baselines/playback_v1.json` — без изменений.
4. `uv run scripts/run_evals.py --scenarios tests/evals/cascade_scenarios --baseline tests/evals/baselines/cascade_v1.json` — совпадает.
5. `uv run main.py --no-mcp --task "inspect page"` — интерактивный путь не изменился.
6. `uv run main.py serve --check` с примером профиля (каскад, Telegram, `workers: 2`) — токен не
   печатается.
7. `uv run pytest -m live tests/pipeline/test_docker_smoke.py` — затронут `docker/serve.example.yaml`.
8. `code-reviewer` (opus) один раз над партией. Подтверждённые замечания — через падающий тест,
   отдельным коммитом `fix(...)`; о неисправленных — с причиной.
9. Отчёт: коммиты, результаты 1–7, замечания review, что осталось на вас.

---

## 4. Что не входит в Фазу 4

- Блокировка файлов памяти L4 для пула — позже (Q13); сейчас `workers > 1` требует
  `persistent_enabled=false`.
- Webhook-транспорт Telegram (входящий порт) — нет (Q11).
- Свободный текст без `/task` (P7), allowlist пользователей в группах (P10).
- Отдельный `serve.approval_timeout_seconds` — по опыту (Q5).
- Понижение уровня модели, эскалация по сбою провайдера — нет.
- Заметка `[harness]` о смене модели — нет: промпты — ваша зона.
- Другие мессенджеры — через `webhook` или новый адаптер в `src/channels/` по тому же порту.
- Строки `cascade_v1`/`playback_v1`/Docker smoke в `gate.yml`, правка `PLAYBACK_RETARGET_*`, merge и
  push — за вами.

---

## 5. Риски плана

| Риск | Смягчение |
| --- | --- |
| Шаг 18b трогает горячий путь engine: регрессия поведения с одним уровнем. | Снимок `(type, payload)` с одним уровнем; `_tier_fields` пустой при одном уровне; оба baseline в конце. |
| Шаг 20b — переписывание `Dispatcher` (633 строки) под слоты. | Существующие `test_dispatcher.py` идут на `WorkerPool.of` без изменения ожиданий; снимки V1; при > ~800 строк диффа `pool.py` уходит отдельным коммитом. |
| Утечка токена бота через сторонние логи или traceback. | `from None`, фильтр логгеров, поиск значения токена во всех выходах тестов. |
| `escalate_after_failures` ≥ `max_consecutive_failures` делает подъём по неудачам невозможным. | Предупреждение `doctor`/`--check`; дефолты 2 < 3. |
| Резервы бюджета дня держат прогоны в очереди. | С одним worker'ом резервов нет; резерв = бюджет прогона, уже ограниченный остатком. |
| N браузеров по памяти. | Дефолт 1, потолок 8, руководство. |
| Evals без поддержки второго уровня в сценарии. | Аддитивное расширение загрузчика в 18d; старые сценарии не меняются. |

---

## 6. Review Focus

1. **Байтовая совместимость без новых настроек** — `model.*` без `tier`/`model`, ledger, `status()`,
   `serve.started`, `goal.*` (`TaskUsage` не расширен) — шаги 18b, 20b.
2. **Токен бота в любом выходе** — URL в ошибках httpx, логгер `httpx`, `doctor`, `--check`,
   события, сообщения исключений — шаги 19b, 19c.
3. **Повторная эскалация сразу после подъёма** — метки «с последней смены», сброс серии неудач —
   шаг 18b.
4. **Ответ кнопкой не тому worker'у и подделанный `callback_data`** — `ApprovalDesk`, allowlist
   callback, `session` для тикета без `allow_session` — шаги 19a, 19b.
5. **Параллельные прогоны превышают дневной бюджет** или два прогона одной записи — резервы и
   ключи исключения — шаг 20b.
6. **Старые команды после рестарта** — offset до обработки, `max_update_age_seconds` — шаг 19b.
7. **Закреплённый профиль с логинами достаётся свободной задаче из чата** — шаг 20b.
