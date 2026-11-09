# ADR-2026-11-09: Контекст MCP-серверов, нейтральные промпты и `EnvironmentState`

Status: Accepted
Date: 2026-11-09

Дополняет: [ADR-2026-10-19: Обработка инструментов без имён](2026-10-19-name-free-tool-handling.md)
и [ADR-2026-10-26: Agent Skills](2026-10-26-agent-skills.md). Не заменяет их.

## Context

- Движок и харнесс уже не решают ничего по имени инструмента, но промпты
  (`src/agent_loop/prompts.py`) по-прежнему описывали инструменты Playwright MCP
  (`browser_snapshot`, `browser_click`, …), правила поиска, фильтров и Ozon. С любым
  другим сервером агент получал браузерные правила.
- MCP-сервер может сам объяснить свои инструменты: `InitializeResult.instructions` и
  `prompts/list` + `prompts/get`. `MCPManager` получал `instructions` и выбрасывал их.
- В состоянии цикла оставались следы браузера: `BrowserState`, `LoopState.browser`,
  `BrowserStateReducer` и поля `last_browser_action`, `ineffective_browser_action(s)`. Последние
  три никто не записывал: они переносились между задачами пустыми.

## Decision

1. **Контекст сервера как блоки системного промпта.** `MCPManager` хранит `instructions`
   последнего `initialize` (`MCPManager.instructions(name)`). При старте сессии
   `src/harness/server_context.py` (через `MCPRuntime.context_sections`) превращает их в
   `PromptSection` `mcp:<server>` («Server Instructions: <server>», приоритет 46 — после
   каталога и всегда-включённых скиллов). Промпты сервера подключаются явно: запись
   `mcp_servers` перечисляет их в `context_prompts`; каждый читается один раз через
   `prompts/get` в блок `mcp:<server>:<prompt>`. Промпт с обязательными аргументами,
   отсутствующий, упавший или у неготового сервера пропускается с событием `mcp.context_skipped`
   (`server`, `prompt`, `reason`) и не ломает старт. Поля записи сервера:
   `use_instructions` (по умолчанию `true`), `context_prompts` (`[]`), `context_max_chars`
   (8000, ≥ 1, обрезка каждого блока). Имена блоков попадают в `session.started.server_context`.
2. **Нейтральные промпты.** Агентский, планировочный, observer- и classifier-промпты не
   называют ни сервер, ни инструмент, ни сайт (`tests/test_prompts.py`,
   `test_the_prompts_are_server_neutral`); `prompts.py` больше не исключён из проверки имён
   инструментов в `tests/test_engine_boundaries.py`. Браузерные правила (снимки и ref'ы,
   вкладки, редактируемые поля, iframe, поиск, фильтры, `browser_evaluate`) живут только во
   встроенном скилле `browser-agent-rules`, который подключается при настроенном браузерном
   сервере. Правило про Ozon осталось только в скилле.
3. **`EnvironmentState`.** `BrowserState` → `EnvironmentState`, `LoopState.browser` →
   `LoopState.environment`, `BROWSER_STATE_FIELDS` → `ENVIRONMENT_STATE_FIELDS`,
   `BrowserStateReducer` → `SnapshotReducer`. Мёртвые поля `last_browser_action`,
   `ineffective_browser_action(s)` удалены (из `LoopState`, `SESSION_STATE_KEYS` и
   `AgentState`), а не переименованы. Модули движка не содержат идентификаторов с `browser`
   (`test_the_engine_state_has_no_browser_names`), кроме аннотации `BrowserHarness`.

## Consequences

- Любой сервер, присылающий `instructions`, учит агента своим инструментам без кода и
  без правки промптов.
- `instructions` включены по умолчанию: текст настроенного пользователем сервера попадает
  в системный промпт (как и описания его инструментов). Отключается `use_instructions: false`.
- Сохранённый `state.json` старых сессий со старыми ключами читается: неизвестные ключи
  `LoopState.from_state_overrides` игнорирует.
- Поведение на Playwright теперь зависит от скилла сильнее: правила, которых не было в
  скилле, перенесены в него. Нужна проверка на живых задачах и в evals.
- Контекст читается один раз: сервер, не готовый при старте сессии, и сервер, поднявшийся
  позже, своих блоков в эту сессию не добавляют (его инструменты модели доступны).
- `BrowserHarness` пока сохраняет имя; его переименование — отдельное изменение.

## Alternatives Considered

- Подключать все промпты сервера без аргументов автоматически — отвергнуто: промпт часто
  шаблон для пользователя, а не правило для агента; выбор за конфигурацией.
- Переименовать мёртвые поля в нейтральные имена — отвергнуто: их никто не пишет.
- Удалить скилл `browser-agent-rules` в пользу `instructions` — отвергнуто: Playwright MCP
  своих правил не присылает.

## Related

- `src/harness/server_context.py`, `src/harness/mcp_setup.py`, `src/mcp/manager.py`,
  `src/mcp/config.py`, `src/agent_loop/prompts.py`,
  `src/agent_loop/resources/skills/browser-agent-rules/SKILL.md`,
  `src/agent_loop/execution/state.py`.
- `tests/test_server_context.py`, `tests/test_session_skills.py`, `tests/test_prompts.py`,
  `tests/test_engine_boundaries.py`.
