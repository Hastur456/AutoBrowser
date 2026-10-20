# ADR-2026-10-19: Команда `autobrowser` и домашний каталог AutoBrowser

Status: Accepted (2026-10-20: packaging, the home directory, print mode, subcommands and
--continue/--resume are implemented; see docs/development/terminal-ui.md)
Date: 2026-10-19
Supersedes: [ADR-2026-09-26: Default YAML Settings File](2026-09-26-default-yaml-settings-file.md)
— в части места файла по умолчанию

## Context

Пользователь хочет запускать агента командой `autobrowser` из PowerShell, cmd, bash и zsh, из
любого каталога. Сейчас это невозможно.

**Нет точки входа.** В `pyproject.toml` нет `[build-system]` и `[project.scripts]`, а
`[tool.uv] package = false`. Запуск — только `uv run main.py` из репозитория.

**Неудобное имя пакета.** Код — пакет верхнего уровня `src`: 394 строки `from src…` /
`import src…` в 104 файлах. Есть и строковые ссылки на модули в конфигурациях хуков
(`handler: src.harness.builtin_hooks:…`).

**Пути привязаны к репозиторию или к текущему каталогу:**
- `config.yaml` ищется рядом с исходниками (`ROOT_PATH`, `src/config.py:113-118`);
- `.env` — в текущем каталоге (`env_file=".env"`, `config.py:1168`). Запуск из чужого
  проекта подхватил бы его `.env`;
- `storage.root_dir = Path(".autobrowser")` — тоже относительно текущего каталога;
- правила браузера читаются из `docs/development/browser-agent-rules.md` по пути от
  исходников (`src/agent_loop/skills.py:36-41`);
- командные хуки запускаются с `cwd=ROOT_PATH`.

**Playwright MCP** стартует через `npx -y @playwright/mcp@latest`
(`src/harness/mcp_setup.py:37`, `:47`) — нужен Node.js в `PATH`, но не нужен текущий каталог.

## Decision

1. **Точка входа.**
   - Сборка через `uv_build` (`requires = ["uv_build>=0.13.1,<0.14"]`).
   - Упаковывается существующий пакет: `module-name = "src"`, `module-root = ""`.
   - `[project.scripts] autobrowser = "src.cli.entry:main"`, `package = true`.
   - `main.py` вызывает тот же `entry.main`.
   - Установка: `uv tool install --editable <репозиторий>` (разработка),
     `uv tool install git+…`, `pipx`, либо `uv run autobrowser` в репозитории.
     `uv tool update-shell` добавляет каталог команд в `PATH`.
2. **Домашний каталог.**
   - `AUTOBROWSER_HOME` (переменная уровня процесса, как `AUTOBROWSER_CONFIG_FILE`), иначе
     `~/.autobrowser`.
   - Одно фиксированное место; сканирования текущего каталога нет.
3. **Поиск файлов** (первый найденный, без слияния, если не сказано иное):
   - **YAML:** `AUTOBROWSER_CONFIG_FILE` (обязателен, если задан) → `<home>/config.yaml` →
     `ROOT_PATH/config.yaml` (наследие чекаута).
   - **`.env`:** `ROOT_PATH/.env`, затем `<home>/.env` поверх. Текущий каталог не читается.
   - **`storage.root_dir`:** дефолт — `<home>`; относительный путь — от `<home>`.
   - **Правила браузера:** ресурс пакета через `importlib.resources`; копия в `docs/` сверяется
     тестом.
   - **Командные хуки:** `cwd` — каталог файла конфигурации, объявившего хук (иначе `<home>`).
     - **Изменено при внедрении (2026-10-20):** `cwd` командных хуков остаётся корнем чекаута (`ROOT_PATH`). Хук может прийти не только из файла, но и из env, `.env` или кода, поэтому «файл, где объявлен хук» определить нельзя; поставляемые `scripts/hooks/` лежат в чекауте.
4. **Поверхность CLI в стиле Claude Code:**
   - запуск: `autobrowser`, `autobrowser "задача"`, `autobrowser -p "задача"`;
   - print mode: `--output-format text|json|stream-json`, задача из stdin, коды возврата `0`
     done / `1` failed / `2` ошибка аргументов / `3` blocked / `130` прервано;
   - продолжение: `-c/--continue`, `-r/--resume [id]` — после появления хранения `SessionState`;
   - `--version`;
   - подкоманды: `doctor`, `sessions`, `replay`, `completion`.
   
   Разбор — argparse (`build_parser()`) с тонким диспетчером зарезервированных подкоманд;
   существующие флаги сохраняются.
5. **Дополнение в оболочках.** Через `argcomplete`: bash/zsh штатно, fish/PowerShell через
   contrib-генератор. `autobrowser completion <shell>` печатает скрипт. В cmd дополнения нет.
6. **Node.js и Playwright MCP.**
   - Остаются записью `mcp_servers`.
   - `autobrowser doctor` проверяет `node`/`npx`, Chrome, CDP-порт, источники конфигурации и
     возможности терминала.
   - `config.example.yaml` показывает закреплённую версию пакета как пример.

## Consequences

Плюсы:

- Одна команда во всех оболочках. Editable-установка из чекаута сохраняет текущий
  рабочий процесс.
- Запуск из любого каталога не зависит от текущего каталога и не читает чужой `.env`.
- Личный `config.yaml` в репозитории продолжает работать (наследственный путь).

Минусы и риски:

- Пакет верхнего уровня `src` — антипаттерн, безопасный только в изолированных окружениях
  (`uv tool`, `pipx`).
- Новые сессии по умолчанию пишутся в `~/.autobrowser`, а не в `<репозиторий>/.autobrowser`.
  Для прежнего места нужно задать `storage.root_dir`; `doctor` показывает итоговые пути.
- `.env` из текущего каталога больше не читается — изменение поведения для запусков не из
  корня репозитория.
- Новая runtime-зависимость `argcomplete`; поддержка PowerShell в ней — contrib.

Последующая работа:
- переименование пакета `src` → `autobrowser` (src-layout `src/autobrowser/`, codemod
  импортов, переходное отображение префикса `src.` в загрузчике хуков) — отдельный ADR;
- перенос `PLAYWRIGHT_MCP_PACKAGE` в настройки.

## Alternatives Considered

- **Сразу переименовать пакет в `autobrowser`.** Правильнее, но это большой механический
  коммит (импорты, строки `monkeypatch`, обработчики хуков в пользовательских конфигах,
  документы, AST-ограды). Не стоит смешивать его с UI-работой.
- **Скрипты-обёртки (`autobrowser.ps1`, `autobrowser.cmd`, shell-скрипт) вокруг `uv run
  --project`.** Без упаковки, но три файла на три оболочки, нет `pipx`/`uv tool`, хуже
  дополнение.
- **Typer вместо argparse** (есть в `uv.lock`, дополнение для PowerShell из коробки).
  Пришлось бы переписать парсер и тесты, а позиционная задача вместе с подкомандами в Click
  неудобна.
- **Конфиг в текущем каталоге (`./.autobrowser/config.yaml`)** — противоречит решению не
  сканировать текущий каталог (ADR-2026-09-16 и ADR-2026-09-26) и делает поведение зависимым
  от места запуска.
- **platformdirs-каталоги** (`%APPDATA%`, `~/.config`) — разносит конфиг и данные по разным
  местам. Один `~/.autobrowser` проще объяснить и совпадает с привычным `.autobrowser`.

## Related

- [Проектный документ, §7](../development/2026-10-19-gui-redesign-design.md)
- [ADR-2026-10-19: Терминальный UI и SessionController](2026-10-19-session-controller-and-local-web-ui.md)
- [ADR-2026-09-26: Default YAML Settings File](2026-09-26-default-yaml-settings-file.md)
- [ADR-2026-09-16: Opt-In YAML Settings File](2026-09-16-opt-in-yaml-settings-file.md)
- [ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md)
- Код: `pyproject.toml`, `main.py`, `src/config.py`, `src/cli/parser.py`,
  `src/cli/bootstrap.py`, `src/agent_loop/skills.py`, `src/harness/command_hooks.py`,
  `src/harness/mcp_setup.py`
