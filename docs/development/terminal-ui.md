# Терминальный UI: руководство

Статус: действует с 2026-10-19, дополнено 2026-10-20 (шаги 1–18 плана из
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
uv run main.py --continue               # продолжить последнюю сессию с её диалогом
uv run main.py --resume                 # выбрать сессию из списка (или --resume <id>)
uv run main.py -p "задача"              # print-режим: только результат в stdout
uv run main.py doctor                   # пути, конфигурация, Node.js, Chrome, терминал
```

После `uv tool install --editable .` то же самое доступно командой `autobrowser`.

Флаги переопределяют поля секции `ui` только на один запуск. Постоянные настройки задаются в
`config.yaml` (`ui:`) или через `AUTOBROWSER_UI__<ПОЛЕ>` (см. `config.example.yaml`).
`NO_COLOR` в окружении отключает цвета.

## Домашний каталог

Всё хранится в домашнем каталоге AutoBrowser: `AUTOBROWSER_HOME`, иначе `~/.autobrowser`
(на Windows `%USERPROFILE%\.autobrowser`).

| Что | Где |
|---|---|
| Конфигурация | `AUTOBROWSER_CONFIG_FILE` → `<home>/config.yaml` → `config.yaml` в корне чекаута (наследие) |
| `.env` | `.env` в корне чекаута (наследие), поверх него `<home>/.env`; текущий каталог не читается |
| Сессии, память, журнал | `<home>/sessions/`, `<home>/memory/`, `<home>/logs/autobrowser.log` (`storage.root_dir`) |
| История ввода | `<home>/input_history` |
| Командные хуки | запускаются в корне чекаута: там лежат `scripts/hooks/`, а хук может прийти не только из файла (env, `.env`) |

Относительный `storage.root_dir` отсчитывается от домашнего каталога. Прежнее значение
`.autobrowser` означает сам домашний каталог: при запуске выводится предупреждение,
строку можно удалить из конфигурации. Старые данные в `<чекаут>/.autobrowser` не переносятся
автоматически: `autobrowser doctor` показывает их и подсказывает, что делать. Варианты —
перенести `sessions/` и `memory/` в домашний каталог или задать `storage.root_dir`
абсолютным путём.

## Продолжение сессий

После каждой задачи в `<session>/state.json` сохраняется то, что наследует следующая задача:
диалог, последнее наблюдение, сохранённые снимки страниц, данные последнего действия
(`storage.persist_state`, по умолчанию включено). Файл хранится как есть, без
редактирования секретов, — как и содержимое страниц, которое видел агент.

- `--continue` (`-c`) продолжает самую свежую сессию, у которой есть `state.json`.
- `--resume <id>` (`-r`) принимает id или его уникальный префикс; без id показывает
  `ui.resume_list_size` последних сессий и спрашивает номер.
- Продолженная сессия сохраняет id, историю задач (`/tasks`) и нумерацию событий;
  браузер и MCP запускаются заново при первой задаче.
- Из `state.json` в продолженную сессию переносится только диалог. Состояние страницы
  (наблюдение, снимки, последнее действие) описывало браузер, которого после перезапуска
  уже нет: с ним модель видела бы мёртвые `ref`, а правила доступа — чужой домен.
- Сессию держит один процесс: на `<session>/.lock` ставится блокировка ОС (не pid). Она
  берётся при открытии продолженной сессии (ещё до чтения файлов) или при старте новой и
  держится до выхода; открыть сессию, открытую в другом процессе, нельзя. После падения
  процесса ОС снимает блокировку сама; на Windows это может занять мгновение, тогда повторите
  `--continue`.
- Продолжить можно только сессию, в которой была хотя бы одна задача. Сессия, которая так
  и не запускалась, не оставляет записи; запущенная без задач видна в `sessions`, но не
  продолжается.
- `autobrowser sessions [--all]` — список сессий; `resume` отмечает те, что можно продолжить.

## Print-режим

`autobrowser -p "задача"` (или задача из stdin: `echo "задача" | autobrowser -p`) выполняет
одну задачу без интерфейса и печатает только результат:

| `--output-format` | stdout |
|---|---|
| `text` (по умолчанию, `ui.print_format`) | финальный ответ |
| `json` | `{"status", "final_answer", "turns", "session_id", "task_id"}` (+ `error`) |
| `stream-json` | JSON-строки элементов ленты (редактированы), затем `{"type": "result", ...}` |

Ход задачи пишется в stderr только с `--verbose`. Подтверждения отклоняются (`dont_ask`),
если не задан `--permission-mode`. Коды возврата: `0` — готово, `1` — ошибка, `2` — неверные
аргументы, `3` — остановлено (blocked), `130` — прервано.

## Подкоманды и дополнение

- `autobrowser doctor` — версия, домашний каталог, файл конфигурации и его источник, `.env`,
  хранилище, сессии, журнал, старые данные, `node`/`npx`, Chrome, порт CDP, MCP-серверы,
  возможности терминала. Код возврата `1` при критической проблеме.
- `autobrowser sessions [--all]`, `autobrowser replay <id|префикс|папка|events.jsonl>`.
- `autobrowser completion <bash|zsh|fish|tcsh|powershell>` печатает скрипт дополнения
  (argcomplete). Дополняются флаги, подкоманды и id сессий для `--resume`.
  - PowerShell: `autobrowser completion powershell | Out-String | Invoke-Expression`
    в `$PROFILE`;
  - bash: `eval "$(autobrowser completion bash)"` в `~/.bashrc`, zsh — то же в `~/.zshrc`;
  - в cmd дополнения нет.
- Задачу, совпадающую с именем подкоманды, передают через `--task` или после `--`.

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

История ввода хранится между запусками в `<storage.root_dir>/input_history`
(`ui.input_history`, размер — `ui.input_history_size`). Это обычный текст: всё введённое,
включая пароли, сохраняется как есть. Отключить: `ui.input_history: false`.

## Slash-команды

`/help`, `/status`, `/tasks`, `/cancel`, `/mcp [reconnect <имя>]`, `/history [N]`, `/memory`,
`/url`, `/replay [№ | id сессии]`, `/permissions`, `/detail compact|verbose`,
`/theme dark|light|mono`, `/doctor`, `/reset`, `/clear`, `/exit`. Те же команды работают в
plain-режиме. `/url` показывает адрес страницы, которую агент видел последней (без вызова
инструмента); `/replay` — ход последней задачи, задачи с номером из `/tasks` или другой
сохранённой сессии.

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

- Markdown-таблицы в итоговом блоке.
- Fullscreen-режим (шаг 19), удаление cmd2 (шаг 20), переименование пакета `src` →
  `autobrowser` (шаг 21), веб-UI (шаг 22).
- CI-workflow для Windows и Linux — отложен до появления CI. Проект пайплайна —
  [автономный пайплайн тестирования](2026-10-19-autonomous-test-pipeline-design.md).
