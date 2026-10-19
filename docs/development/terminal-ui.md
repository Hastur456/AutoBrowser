# Терминальный UI: руководство

Статус: действует с 2026-10-19 (шаги 11–17 плана из
[дизайна терминального UI](2026-10-19-gui-redesign-design.md)).

`autobrowser` и `uv run main.py` открывают inline-TUI в стиле Claude Code. Когда терминал не
справляется, включается построчный plain-режим. Прежний cmd2-REPL доступен через `--classic`.

## Запуск

```powershell
uv run main.py                          # TUI (или plain без способного терминала)
uv run main.py --task "найди наушники"  # стартовая задача
uv run main.py --no-mcp                 # без браузера и MCP: проверить интерфейс
uv run main.py --plain                  # принудительно построчный режим
uv run main.py --classic                # прежняя cmd2-оболочка
uv run main.py --ascii --no-color       # ASCII-глифы, без цветов
uv run main.py --theme light            # тема dark | light | mono
```

Флаги переопределяют поля секции `ui` только на один запуск. Постоянные настройки задаются в
`config.yaml` (`ui:`) или через `AUTOBROWSER_UI__<ПОЛЕ>` (см. `config.example.yaml`).
`NO_COLOR` в окружении отключает цвета.

## Как устроено

```text
главный поток: TuiApp (prompt_toolkit) ── RenderPump ── терминал
                    │   ▲
     RuntimeBridge  │   │ StreamItem / ApprovalTicket (call_soon_threadsafe)
                    ▼   │
поток RuntimeLoop:  SessionController ── SessionRuntime → GoalRunner → Agent → Engine
                        ├── EventBroadcaster (сток событий сессии)
                        └── ApprovalBroker   (HumanInputCallback)
```

- `src/ui/feed.py` проецирует события в элементы ленты по их типу и вычищает управляющие
  последовательности из содержимого страниц. Имена инструментов только выводятся.
- `src/ui/tui/transcript.py` — чистая модель блоков. Завершённые блоки печатаются в
  scrollback один раз и больше не перерисовываются. Живая зона (план, текущий вызов,
  карточка, spinner) не выше `ui.live_max_lines`.
- `src/ui/tui/render.py` — чистый рендер. Он не рисует в последней колонке, не ставит правых
  рамок и усекает по ячейкам.
- `src/ui/tui/pump.py` — единственный писатель в терминал. `logging`, `warnings` и traceback
  потоков на время работы уходят в `ui.log_file` (`src/ui/logging_setup.py`). stderr
  MCP-серверов пишется в `<session_dir>/stderr.log`.
- `src/ui/launch.py` выбирает фронтенд. Если TUI не стартует, он откатывается на plain и
  пишет причину в лог.

## Клавиши

| Клавиша | Когда | Действие |
|---|---|---|
| Enter | ввод | отправить задачу или команду |
| `\` + Enter, Ctrl+J | ввод | новая строка |
| Esc, Ctrl+C | идёт задача | прервать |
| Ctrl+C | есть текст | очистить ввод |
| Ctrl+C ×2, Ctrl+D | пустой ввод | выход |
| ↑ ↓, 1–3, Enter | карточка подтверждения | один раз / до конца сессии / нет |
| Esc | карточка подтверждения | нет |
| Ctrl+O | всегда | свернуть или развернуть вызовы |
| Ctrl+T | всегда | показать или скрыть план |
| Ctrl+L | всегда | перерисовать экран |
| Tab | меню команд | принять дополнение |

Новая задача во время выполнения отклоняется с подсказкой.

## Slash-команды

`/help`, `/status`, `/tasks`, `/cancel`, `/mcp [reconnect <имя>]`, `/history [N]`, `/memory`,
`/permissions`, `/detail compact|verbose`, `/theme dark|light|mono`, `/doctor`, `/reset`,
`/clear`, `/exit`. Те же команды работают в plain-режиме.

`/doctor` показывает, как был определён терминал: хост, уровень, глубину цвета, Unicode и
DEC 2026. Эти данные пригодятся в отчёте о визуальном баге.

## Терминалы

| Терминал | Уровень |
|---|---|
| Windows Terminal (pwsh, PowerShell 5.1, cmd, Git Bash внутри WT) | full |
| VS Code terminal | full, без DEC 2026 |
| conhost Windows 11 / 10 | degraded: ASCII для редких глифов, 256 / 16 цветов |
| Git Bash в mintty с ConPTY | degraded |
| Git Bash в mintty без ConPTY | plain: запустите в Windows Terminal или `winpty autobrowser` |
| tmux / screen | degraded |
| `TERM=dumb`, pipe, CI | plain |

Если ширина символов в VS Code «съезжает», задайте
`terminal.integrated.unicodeVersion = 11`. Если терминал повреждён после жёсткого убийства
процесса, помогут `reset` / `cls` или Ctrl+L.

## Тесты

```powershell
uv run pytest tests\ui tests\test_ui_feed.py tests\test_cli_frontend.py
uv run pytest tests\ui\test_windows_conpty.py   # настоящая ConPTY + pyte, только Windows
```

- Снимки рендера (`tests/ui/test_render_snapshots.py`) — обычные строки в тесте: точный снимок
  на 80 колонок плюс инварианты для 40/80/120 колонок × unicode/ascii × none/16/truecolor.
- Клавиши (`tests/ui/test_tui_keys.py`) проверяются через pipe-ввод prompt_toolkit с настоящим
  `RuntimeLoop` и фейковым контроллером.
- ConPTY smoke запускает `main.py --no-mcp` с нейтральным профилем
  `tests/ui/conpty_profile.yaml`. Модель при этом не вызывается.

## Ручной чек-лист перед релизом

- Windows Terminal: pwsh 7, Windows PowerShell 5.1, cmd, Git Bash.
- conhost: Windows PowerShell 5.1 с терминалом по умолчанию «Узел консоли Windows».
- VS Code terminal с GPU-ускорением и без него.
- Git Bash в mintty (ожидается plain) и с `winpty`.
- `chcp 866` и `chcp 1251` с кириллической задачей.
- `NO_COLOR=1`; `uv run main.py --task "..." | more` (plain без ANSI).
- Resize посреди задачи; окно 40×15.
- Ctrl+C посреди вызова и во время карточки, затем ввод в той же оболочке (курсор, эхо,
  стрелки).
- Длинный ответ с markdown-таблицей; страница с CJK и эмодзи в заголовке.

## Что ещё не сделано

- Print-режим `-p` и подкоманды `doctor`, `sessions`, `replay`, `completion` (шаг 7), а также
  домашний каталог `AUTOBROWSER_HOME` (шаг 6). Пока относительный `ui.log_file` отсчитывается
  от `storage.root_dir`.
- `/replay`, `/url` и markdown-таблицы в итоговом блоке (часть шага 16).
- `--continue` / `--resume` (шаг 18), fullscreen-режим (шаг 19), удаление cmd2 (шаг 20).
- CI-workflow для Windows и Linux. Проект пайплайна —
  [автономный пайплайн тестирования](2026-10-19-autonomous-test-pipeline-design.md).
