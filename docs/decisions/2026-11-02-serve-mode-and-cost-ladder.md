# ADR-2026-11-02: Режим стенда (`autobrowser serve`), лестница стоимости и replay

Status: Accepted
Date: 2026-11-02

Открытые вопросы Q1–Q15 [проектного документа](../development/2026-11-02-serve-mode-design.md)
(§10.2) закрыты 2026-11-02: приняты рекомендуемые дефолты. В частности, `croniter` добавляется
(Q1), черновик `PLAYBACK_RETARGET_PROMPT` предлагается в PR с финальной правкой пользователя (Q2),
проба идёт через engine (Q3), `bypass` на стенде запрещён (Q10), в v1 replay доступен через print
mode и `/record` без TUI (Q15).

## Context

Позиционирование AutoBrowser, согласованное с пользователем: open-source harness для браузера.
Он выжимает максимум из открытых и дешёвых моделей и делает повторяемые задачи почти
бесплатными. От Claude Code + Playwright MCP его отличают свобода модели, цикл под браузер,
детерминированный replay и открытый код.

Режимов два, ядро одно:
- **интерактивный** — существующие TUI, plain, classic;
- **стенд** — долгоживущий процесс на сервере или в Docker, задачи по триггерам.

Для стенда главная метрика — стоимость одного прогона и надёжность.

Что мешает сейчас:
- **Каждый прогон стоит как первый.** План (вызов модели №0, `src/agent_loop/execution/loop.py:1004`)
  и каждый ход (`_agent_step`, `:373`) — вызовы модели. Удачный проход переиспользовать нельзя.
  `autobrowser replay` и `src/agent_loop/replay.py` только печатают трейс. Кассеты из
  [ADR пайплайна](2026-10-19-autonomous-test-pipeline.md) — строгое офлайн-воспроизведение для
  тестов, против живой страницы оно не работает.
- **Нет ступени без модели.** Heartbeat, который спрашивает модель «есть что делать?», жжёт
  токены вхолостую — урок OpenClaw, названный пользователем.
- **Нет бюджетов.** Usage уже приходит в `model.responded`, но не суммируется по задаче. Бюджета
  нет, отчёта стоимости нет, `StopReason` (`src/contracts.py:27`) не знает о бюджете.
- **Нет процесса без терминала.** Нет триггеров, очереди, HTTP-входа, headless и отдельного
  профиля. `SessionContext.initialize` всегда запускает видимый Chrome
  (`src/harness/chrome.py:19`). `SessionController.submit` (`src/ui/controller.py:100`)
  принимает только текст, а каждая задача наследует контекст предыдущей
  (`_task_state_overrides`, `src/harness/session.py:436`).
- **Одна модель на сессию.** Нельзя держать дешёвую модель по умолчанию и подключать сильную при
  застревании.
- **Безопасность стенда не описана.** Урок OpenClaw: открытые в интернет инстансы и
  вредоносные скиллы.

Главная техническая проблема replay — эфемерные ref'ы Playwright MCP. `[ref=e42]` «действителен,
пока страница не изменилась» ([playwright.dev](https://playwright.dev/mcp/snapshots)), поэтому
записанный `{"target": "e42"}` в следующем прогоне не найдётся или укажет на другой элемент.
`target` принимает и селектор Playwright, но решить name-free, принимает ли его конкретный
аргумент, нельзя.

Ограничения, которые решение обязано соблюсти:
- цепочка `SessionRuntime → GoalRunner → Agent → AgentLoopEngine → TurnController`;
- фронтенды — клиенты `SessionController`;
- никакой логики по именам инструментов ([ADR](2026-10-19-name-free-tool-handling.md));
- engine не импортирует `src/browser/`;
- модель — только через `ChatModel`;
- тюнаблы — в `src/config.py`;
- session-scoped hooks, permissions, memory и skills с их инвариантами;
- промпты правит пользователь;
- изменения аддитивные и opt-in, `goal_id == task_id`.

## Decision

1. **Лестница стоимости.** Каждый прогон поднимается по ступеням только при необходимости и не
   выше `RunProfile.max_stage`:
   - `probe` (0) — MCP-вызовы без модели и отпечаток региона вывода; нет изменений — 0 токенов;
   - `playback` (1) — воспроизведение записи без модели;
   - `retarget` (2) — дешёвая модель только на разошедшемся шаге;
   - `agent` (3) — агент; с каскадом сильная модель подключается при застревании;
   - `human` (4) — подтверждение через канал.

   Каждый переход — событие `ladder.escalated`. Выше `max_stage` — `blocked` с
   `stop_reason="ladder_cap"`.
2. **Replay внутри engine через нейтральный `StepSource`.**
   - `EngineResources.steps` — session-scoped `StepSourceFactory`; по умолчанию engine его не
     использует.
   - Для задачи с `TaskRequest.profile.playback` engine берёт записанный план без вызова №0 и на
     каждом ходе спрашивает источник раньше модели.
   - Каждое предложение — синтетический assistant tool call. Дальше он идёт **тем же
     конвейером**: progress guard, `pre_tool_use`, `PermissionEngine`, approval, `ToolBroker`,
     `post_tool_use`, наблюдение.
   - `StepHandoff` передаёт управление модели до конца задачи с заметкой `[harness]`; история
     воспроизведённых шагов остаётся в контексте.
   - Deny на воспроизводимом шаге не терминален: передача агенту. Отказ в подтверждении —
     `blocked`, как сейчас.

   В коде воспроизведение называется `playback`, артефакт — `recording`. Слово `replay` занято
   просмотром трейса.
3. **Эфемерные ref'ы: перепривязка по описанию элемента (R2), а не устойчивый локатор.**
   - **При записи.** Строковое значение аргумента, для которого в тексте страницы до шага есть
     `[ref=V]`, становится привязкой. Ref узнаётся по значению и маркеру, без имён инструментов и
     аргументов. Привязка хранит дескриптор строки: роль, доступное имя, номер среди одинаковых,
     предков, фрейм.
   - **При replay.** Перепривязка выполняется **до вызова**. Не ровно одна подходящая строка —
     расхождение без побочного эффекта.
   - Знание формата снимка живёт только в `src/browser/playback.py` (`BrowserPlaybackAdapter`,
     переиспользует `snapshot_element` и `page_url`). Для остальных серверов —
     `TextPlaybackAdapter` без привязок.
   - Локатор из блока сгенерированного кода сохраняется только как диагностический
     `locator_hint`.
4. **Детект расхождения name-free**, виды:
   - `catalog` — инструмента нет, изменились дайджест схемы или `readOnlyHint`;
   - `binding` — ref не перепривязался, или шаг содержит отредактированный секрет;
   - `tool_error`;
   - `checkpoint` — отпечаток вывода (путь URL, структура пар роль+имя без чисел) ниже
     `replay.checkpoint_min_similarity`;
   - `denied`.

   `binding` и `tool_error` идут на ступень 2 (`Retargeter`). Он вызывает модель уровня
   `retarget_tier` через `ChatModel` и принимает только один вызов записанного инструмента, у
   которого все привязки — реальные `[ref=…]` текущей страницы. Остальные виды и неудачное
   перенацеливание ведут на ступень 3. Текст `PLAYBACK_RETARGET_PROMPT` пишет пользователь в
   `prompts.py` по требованиям проектного документа.
5. **Записи.**
   - Session-scoped `Recorder` — это `EventSink` над сырыми событиями `tool.started` /
     `tool.finished`; `tool.started` аддитивно получает `server` и `read_only`.
   - Запись взводится через `RunProfile.record_as`. Сохраняется только завершённый прогон, после
     `goal_end`, с отредактированными секретами, без ошибочных и заблокированных шагов и без
     служебных серверов сессии.
   - Формат — `autobrowser.recording/1`, хранение —
     `<home>/<replay.dir>/<name>/{recording,previous,stats}.json`.
   - Финальный ответ после replay (`answer`): `agent` (передача на один ход, без нового промпта),
     `output` (регион последнего вывода, режим пробы) или `summary`.
6. **Бюджеты и учёт.**
   - Engine суммирует usage задачи в `TaskUsage`: вызовы, токены, цена по `llm.prices`, разбивка
     по ступеням и моделям.
   - Перед каждым вызовом модели engine проверяет `RunProfile.budget`. Исчерпание — `blocked` с
     `stop_reason="budget_exhausted"`.
   - Неизвестный usage даёт `cost=None` без ложной проверки денег, поэтому `max_model_calls`
     обязателен на стенде.
   - Дневные бюджеты, coalescing (`drop | queue_one`), cooldown и очередь живут в `serve`.
   - `RunReport` каждого прогона пишется в `<home>/serve/ledger.jsonl`.
7. **`autobrowser serve` — новый пакет `src/serve/`, клиент `SessionController`.**
   - Каждый worker — свой `SessionRuntime` с `ApprovalBroker` и `EventBroadcaster`.
   - `submit` и `run_outcome` аддитивно принимают `RunProfile`; прогоны стенда идут с
     `carry_context=False`.
   - Задания — только из конфигурации: `serve.jobs`, триггеры `interval`, `cron` (croniter),
     `webhook`, позже `telegram`.
   - Проба — обычная задача с профилем `max_stage="playback"`, `answer="output"`.
   - HTTP API работает на уже закреплённых `starlette` и `uvicorn`.
   - Импорт `src.agent_loop.execution`, `src.browser`, `src.cli`, `src.providers` из
     `src/serve/` запрещён тестом.
8. **Браузер стенда.**
   - `browser.launch: cdp | external` — во втором случае браузером владеет MCP-сервер, например
     Playwright MCP `--headless --user-data-dir`;
   - `browser.headless` и `browser.extra_args` — для `cdp`;
   - отдельный профиль `serve.profiles_dir` и CDP-порт `serve.cdp_port_base + i` на каждого
     worker'а.

   В фазе 3 worker один и очередь FIFO. Пул из N worker'ов (N браузеров и сессий) — фаза 4.
9. **Безопасность по умолчанию.**
   - Bind `127.0.0.1`. Нелокальный адрес требует `serve.allow_remote: true` и токен.
   - Bearer-токен и секреты триггеров.
   - Rate limit и лимит тела запроса.
   - Тело webhook не попадает в задачу.
   - Свободные задачи из API и канала выключены (`serve.free_tasks=false`).
   - `RunProfile.allowed_domains` задания только сужает права в `PermissionEngine`.
   - Без канала подтверждений — `dont_ask`, как сейчас без TTY. `bypass` на стенде — ошибка
     старта.
   - Скиллы по-прежнему не исполняются и не ставятся через API.
   - Стенд обслуживает только браузерные задания: без почты и календаря.
10. **Каскад моделей (фаза 4).**
    - `ModelTiers` в `src/llm.py` и секция `cascade`.
    - Engine поднимает `model_tier` чистой функцией `tier_escalation`. Поводы: блок progress
      guard, серия неудач, повторное планирование, ответ без действия.
    - Вниз уровень не опускается. Уровни строит `src/cli/` через `registry.build_chat_model`.
11. **Каналы.**
    - `NotificationChannel` в `src/serve/notify.py`: `log` и исходящий webhook с HMAC (фаза 3).
    - `TelegramChannel` (фаза 4) — вендорный адаптер в `src/channels/` по образцу `src/providers/`,
      импортируется только композицией в `src/cli/`. Транспорт — long polling, allowlist чатов,
      подтверждения inline-кнопками через `ApprovalBroker.answer(via="channel")`.
12. **Настройки.**
    - Новые секции: `replay`, `cascade`, `serve`.
    - Новые поля: `llm.prices`, `llm.currency`, `loop.max_task_model_calls`, `max_task_tokens`,
      `max_task_cost`, `browser.launch`, `headless`, `extra_args`.

    Всё выключено или нейтрально по умолчанию. Поля описаны в §9 проектного документа и
    добавляются в `.env.example` и `config.example.yaml`.
13. **Новые события.**
    - Новые типы: `ladder.escalated`, `playback.started`, `playback.step`, `playback.diverged`,
      `recording.saved`, `recording.skipped`.
    - Аддитивные поля:
      - `tool.started` — `server`, `read_only`;
      - `action.proposed` — `source`;
      - `goal.started` — `profile`;
      - `goal.*` — `usage`, `stage_reached`;
      - `model.*` — `phase="retarget"`, `tier`.
    - Стенд пишет собственный поток `<home>/serve/events.jsonl` (`serve.*`) с тем же
      редактированием.

## Consequences

Плюсы:
- Повторяемое задание без изменений стоит 0 токенов (ступень 0). Удачное — 0 вызовов модели
  (ступень 1). Модель платит только за расхождения.
- Replay не обходит безопасность: тот же конвейер hooks, permissions и approvals.
- Передача агенту бесшовная: история уже в `LoopState.messages`.
- Решение name-free и не зависит от версии Playwright MCP. Детект `binding` срабатывает до
  действия.
- Стоимость прозрачна. `RunReport`, `TaskUsage` и OTLP-атрибуты дают бенчмарку и Langfuse
  стоимость по ступеням.
- Интерактивный режим получает replay (`--record`, `-p --recording`) на том же ядре.

Минусы и риски:
- Аддитивные правки engine: ветка источника, пропуск вызова №0, бюджет, уровни моделей. Новые
  поля `LoopState`, `TaskRequest.profile`. Нужно доказать неизменность baseline `agent_loop_v2`.
- R2 ломается на одинаковых элементах и при смене текста. Стоимость растёт до ступени 2–3,
  пороги калибрует бенчмарк. Резерв — R1 (Q8).
- Формат снимка Playwright MCP может меняться. Знание локализовано в одном модуле и покрыто
  фикстурами реальных снимков.
- Новая зависимость `croniter` (Q1).
- Окно подтверждения в мессенджере ограничено watchdog'ом `GoalRunner` (Q5).
- Рост истории сессии на стенде — смягчается `serve.session_max_runs`.
- Секций настроек станет 18. `CLAUDE.md` и `AGENTS.md` нужно обновить.

Последующая работа:
- упаковка для PyPI (переименование `src`, LICENSE) — отдельный дизайн;
- бенчмарк открытых моделей с метриками `stage_reached` и `usage`;
- руководства `docs/development/serve-mode.md` и `replay.md`;
- обновление диаграмм engine, harness и сессии при реализации;
- параметризация записей и секреты в записях (Q6);
- пул worker'ов и правило для памяти L4 (Q13).

## Alternatives Considered

- **`serve` внутри `SessionRuntime`.** Сессия — «not a task solver». Триггеры, бюджеты и HTTP
  превратили бы её в приложение и нарушили бы правило «фронтенды — клиенты сессии».
- **Только внешний cron + `autobrowser -p`.** Ноль кода, но холодный старт браузера и MCP на
  каждый прогон. Нет coalescing, бюджетов, webhook, подтверждений и ступени 0. Остаётся
  документированным минимальным путём.
- **Replay как `ChatModel` (`PlaybackChatModel`).** Нарушает контракт «модель не принимает
  решений исполнения». Перепривязка по сообщениям ломается после сжатия истории, вызов №0
  приходится подделывать, телеметрия показывает несуществующие ответы модели.
- **Отдельный `PlaybackRunner` вне engine.** Либо дублирует конвейер permissions, hooks и
  approvals, либо обходит его — replay покупки без подтверждения недопустим.
- **R1: устойчивый локатор из блока кода Playwright MCP в `target`.** Привязан к формату codegen
  и версии одного сервера, `--codegen none` его отключает. Принимает ли аргумент селектор, видно
  только из описания. Ошибка видна только после вызова. Отложен как резерв (Q8).
- **R3: перенацеливание моделью на каждом шаге.** Делает ступень 1 платной. Оставлен только
  как ступень 2.
- **R4: исполнение сгенерированного Playwright-скрипта вне MCP.** Обходит MCP, permissions и
  события; привязан к одному серверу.
- **Изолированные контексты внутри одного MCP-сервера для параллелизма.** Специфика Playwright в
  ядре; одна сессия не исполняет параллельные задачи.
- **Свой парсер cron.** DST и граничные случаи. `croniter` снова поддерживается (pallets-eco).
- **Telegram через входящий webhook.** Требует открытого порта; long polling безопаснее.

## Related

- [Проектный документ](../development/2026-11-02-serve-mode-design.md)
- [Диаграммы: Serve Mode](../diagrams/serve-mode.md)
- [ADR-2026-10-19: Терминальный UI над общим SessionController](2026-10-19-session-controller-and-local-web-ui.md)
- [ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)
- [ADR-2026-10-01: PermissionEngine](2026-10-01-permission-engine.md),
  [Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
- [ADR-2026-09-30: Lifecycle Hooks Engine](2026-09-30-lifecycle-hooks-engine.md)
- [ADR-2026-09-28: Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)
- [ADR-2026-10-22: OpenAI-Compatible Chat Provider](2026-10-22-openai-compatible-chat-provider.md)
- [ADR-2026-10-19: Autonomous Test Pipeline](2026-10-19-autonomous-test-pipeline.md) (кассеты)
- [ADR-2026-10-19: Команда `autobrowser` и домашний каталог](2026-10-19-autobrowser-command-and-home-directory.md)
- [ADR-2026-10-29: OTLP Trace Export](2026-10-29-otlp-trace-export.md)
- Код: `src/ui/controller.py`, `src/harness/session.py`, `src/harness/approvals.py`,
  `src/harness/event_stream.py`, `src/cli/bootstrap.py`, `src/agent_loop/execution/loop.py`,
  `src/agent_loop/execution/resources.py`, `src/browser/permissions.py`, `src/browser/pages.py`,
  `src/harness/chrome.py`, `src/contracts.py`, `src/llm.py`, `src/config.py`.
