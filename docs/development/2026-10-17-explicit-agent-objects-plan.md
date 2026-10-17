# Явные объекты агента — план реализации

Дата: 2026-10-17 · Ветка: `refactor/explicit-agent-objects` · Статус: **In progress**
(коммиты 1–5 и документация к ним сделаны; коммит 6 ждёт согласования ADR)

> **Ход реализации.** Решение по коммитам 1–5 зафиксировано в
> [ADR 2026-10-17 Injected Tool Roles](../decisions/2026-10-17-injected-tool-roles.md).
> Отклонения от плана:
>
> - Коммит 1: `src/autobrowser` был нужен сборщику `uv_build`, поэтому проект объявлен
>   неустанавливаемым uv-приложением (`[tool.uv] package = false`, без `[build-system]` и
>   `[project.scripts]`); в `uv.lock` источник проекта стал `virtual`.
> - Коммит 2: `policy_decision`/`policy_event` в `TASK_BOUNDARY_RESETS` оказались живыми
>   (`policy.decided`, тесты) и остались. Carry-over безопаснее, чем описано в §2.2:
>   `LoopState.from_state_overrides` пропускает неизвестные ключи. Вместо теста
>   «`SESSION_STATE_KEYS ⊆ LoopState`» — тест, что `to_session_state()` и
>   `SESSION_STATE_KEYS` описывают одни и те же поля.
> - Коммит 3: `read_only_tools` остался в `TurnController` — он вычисляется из аннотаций
>   живого списка инструментов, а не из ролей. `NullToolRoles` живёт в
>   `src/harness/tools.py`. Роли передаёт сессия (`SessionContext.tool_roles`), как
>   `permissions`/`memory`, а не `BrowserHarness`.
> - Коммит 4: запрет «литерала `browser_`» сужен до слов, начинающихся с `browser_`:
>   поля `last_browser_action` и т. п. остаются до отдельного переименования `BrowserState`.
>   Docstring'и движка поправлены в этом же коммите, allowlist не понадобился.
> - Коммит 5: `context.py` спрашивает сам ресурс (`PromptResource.trigger_tool_prefixes`,
>   `applies_to_tool`); `evals.py` оставлен как есть — это точка сборки с поддельным
>   браузерным сервером, и он уже передаёт `BrowserToolRoles` явно.

## 1. Зачем

`src/agent_loop/execution/` заявлен как server-neutral: docstring `observation.py` говорит, что
имена инструментов сравниваются «exactly as exposed», а `CLAUDE.md` запрещает класть адаптацию
браузерной схемы в движок. На деле движок импортирует `src.browser.names` и сам решает, какой
инструмент браузерный и какой из них снимает snapshot. Рядом лежит мёртвый путь активации
вкладки и пакет-заглушка `src/autobrowser`.

Шире: слои общаются через неявные вещи — `Any`-поля в `EngineResources`, `getattr` по
`harness`/`session_config`, магические ключи в `task_config`. Цель ветки — чтобы каждый слой
получал **явные, типизированные объекты**, как в `deepagents`
([разбор на Хабре](https://habr.com/ru/companies/sberbank/articles/1071036/)): агент собирается
одной фабрикой из модели, инструментов, бэкендов и middleware, и ничего не находит сам по
строкам.

Ветка — refactor: **поведение не меняется**. Главный регрессионный сигнал — baseline эвалов.

## 2. Текущее состояние (проверено по коду)

### 2.1 Браузерные имена в движке

| Место | Что знает о браузере |
| --- | --- |
| `execution/guards.py:18`, `:256–279`, `:360`, `:376` | `TABS_TOOL`; `_pending_tab_activation_request` собирает вызов `browser_tabs` с `{"action": "select", "index": N}` — схема Playwright MCP |
| `execution/observation.py:30`, `:136`, `:173–174` | `is_browser_tool_name` (префикс `browser_`), `is_browser_snapshot_name` (точное имя `browser_snapshot`), поле `is_browser_tool` |
| `execution/observation.py:195–201` | особый случай `TABS_TOOL`: сброс `pending_browser_tab_*` |
| `execution/__init__.py:11` | docstring ссылается на несуществующий `src.browser.observation` |
| `execution/tools.py:10–14` | docstring про удалённые `browser_providers` / `get_browser_providers()` |

Вне движка, та же граница `agent_loop → browser`: `src/agent_loop/context.py:23`,
`src/agent_loop/evals.py:24` (`is_browser_tool_name`), `src/cli/agent_cli.py:22` (`SNAPSHOT_TOOL`).
`src/browser/normalization.py` и тесты `test_browser_contracts.py`, `test_tool_broker.py`,
`test_agent_loop_hooks.py` импортируют `src.browser.names` законно (browser-слой и его тесты).

### 2.2 Мёртвый код

- `pending_tab_activation_request` импортируется в `loop.py:58` и реэкспортируется из
  `execution/__init__.py`, но **нигде не вызывается**; docstring сам признаёт, что поле
  никто не ставит.
- Поля `pending_browser_tab_index` / `_reason` только сбрасываются и переносятся:
  `execution/state.py:36–37, 54–55, 157–158, 181–182`, `harness/session.py:73–74`
  (`SESSION_STATE_KEYS`), `src/state.py:58–59`, и читаются в `context.py:307` как признак
  «браузерного контекста». Ни один путь их не устанавливает.
- `src/autobrowser/__init__.py` — заглушка `uv init` (`print("Hello from autobrowser!")`), на
  которую указывает `pyproject.toml:130` (`autobrowser = "autobrowser:main"`).
- `execution/__pycache__/policy.cpython-312.pyc` от удалённого `policy.py` (мусор, не в git).

**Совместимость удаления полей.** Carry-over между задачами строится в памяти
(`SessionState` → `_task_state_overrides` по `SESSION_STATE_KEYS`); `tasks.json`/`session.json`
только пишутся, обратно в `LoopState` не читаются. Значит, строгий `LoopState.apply()` не
упадёт на старых сессиях — достаточно убрать ключ одновременно из `SESSION_STATE_KEYS`,
`BrowserState` и `to_session_state()`.

### 2.3 Неявные связи между слоями

- `EngineResources`: `llm`, `context`, `events`, `hooks`, `memory` типизированы как `Any`.
- `EngineResources.from_harness(harness: Any)` — duck typing по `harness.tools`,
  `harness.context`, `harness.events` и `getattr(tool_registry, "get_normalizers", list)`.
- `native_task_runner` читает `task_config` с магическими ключами
  (`HARNESS_STATE_OVERRIDES_CONFIG_KEY`, `HARNESS_EVENT_METADATA_CONFIG_KEY`, `turn_cap`) и
  `getattr(session_config, ...)`.
- `TurnController` сам вычисляет `read_only_tools` из списка инструментов (`loop.py:341–345`) —
  правильная идея (знание из MCP-аннотации), но собирается внутри движка, а не приходит
  ресурсом.
- `getattr(` в ключевых модулях: `session.py` — 9, `loop.py` — 4, `goals.py` — 4.

## 3. Целевая форма

Аналогия с `deepagents`, переложенная на наши слои (без LangGraph, движок остаётся нативным):

| deepagents | AutoBrowser |
| --- | --- |
| `create_deep_agent(model, tools, backend, middleware, interrupt_on)` | одна фабрика сборки агента из явных объектов (§ коммит 6) |
| middleware (`before_*`, `wrap_tool_call`) | `HookEngine` + `PermissionEngine` |
| backend (`StateBackend`, `StoreBackend`, `CompositeBackend`) | `MemoryContext` / `MemoryStore` |
| `interrupt_on` | `permissions` + `ApprovalJudge` |
| маршрутизация невидима агенту | **роли инструментов приходят снаружи**, движок не знает имён |

Инварианты, которые ветка закрепляет:

1. `src/agent_loop/execution/` не импортирует `src.browser` и не содержит литералов `browser_`.
2. Знание «что это за инструмент» приходит через `EngineResources` (как уже приходит
   `readOnlyHint`), а не выводится из имени.
3. Без браузерного классификатора движок ничего не кэширует и не инвалидирует — безопасный
   дефолт для любых MCP-серверов.
4. Поля `EngineResources` типизированы протоколами из `src/contracts.py`, не `Any`.
5. Задача передаётся в runner явным объектом, а не словарём с магическими ключами.

## 4. Коммиты

Порядок выбран так, чтобы любой сдвиг метрик эвалов указывал на один коммит. После каждого —
`python -m pytest` и `scripts/run_evals.py --baseline tests/evals/baselines/agent_loop_v1.json`.

### Коммит 1 — `chore: drop the autobrowser package stub`

- Удалить `src/autobrowser/`.
- `pyproject.toml:130`: убрать entry point или направить на реальную точку входа CLI
  (решение — по тому, ставится ли пакет через `pip install -e .`; проверить `[tool.*]`-секции
  на ссылки на пакет).
- Удалить локальный `execution/__pycache__/policy.cpython-312.pyc`.

### Коммит 2 — `refactor(engine): remove the dead tab-activation path`

- Удалить `_pending_tab_activation_request`, алиас, экспорт из `guards.__all__` и
  `execution/__init__.py`, импорт в `loop.py:58`; удалить `TABS_TOOL` из импортов движка.
- Удалить ветку `TABS_TOOL` в `BrowserStateReducer` (`observation.py:199–201`).
- Удалить `pending_browser_tab_index`/`_reason` из `BrowserState` (`execution/state.py`),
  `to_session_state()`/второго сериализатора, `SESSION_STATE_KEYS` (`session.py`),
  `src/state.py`, кортежа в `context.py:307`.
- `TABS_TOOL` в `src/browser/names.py` оставить, только если он ещё где-то нужен; иначе удалить.
- Попутно сверить ключи `TASK_BOUNDARY_RESETS` (`policy_decision`, `policy_event`) с полями
  `LoopState`: если это остатки удалённого `execution/policy.py`, удалить в этом же коммите.
- Тесты: поправить те, что упоминают удалённые поля; добавить проверку, что
  `SESSION_STATE_KEYS ⊆` ключей, которые принимает `LoopState.apply()`.

### Коммит 3 — `refactor(engine): inject tool roles instead of browser names`

- В `src/contracts.py` — нейтральный протокол (рабочее имя `ToolRoles`):
  - `is_observation(name) -> bool` — успешный вызов даёт наблюдаемое состояние (snapshot);
  - `is_stateful(name) -> bool` — инструмент относится к среде, чьё состояние кэшируется
    (успешный не-read-only вызов инвалидирует snapshot).
- Дефолт `NullToolRoles` (всё `False`) рядом с протоколом или в `src/harness/` — не в
  `src/browser/` и не в движке-импортёре браузера.
- Браузерная реализация в `src/browser/` (например, `src/browser/roles.py`) поверх
  `src/browser/names.py`.
- `EngineResources`: новое поле `tool_roles` с дефолтом `NullToolRoles`, плюс перенос
  `read_only_tools` из `TurnController` в ресурсы (одно место, где собирается знание об
  инструментах). `from_harness` берёт роли у `BrowserHarness`.
- `ToolResultNormalizer` / `BrowserStateReducer` / `ObservationCompiler` получают роли через
  конструктор; поле `is_browser_tool` переименовать в `is_stateful` (имя поля — часть
  нейтральности).
- Все пути сборки получают роли явно: `SessionRuntime` (боевой), `src/agent_loop/evals.py`,
  `scripts/run_evals.py`, `scripts/run_batch.py`, тестовые фабрики.

### Коммит 4 — `test: fence the engine off the browser layer`

- `tests/test_engine_boundaries.py` по образцу `tests/test_memory_boundaries.py`:
  параметризованно по `src/agent_loop/execution/*.py` — нет `src.browser` в импортах (через
  `ast`), нет литерала `browser_` в коде (docstring'и — только по явному allowlist, после
  коммита 7 он должен быть пустым).
- В тесте сессии — assert, что боевая сборка (`SessionRuntime` → `EngineResources`) получает
  браузерную реализацию `ToolRoles`, а не `NullToolRoles` (защита от тихой деградации).

### Коммит 5 — `refactor(agent-loop): move browser naming out of context and evals`

- `context.py:23`: признак «браузерного контекста» — через те же роли (или через
  инъецируемый предикат в `ContextAssembler`), без импорта `src.browser.names`.
- `evals.py:24`: `_FakeBrowserTools` получает браузерные роли явно.
- `src/cli/agent_cli.py:22` (`SNAPSHOT_TOOL`) — CLI вправе знать браузер, оставить; отметить
  в ADR.
- `prompts.py` не трогать (правило проекта); если граница теста расширяется на весь
  `src/agent_loop/`, `prompts.py` и `skills.py` — в allowlist.

### Коммит 6 — `refactor: explicit agent objects` (только после согласования ADR)

Отдельный шаг, потому что затрагивает всю цепочку
`SessionRuntime → GoalRunner → native_task_runner → AgentLoopEngine`. Перед кодом —
ADR с целевыми сигнатурами на ревью у пользователя. Черновик направлений:

- **Протоколы вместо `Any`** в `src/contracts.py`: `PromptContext` (для `ContextAssembler`),
  `EventSink`, `HookRunner`, `MemoryRenderer`; `llm` — уже существующий `ChatModel`.
- **`TaskRequest`** (frozen dataclass): `task`, `task_id`, `goal_id`, `session_id`,
  `state_overrides`, `turn_cap`, `compress_tools` — вместо `task_config` с
  `HARNESS_*_CONFIG_KEY` и `getattr(session_config, ...)`. `GoalRunner` строит его, runner
  принимает его.
- **`EngineResources.from_harness`** принимает типизированный `BrowserHarness` (или протокол
  с `tools`/`context`/`events`/`tool_roles`), без `getattr`.
- **Фабрика агента** — одна точка сборки (`build_agent(...)` или `AgentSpec`), которая из
  модели, реестра инструментов, ролей, хуков, разрешений и памяти собирает готовый runner.
  Её вызывают `SessionRuntime`, evals и batch одинаково — сейчас каждый путь собирает
  ресурсы по-своему.
- Сократить `getattr(` в `session.py`, `loop.py`, `goals.py` до обоснованных случаев.

Если объём окажется большим, коммит 6 разбивается на 6a (протоколы), 6b (`TaskRequest`),
6c (фабрика) — каждый с прогоном эвалов.

### Коммит 7 — `docs: record server-neutral engine and explicit objects`

- Исправить docstring'и `execution/__init__.py` и `execution/tools.py`.
- ADR `docs/decisions/2026-10-17-injected-tool-roles.md`, уточняющий
  `2026-09-28-server-neutral-progress-journal.md` и `2026-09-28-universal-mcp-manager.md`;
  при выполнении коммита 6 — отдельный ADR про явные объекты агента.
- `docs/diagrams/` (`harness-boundaries.md`, `agent-runtime-flow.md`): инъекция ролей в
  `EngineResources`, фабрика агента.
- `docs/glossary.md`: «роли инструментов», `TaskRequest`.
- `CLAUDE.md`: удалить упоминания `pending_browser_tab_*` в описании carry-over, если есть;
  добавить инвариант границы движка.
- `docs/README.md`, `docs/decisions/index.md` — ссылки.

## 5. Вне объёма

- Переименование `BrowserState`/`BrowserStateReducer`/`last_browser_action` в нейтральные
  имена (`EnvironmentState` и т.п.) — логично, но меняет session-state и много кода;
  отдельная ветка.
- Изменение промптов: правила snapshot/ref в них остаются (их ревизует пользователь).
- Новые серверы или поведение snapshot'а — только перенос классификации.

## 6. Критерии приёмки

- `grep -rn "src.browser\|browser_" src/agent_loop/execution/` — пусто.
- `tests/test_engine_boundaries.py` зелёный; assert боевых ролей в тесте сессии зелёный.
- `python -m pytest` зелёный целиком, в том числе `test_harness_session.py`,
  `test_agent_loop_hooks.py`, `test_tool_broker.py`, `test_browser_contracts.py`,
  `test_prompts.py`, `test_config.py`.
- `scripts/run_evals.py --baseline tests/evals/baselines/agent_loop_v1.json` — метрики без
  изменений.
- Ручной прогон `python main.py --show-state` на задаче с переходом по ссылке: snapshot
  инвалидируется после клика и не инвалидируется после read-only инструмента.
- `pip install -e .` не ломается после удаления заглушки.

## 7. Риски

| Риск | Мера |
| --- | --- |
| Тихая деградация: роли не переданы в одном из путей сборки, snapshot перестаёт кэшироваться без ошибок | assert в тесте сессии (коммит 4); фабрика агента (коммит 6) убирает разные пути сборки |
| Удаление полей ломает carry-over | carry-over только in-memory (§2.2); тест `SESSION_STATE_KEYS ⊆ LoopState` |
| Расхождение инвариантов промптов, guards и observer | меняется только способ классификации, не правила; сверка baseline эвалов после коммитов 2, 3, 6 |
| Коммит 6 разрастается | ADR до кода; разбиение на 6a–6c; при необходимости вынос в следующую ветку |
| Новые тюнаблы | не ожидаются; если появятся — только через `src/config.py` + `.env.example` + `config.example.yaml` |
