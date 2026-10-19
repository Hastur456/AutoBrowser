# ADR-2026-10-19: Терминальный UI в стиле Claude Code над общим SessionController

Status: Proposed
Date: 2026-10-19

> Файл назван по первой редакции, где главным клиентом был локальный веб-UI. Решение
> пересмотрено по отзыву пользователя до коммита. Переименовать файл можно в
> `2026-10-19-terminal-ui-and-session-controller.md` одновременно с принятием ADR.

## Context

Единственный интерфейс AutoBrowser — REPL на cmd2 (`src/cli/agent_cli.py`).

**Что видит пользователь.** Задача идёт в фоне на отдельном asyncio-цикле. Видны только
`Task started` и итоговый `final_answer`. План, ходы, вызовы инструментов, наблюдения, решения
хуков и разрешений эмитятся как `EventRecord` в `events.jsonl`, но в интерфейс не попадают.

**Технические проблемы текущего REPL:**
- Подтверждения (`ApprovalPrompt`) делят строку ввода с командами.
- Логика представления живёт в `AgentCli`.
- В `src/cli/` зашиты тюнаблы.
- Посторонние писатели (stderr Playwright MCP и Chrome, `print` в `chrome.py`, `logging`)
  порвали бы любой терминальный интерфейс.
- Терминальные `goal.*` несут repr всего `AgentLoopResult` вместе с историей сообщений.
- Отмена задачи не даёт `goal.cancelled` и оставляет задачу «running».

**Пожелания пользователя:**
- интерфейс «как у Claude Code»;
- запуск командой `autobrowser` из PowerShell, cmd, bash и zsh — этому посвящён отдельный
  [ADR о команде и домашнем каталоге](2026-10-19-autobrowser-command-and-home-directory.md);
- красота;
- минимальный риск визуальных багов.

Основная платформа пользователя — Windows.

**Ключевые факты исследования:**
- Inline-режим Textual не поддерживается на Windows.
- Toad (Textual) не поддерживает нативный Windows; Mistral Vibe (Textual) называет Windows
  второсортной платформой.
- prompt_toolkit 3.0.53 уже в `uv.lock` (через cmd2 4.1.2) и годами работает в консолях
  Windows. В mintty без ConPTY не стартует — нужен fallback.
- Claude Code пришёл к схеме «замороженная история в scrollback + маленькая живая зона» и
  диффовому рендеру с DEC 2026, чтобы победить мерцание.

Подробности — в [проектном документе](../development/2026-10-19-gui-redesign-design.md).

## Decision

1. **UI-нейтральное ядро в процессе.** Пакет `src/ui/` с `SessionController` — единым фасадом
   команд (`submit`, `cancel`, `answer`, `reconnect`, `reset`) и чтения для всех фронтендов.
   Ядро над `SessionRuntime`, цепочка владения не меняется:
   - session-scoped `EventBroadcaster` (`src/harness/event_stream.py`) — ещё один `EventSink`,
     раздаёт уже редактированные записи;
   - session-scoped `ApprovalBroker` (`src/harness/approvals.py`) — реализация существующего
     `HumanInputCallback`; публикует `ApprovalTicket` (`src/contracts.py`), принимает
     `once | session | deny` из любого клиента, по таймауту — `deny`.
   
   Engine не меняется, кроме двух аддитивных событий телеметрии.
2. **Главный клиент — inline-TUI на prompt_toolkit + rich.**
   - Владелец терминала — `prompt_toolkit.Application(full_screen=False)`. rich только рендерит
     блоки в строки; `rich.Live` не используется.
   - Завершённые блоки один раз «замораживаются» в обычный scrollback. Живая зона
     ограничена по высоте: план, активный вызов, карточка подтверждения, spinner, поле ввода,
     строка статуса.
   - UI работает на своём цикле в главном потоке, сессия — на `RuntimeLoop`.
   - UX Claude Code: баннер, блоки вызовов с `⎿`, чек-лист плана, подтверждение в потоке
     (стрелки и цифры), slash-команды с дополнением, Esc/Ctrl+C, Ctrl+O/Ctrl+T, строка статуса,
     markdown-ответ.
3. **Правила против визуальных багов** обязательны:
   - единственный писатель (`RenderPump`), все обновления через очередь;
   - никакого вывода из других потоков; stderr MCP, Chrome, `logging` и `warnings` — в файл;
   - ограниченная живая зона, троттлинг перерисовки, DEC 2026 где поддерживается;
   - глифы «хрома» только ширины 1 с ASCII-заменой, без правых границ у блоков с переменным
     содержимым;
   - санитизация управляющих последовательностей из содержимого страниц;
   - восстановление терминала при выходе и краше.
4. **Уровни поддержки терминала** `full / degraded / plain` с автоопределением (Windows
   Terminal, VS Code, conhost Windows 11/10, mintty с ConPTY и без, tmux, не-TTY) и ручными
   переопределениями. `plain` — построчный режим без prompt_toolkit.
5. **Визуальный язык браузерного агента:**
   - роли цветов: `accent` лазурный, `tool` фиолетовый, `link` бирюзовый, `ok`, `warn`,
     `error`, `muted`, `plan`, `border`;
   - темы `dark` / `light` / `mono`;
   - fallback на 256 и 16 цветов, `NO_COLOR`; truecolor никогда не выводится безусловно.
6. **Конфигурация.** Новая секция `ui` (`AUTOBROWSER_UI__*`):
   - фронтенд и внешний вид: `frontend`, `screen`, `theme`, `color`, `glyphs`, `banner`,
     `density`, `detail`, `locale`;
   - лимиты вывода: `preview_lines`, `preview_chars`, `live_max_lines`;
   - тайминги и ввод: `refresh_hz`, `sync_output`, `escape_timeout_ms`, `double_press_seconds`,
     `size_poll_seconds`;
   - поведение: `markdown`, `input_history`, `input_history_size`, `approval_bell`,
     `print_format`, `log_file`;
   - ядро: `event_buffer_size`, `subscriber_queue_size`, `status_interval_seconds`,
     `shutdown_timeout_seconds`.
   
   `permissions.approval_timeout_ratio` и `approval_args_preview_chars` заменяют константы
   `src/cli/approval.py`.
7. **Телеметрия.**
   - Новые `plan.updated`, `turn.started`, `approval.answered`.
   - `goal.*` несут `{task, status, final_answer, turns}` вместо repr результата.
   - Отмена эмитит `goal.cancelled` и закрывает `TaskRecord`.
8. **cmd2-REPL** остаётся как `--classic` на переходный период. Веб-UI и desktop-окно —
   опциональные будущие клиенты того же ядра.

## Consequences

Плюсы:

- Пользователь видит работу агента по ходам и может прервать её клавишей. Подтверждения
  больше не конфликтуют с вводом.
- Нативный scrollback, поиск и копирование терминала сохраняются (inline).
- Новых runtime-зависимостей для TUI нет: prompt_toolkit, rich, wcwidth и pygments уже в
  `uv.lock`.
- Рендер и редьюсер — чистые функции, проверяемые снимками на 40/80/120 колонках и VT-эмулятором
  `pyte`.
- Исправляются утечка истории в `events.jsonl`, «вечно running» отменённые задачи и посторонний
  вывод в терминал.

Минусы и риски:

- Больше кода, чем у веб-UI: клавиатура, деградация, тесты экрана, зоопарк терминалов
  Windows.
- Два рендерера (rich и prompt_toolkit) с разными таблицами ширины — смягчается правилом
  глифов ширины 1 и отсутствием правых границ.
- Ctrl+O меняет детализацию только живых и будущих блоков: замороженную историю не
  перерисовать; полный просмотр — `/replay`.
- Формат `goal.*` меняется для внешних читателей `events.jsonl`.
- В mintty без ConPTY доступен только plain-режим.

Последующая работа:
- `--continue` / `--resume` (нужно хранение `SessionState`);
- fullscreen-режим;
- удаление cmd2;
- переименование пакета;
- веб-UI.

## Alternatives Considered

- **Textual inline** — не поддерживается на Windows; inline-ввод отмечался как «лагающий».
- **Textual fullscreen** — теряет нативный scrollback (тот же компромисс, что
  `CLAUDE_CODE_NO_FLICKER`); главный клиент для Windows-пользователя рискованнее. Возможен как
  опциональный клиент.
- **cmd2 + `add_alert`** — нет постоянной живой зоны во время выполнения (spinner, карточка,
  статус). Оставлен как `--classic`.
- **rich `Live` + `input()`** — два перерисовщика: мерцание и рваный ввод.
- **Rust/Go TUI рядом с Python** — отдельный тулчейн и протокол; у Mistral Vibe Rust-TUI
  мигал из-за безусловного truecolor и не принимал перевод строки в Git Bash.
- **Локальный веб-UI как главный клиент** (первая редакция: FastAPI + SSE + Alpine CSP) —
  пользователь хочет терминал. Веб остаётся опциональным клиентом того же ядра.
- **NiceGUI, Gradio, Streamlit, Flet, Chainlit, Tauri** — отклонены в первой редакции:
  конфликт стека, неподходящая модель исполнения, архивирован или избыточен.

## Related

- [Проектный документ](../development/2026-10-19-gui-redesign-design.md)
- [ADR-2026-10-19: Команда `autobrowser` и домашний каталог](2026-10-19-autobrowser-command-and-home-directory.md)
- [ADR-2026-10-17: Explicit Agent Objects](2026-10-17-explicit-agent-objects.md)
- [ADR-2026-10-01: PermissionEngine](2026-10-01-permission-engine.md),
  [Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
- [ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)
- [ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md)
- Код: `src/cli/agent_cli.py`, `src/cli/approval.py`, `src/harness/session.py`,
  `src/harness/chrome.py`, `src/mcp/transports.py`, `src/agent_loop/events.py`,
  `src/agent_loop/goals.py`, `src/agent_loop/execution/loop.py`
