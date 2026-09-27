# Миграция на универсальный MCP Manager и удаление browser-адаптеров

Дата: 2026-09-24 · Основа: research «MCP Manager — system design», ред. 2 (2026-09-23)

## 1. Что добавлено

| Файл | Что это |
|---|---|
| `src/mcp/config.py` | `ServerRegistry`, `StdioServerConfig` / `StreamableHttpServerConfig` (discriminated union по `transport`), `ReconnectPolicy`, строгая подстановка `${VAR}` |
| `src/mcp/naming.py` | валидация имён серверов, `qualify()` — `server__tool`, санитизация, усечение до 64 с хэшем, разрешение коллизий |
| `src/mcp/catalog.py` | `ConnectionState`, дескрипторы tools/resources/templates/prompts, `ServerStatus`, discovery с пагинацией |
| `src/mcp/transports.py` | фабрики транспортов по типу (stdio, streamable HTTP) |
| `src/mcp/errors.py` | типизированные ошибки, у каждой стабильный `error_code` |
| `src/mcp/manager.py` | `MCPManager`: задача-владелец на соединение, реконнект-супервизор, inflight, list_changed, liveness, ephemeral-режим, graceful shutdown |
| `src/harness/mcp_tools.py` | мост «каталог → инструменты harness»: `MCPToolSource`, `MCPTool.invoke()`, преобразование `CallToolResult` в текст |
| `src/browser/normalization.py` | `ToolCallNormalizer` (протокол) + `BrowserToolNormalizer` — замена `BrowserProvider` и `PlaywrightMCPBrowserProvider`. Только сопоставление имени браузерного действия и отбрасывание аргументов, запрещённых схемой; логики ref/snapshot нет |
| `src/agent_loop/execution/tools.py` | `ToolBroker` принимает `normalizers=` вместо `browser_providers=`, пробрасывает `error_code` из исключений |
| `patches/loop.py.patch` | однострочная правка `TurnController` под новый конструктор `ToolBroker` |
| `tests/test_mcp_manager.py`, `tests/test_mcp_tools_bridge.py`, `tests/test_browser_normalization.py`, `tests/mcp_fixtures/fake_server.py` | 29 тестов на реальном stdio/HTTP MCP-сервере, без сети и без браузера |

Зависимость: `mcp>=1.24,<2` (проверено на 1.27.0), Python ≥ 3.11. Тесты не требуют pytest-asyncio.

## 2. Что сверено с python-sdk 1.27 (пункты «сверить» из research)

| Вопрос из research | Факт в 1.27 | Как учтено |
|---|---|---|
| Сигнатура `ClientSession` | `message_handler`, `read_timeout_seconds`, `client_info` — в конструкторе | так и передаётся в `_owner` |
| Код таймаута | `McpError(code=408)` (`httpx.codes.REQUEST_TIMEOUT`) | `REQUEST_TIMEOUT_CODE = 408` |
| Код обрыва | `CONNECTION_CLOSED = -32000`, сообщение `"Connection closed"` | -32000 — ещё и общий код «server error» по JSON-RPC, поэтому обрывом считается только SDK-сообщение или реально закрытый транспорт |
| Отправляет ли SDK `notifications/cancelled` при таймауте | **нет** | не компенсируем: id запроса SDK не отдаёт; сервер может доделать действие после таймаута — это ещё одна причина не повторять `tools/call` |
| Мёрж `env` у stdio | `{**get_default_environment(), **env}` | `env: None` → только безопасный набор SDK |
| Эскалация при закрытии stdio | закрыть stdin → 2 с → SIGTERM/SIGKILL дерева процессов | покрыто тестом с сервером, который игнорирует stdin |
| Имя HTTP-клиента | `streamable_http_client(url, http_client=...)`; у `streamablehttp_client` параметр `headers` устарел | используется новый API, старый — fallback |
| Заметит ли клиент смерть stdio-сервера без запроса в полёте (#396) | нет | транспорт читается через relay: EOF сразу переводит соединение в `RECONNECTING` (тест `test_silent_server_exit_is_detected_without_a_request`) |

Ещё два бага, найденные тестами (в research их не было):

1. **Висящий запрос при обрыве.** Когда транспорт закрывается, задача-владелец выходит из `ClientSession` и отменяет её receive-loop раньше, чем тот успевает ответить ожидающим запросам `CONNECTION_CLOSED`. Вызов висел до `call_timeout_s`. Исправлено так: владелец даёт запросам до 1 с на завершение, а оставшиеся запросы умершего соединения отменяются и получают `ServerConnectionLostError`.
2. **Незакрытый read-stream транспорта** (ResourceWarning на streamable HTTP). Relay теперь закрывает оба конца, как это сделал бы `ClientSession`.

## 3. Подключение

### 3.1 Конфиг

```python
# src/config.py
from src.mcp.config import MCPServerConfig

class AutoBrowserSettings(BaseSettings):
    ...
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    # какой сервер отдаёт browser_* тулы без префикса (см. §5.1)
    browser_mcp_server: str | None = "playwright"
```

```yaml
mcp_servers:
  playwright:
    transport: stdio
    command: npx
    args: ["-y", "@playwright/mcp@<pinned-version>"]
    stateful: true
    call_timeout_s: 120
  internal_search:
    transport: streamable_http
    url: "http://localhost:9000/mcp"
    headers:
      Authorization: "Bearer ${INTERNAL_MCP_TOKEN}"
browser_mcp_server: playwright
```

Аргументы запуска Playwright (`--headless`, `--isolated`, `--browser` и т.д.), которые сейчас собирает `src/mcp/playwright_runtime.py`, переносятся в `args`. Флаги, которые задаются через CLI или `ChromeSettings` (`src/harness/chrome.py`), можно дописывать в `args` при сборке registry: `settings.mcp_servers["playwright"].model_copy(update={"args": [...]})`.

### 3.2 Жизненный цикл: один Manager на долгоживущую сессию

По ADR `2026-07-23-long-lived-session-runtime` Manager должен жить столько же, сколько сессия runtime, а не одна задача. Иначе каждая задача будет запускать новый процесс Playwright.

```python
from mcp.types import Implementation
from src.mcp import MCPManager, ServerRegistry
from src.harness.mcp_tools import MCPToolSource
from src.browser.normalization import BrowserToolNormalizer

manager = MCPManager(
    ServerRegistry(settings.mcp_servers),
    client_info=Implementation(name="autobrowser", version=__version__),
)
await manager.start()                      # частичный старт не бросает — см. manager.status()
tool_source = MCPToolSource(
    manager,
    unprefixed_servers=[settings.browser_mcp_server] if settings.browser_mcp_server else [],
)
tool_registry = ToolRegistry(..., sources=[tool_source])        # адаптировать к реальному API
resources = EngineResources(..., tool_registry=tool_registry,
                            tool_normalizers=[BrowserToolNormalizer()])
try:
    ...  # GoalRunner / native_task_runner
finally:
    await manager.shutdown()               # можно звать из другой задачи
```

Если нужно отказаться от запуска без браузера, проверяйте после `start()`: `manager.status()["playwright"].state is ConnectionState.READY`.

### 3.3 Инструменты для LLM

`MCPToolSource.get_tools()` отдаёт объекты с `name`, `description`, `input_schema` (и алиасами `args_schema` / `args`) и async `invoke(args)`. Это тот же duck-typing, который уже понимают `ToolBroker._invoke_tool` и старый `_schema_dict`. **Сверьте с `src/harness/tools.py` и `ModelDriver`**, какие атрибуты они читают, чтобы собрать tool-спеку провайдера. Если там ожидается другое поле, добавьте его как property в `MCPTool`.

Список пересобирается из каталога в памяти при каждом вызове, без I/O и с кэшем по `manager.catalog_version`. `AgentLoopEngine.run()` читает `tool_registry.get_all()` один раз за прогон, а `ToolBroker` вызывает `registry.get()` на каждый вызов. Поэтому новые тулы после `list_changed` исполняются сразу, но LLM увидит их только в следующем прогоне. Если `ToolRegistry` кэширует тулы, инвалидируйте кэш по `catalog_version`.

## 4. Удаление старых адаптеров — рекомендуемый порядок

Делать отдельными коммитами, чтобы каждый шаг можно было откатить и чтобы parity/eval-прогоны показывали, какой шаг что изменил.

**Шаг 1 — добавить новое рядом (поведение не меняется).** Закоммитить `src/mcp/{config,naming,catalog,transports,errors,manager}.py`, `src/harness/mcp_tools.py`, `src/browser/normalization.py` и тесты. Новый `src/mcp/__init__.py` на этом шаге пока не ставить: старый реэкспортирует `playwright_runtime`. Либо временно добавить реэкспорт старого в новый `__init__`.

**Шаг 2 — переключить wiring.**

- `EngineResources` (`src/agent_loop/execution/resources.py`): поле `browser_providers` заменить на `tool_normalizers: Sequence[ToolCallNormalizer] = ()`.
- `src/agent_loop/execution/tools.py` — новая версия; `loop.py` — применить `patches/loop.py.patch` (одна строка).
- Место, где сейчас стартует `playwright_runtime` (`src/cli/bootstrap.py` / `src/harness/runtime.py` / `session.py`), заменить блоком из §3.2.
- `ToolRegistry`: регистрировать `MCPToolSource` как источник тулов. Метод `get_browser_providers()` больше не вызывается.
- Прогнать `tests/`, `scripts/run_evals.py` против `tests/evals/baselines/agent_loop_v1.json` и parity-тест последовательности имён тулов. Имена `browser_*` не меняются (§5.1), поэтому baseline должен совпасть.

**Шаг 3 — удалить старое.**

```text
src/browser/provider.py                  # BrowserProvider (заодно уходит зависимость от src.state.AgentState)
src/browser/adapters/                    # весь пакет, включая playwright_mcp.py
src/mcp/playwright_runtime.py
tests/test_playwright_mcp_provider.py    # заменён tests/test_browser_normalization.py
```

Затем:

- `src/browser/__init__.py`: убрать экспорт `BrowserProvider` (и `PlaywrightMCPBrowserProvider`, если реэкспортируется), при желании экспортировать `BrowserToolNormalizer`, `ToolCallNormalizer`.
- `src/browser/names.py`: `to_playwright_browser_name` больше не нужен — `BrowserToolNormalizer.resolve_name` сопоставляет по `to_canonical_browser_name`. Удалить, если grep ничего не находит.
- `src/harness/tools.py`: удалить `get_browser_providers()` и хранение провайдеров.
- `src/mcp/__init__.py`: заменить новым.
- **`src/browser/fake.py` не удалять** — на нём держатся детерминированные eval-сценарии. Его `get_tools()` остаётся источником тулов для реестра, а свои `normalize_request/normalize_result` он либо удаляет (их выполняет `BrowserToolNormalizer`), либо реализует протокол `ToolCallNormalizer` с новой сигнатурой `(request, state, tools)`. `tests/test_fake_browser_provider.py` поправить соответственно.

Контрольный grep (в конце шага должен быть пустым):

```bash
rg -n "BrowserProvider|browser_providers|get_browser_providers|PlaywrightMCPBrowserProvider|playwright_mcp|playwright_runtime|to_playwright_browser_name|src\.browser\.adapters"
```

**Шаг 4 — документация.**

- Новый ADR `docs/decisions/2026-09-24-universal-mcp-manager.md`: supersedes `2026-07-26-browser-provider-boundary.md` (у старого поставить статус *Superseded*), добавить его в `index.md`.
- `docs/diagrams/browser-provider-boundary.md` и `harness-boundaries.md` перерисовать: `Harness → ToolRegistry ← MCPToolSource ← MCPManager → N×(owner task → ClientSession → server)`, нормализация — `ToolBroker ⟲ BrowserToolNormalizer`.
- `docs/development/2026-07-26-browser-engine-migration.md`: сослаться на этот документ; `glossary.md`: Host, owner task, generation, stateful server.

## 4a. Удаление ref-логики

Из новой поставки ref-логика убрана полностью. `BrowserToolNormalizer` больше не:

- переписывает аргументы `ref` ↔ `target`;
- заполняет `element` из снапшота (`element_description_from_snapshot` удалена);
- ставит `error_code = BROWSER_ERROR_INVALID_REF` по тексту `Ref … not found` (`INVALID_REF_PATTERN` удалён).

Аргументы модели уходят в тул как есть; отбрасываются только ключи, запрещённые схемой (`additionalProperties: false`).

После этого восстановление по устаревшему ref в loop больше никогда не срабатывает. Его нужно удалить в файлах, которых в этой поставке нет:

| Файл | Что удалить |
|---|---|
| `src/agent_loop/execution/loop.py` | импорт и вызов `stale_snapshot_retry_update` в `TurnController._agent_step` (строки ~282–289: ветка `replan` и `snapshot_request.update(stale_snapshot_update)`), ключи `stale_snapshot_retries` и `invalid_ref_recovery_count` в `_run_plan`, упоминание stale-snapshot retry в docstring модуля |
| `src/agent_loop/execution/guards.py` | `stale_snapshot_retry_update` и всё, что читает `BROWSER_ERROR_INVALID_REF` |
| `src/agent_loop/execution/state.py` | поля `stale_snapshot_retries`, `invalid_ref_recovery_count` (`LoopState.apply` отвергает неизвестные ключи — удалять вместе с loop.py) |
| `src/agent_loop/execution/observation.py`, `completion.py` | обработку `invalid_ref`, если есть |
| `src/browser/errors.py`, `src/browser/__init__.py` | константу `BROWSER_ERROR_INVALID_REF` и её экспорт |
| `src/browser/fake.py` | генерацию ошибки `Ref … not found` и `ref`-аргументы фейковых тулов, если есть |
| `tests/evals/scenarios/stale_ref_recovery.yaml`, `tests/evals/baselines/agent_loop_v1.json` | сценарий восстановления по ref и его строки в baseline (baseline перегенерировать) |
| `tests/test_playwright_mcp_provider.py` | удаляется целиком (см. шаг 3) |

Контрольный grep после чистки:

```bash
rg -n "invalid_ref|INVALID_REF|stale_snapshot_retr|element_description_from_snapshot|\bref=|\[ref=" src tests scripts
```

## 5. Изменения поведения и риски

### 5.1 Имена тулов

По дизайну тулы называются `server__tool`. Но policy, observation, guards (`fresh_snapshot_request`, `browser_tabs`), eval-сценарии и golden-трейсы завязаны на словарь `browser_*` через `src/browser/names.py`. Поэтому сервер из `browser_mcp_server` выставляется **без префикса** (`browser_click`), а все остальные — с префиксом (`internal_search__query`). Имя без префикса откатывается к qualified, если оно не provider-safe или конфликтует. Manager об этом ничего не знает: это решение моста в harness.

Если позже захотите префикс и для браузера, нужно, чтобы `is_browser_tool_name` / `to_canonical_browser_name` понимали `playwright__browser_*`. Тогда `BrowserToolNormalizer` заработает без изменений, но придётся обновить baseline evals.

### 5.2 Ошибки в `ToolResult`

| Ситуация | `status` | `error_code` |
|---|---|---|
| `CallToolResult.isError` | `error` | — |
| таймаут | `error` | `mcp_request_timeout` |
| обрыв во время вызова | `error` | `mcp_connection_lost` (текст содержит «server state is lost» для stateful) |
| сервер недоступен / реконнект не удался | `error` | `mcp_server_unavailable` |
| закрыт при shutdown | `error` | `mcp_server_closed` |

**Рекомендация для loop (в эту поставку не входит).** На `mcp_connection_lost` от браузерного тула считать текущее состояние страницы недействительным (`needs_fresh_snapshot=True`) и написать в observation «браузер перезапущен, открытая страница потеряна». Иначе модель продолжит действовать так, будто страница на месте.

Для явной защиты у Manager есть `call_tool(..., expected_generation=n)`. Если сервер перезапускался, он бросает `ServerStateLostError` **до** отправки запроса. Можно запоминать `manager.generation("playwright")` на старте задачи и передавать через `MCPTool.invoke_raw`.

### 5.3 Результат тула как текст

`call_tool_result_to_text` склеивает text-блоки через `\n`, а картинки и бинарные ресурсы заменяет плейсхолдерами (`[image: image/png, N base64 chars]`). Если observation или скриншоты где-то использовали картинки из результата, берите их через `MCPTool.invoke_raw()`, который возвращает сырой `CallToolResult`.

### 5.4 Открытые вопросы (вне Manager)

- **Один Playwright на все параллельные задачи.** Вкладки и куки у них общие. Варианты: `--isolated` у Playwright MCP, или `await manager.add_server(f"pw-{task_id}", cfg)` на задачу и `remove_server` в конце — Manager это поддерживает, но тогда `unprefixed_servers` должен указывать на сервер текущей задачи.
- **`notifications/cancelled` при таймауте SDK 1.x не шлёт.** Долгий `browser_*` после `RequestTimeoutError` может доделаться на сервере.
- **Переход на python-sdk 2.x** затронет `_owner` (конструктор сессии, `message_handler`) и коды ошибок в `_request`. Всё это локализовано в `manager.py`, тесты ловят регрессии.
