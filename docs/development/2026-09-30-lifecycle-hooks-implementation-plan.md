# Lifecycle hooks — план реализации

Дата: 2026-09-30, **ред. 2** (сверка с кодом, список правок — §9) · Ветка:
`feat/lifecycle-hooks` · Статус: **Planned**

Основа: [Lifecycle Hooks Research](../research/2026-09-30-lifecycle-hooks-research.md)
(далее — «research»). Продолжает «Phase 6: Hooks» из
[Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md).

## 1. Цель

Добавить в engine-native loop детерминированный `HookEngine`: внешне
конфигурируемые проверки, которые в фиксированных точках жизненного цикла могут
**запретить / эскалировать / переписать** tool call, **переписать результат**,
**добавить контекст** модели и **отклонить преждевременное завершение**.

Целевая форма tool-turn-а (`TurnController._run_tool_turn`):

```text
action.proposed
classify_tool_request                  built-in policy: approved | needs_human | blocked
  blocked ─────────────────────────────► policy_updates → return (как сейчас)
ToolBroker.prepare(request, state)     normalize_request + резолв tool/server
  unknown tool → hook-и пропускаются, error result → observe (как сейчас)
hooks.run(pre_tool_use)                по одному hook.decided на handler
  deny ────────────────────────────────► policy_updates("blocked", reason) → return
  ask  → ветка needs_human
  updated_input → повторный prepare(); смена имени tool-а запрещена
needs_human:
  approval.requested
  hooks.run(permission_request): allow → выполнить; deny → terminal blocked;
                                 нет решения → self._human_input (как сейчас)
tool.started
ToolBroker.invoke(prepared)            invoke + normalize_result
tool.finished
hooks.run(post_tool_use | post_tool_use_failure)
  updated_output → заменяет content/error ДО compile
ObservationCompiler.compile
additional_context → отдельное user-сообщение в messages (НЕ в content tool-а)
```

Завершение (`TurnController._agent_step`):

```text
_classify_action → update с decision="done", completion_status="done"
  hooks.run(stop)   только здесь: ответ модели, не guard-терминал
    deny → update {"decision": "continue", messages + фидбек, stop_blocks + 1}
run_turn: новая ветка decision == "continue" → TurnResult(state) (loop продолжается)
```

## 2. Не цели (вне этого плана)

- Типы hook-ов `command`, `http`, `mcp_tool`, `prompt`, `agent` — только Python
  callables. Контракт проектируется так, чтобы их можно было добавить без ломки (§4.1).
- События `SessionStart`/`SessionEnd`, `PreCompact`, `BeforeModel`/`AfterModel`,
  `BeforeToolSelection`.
- Перенос `classify_tool_request` в «built-in hooks» — остаётся отдельным шагом.
- Интерактивный HITL в CLI — `_deny_human_input` остаётся дефолтом.
- Параллельный запуск hook-ов.
- Новые поля в метриках/экспорте (`test_agent_loop_export.py` фиксирует точный набор ключей).

## 3. Рабочие правила

- **С выключенными hook-ами поведение байт-в-байт прежнее**: `pytest` зелёный,
  eval baseline `tests/evals/baselines/agent_loop_v1.json` не меняется, в
  `events.jsonl` нет новых записей, payload существующих событий не меняется.
- Каждый коммит — одна граница; рефакторинг (`ToolBroker`) отдельно от нового поведения.
- Слои по `CLAUDE.md`: контракты — нейтральный лист `src/contracts.py`; движок
  hook-ов — в `src/harness/`; браузерные handler-ы — в `src/browser/`; loop только
  вызывает и сам эмитит события.
- Hook никогда не получает и не возвращает `LoopState`; движок сам переводит
  `HookOutcome` в `LoopState.apply(...)`.
- **Тесты не читают hook-и из `get_settings()`.** В `tests/` нет `conftest.py`, а
  `config.yaml` в корне репозитория (личный профиль) автоматически подхватывается
  любым `get_settings()`. Все тесты строят `HookEngine` из явного `HooksSettings(...)`;
  `BrowserHarness` и evals hook-и из настроек не читают вообще (§4.6).

## 4. Решения, зафиксированные планом

### 4.1 Контракт

```python
# src/contracts.py (нейтральный лист: только stdlib)
HookEventName = Literal[
    "goal_start", "pre_tool_use", "permission_request",
    "post_tool_use", "post_tool_use_failure", "stop", "goal_end",
]
HookDecision = Literal["allow", "deny", "ask"]

@dataclass(frozen=True)
class HookEvent:
    name: HookEventName
    session_id: str | None
    goal_id: str
    task_id: str
    task: str
    tool: str = ""                 # exposed name после нормализации
    server: str = ""               # MCPTool.server; "" для не-MCP tool-ов (FakeBrowserProvider, Tool)
    args: dict[str, Any] = field(default_factory=dict)      # копия, не ссылка на state
    result: dict[str, Any] = field(default_factory=dict)    # ToolResult (post_*)
    reason: str = ""               # причина built-in needs_human (permission_request)
    final_answer: str = ""         # stop
    evidence: tuple[str, ...] = () # stop: state.observation + state.browser.snapshot
    stop_hook_active: bool = False
    status: str = ""               # terminal status (goal_end)

@dataclass(frozen=True)
class HookResult:
    decision: HookDecision | None = None
    reason: str = ""               # видно модели
    updated_input: dict[str, Any] | None = None   # pre_tool_use
    updated_output: str | None = None             # post_tool_use*
    additional_context: str = ""   # отдельное сообщение модели
    user_message: str = ""         # только в события / CLI

HookHandler = Callable[[HookEvent], Awaitable[HookResult | None]]
```

- `None` от handler-а — «нет мнения» (как в ADK).
- Поля — только `dict`/`tuple`/скаляры, чтобы `json.dumps(dataclasses.asdict(event))`
  работал и будущий `command`-hook получил тот же объект через stdin.
- `evidence` собирает **движок** (он владеет `BrowserState`); контракт остаётся
  нейтральным «тексты-доказательства». Tool-сообщения из `messages` не годятся:
  `compact_snapshot_history` (`src/harness/memory.py`) заменяет старые выводы
  заглушками, а история общая для всех задач сессии.

### 4.2 Семантика решений по событиям

| Событие | `decision` | `updated_*` | `additional_context` | при timeout/исключении |
|---|---|---|---|---|
| `goal_start` | `deny` — задача не запускается, `blocked`, модель не вызывается | — | ✅ после «User request» | **deny** |
| `pre_tool_use` | `deny`; `ask` (→ needs_human); `allow` = «нет возражений» | `updated_input` | ✅ | **deny** |
| `permission_request` | `allow` — выполнить без человека; `deny` — **terminal `blocked`**, как отказ человека | — | — | нет решения → `human_input` |
| `post_tool_use(_failure)` | игнорируется | `updated_output` | ✅ | нет решения |
| `stop` | `deny` = не завершать, `reason` → фидбек | — | ✅ (в фидбек) | нет решения (done принимается) |
| `goal_end` | игнорируется | — | игнорируется | нет решения |

- `allow` в `pre_tool_use` **не** обходит built-in `needs_human`. Единственный путь
  авто-одобрения — `permission_request` (так же разделяют Claude Code и Codex).
- `permission_request` `deny` — terminal, потому что это замена человека, а отказ
  человека сейчас завершает run (`loop.py`, ветка `needs_human`: `blocked_response` →
  `return state, "blocked"`). Поломка hook-а не должна завершать задачу, поэтому
  при timeout решение переходит к `human_input` (headless — всё равно отказ по
  `_deny_human_input`, интерактивно — человек).
- Явный `fail_closed` в спецификации hook-а переопределяет колонку «при timeout».

### 4.3 Агрегация и выполнение

- Handler-ы события выполняются **последовательно в порядке `hooks.registry`**.
- Первый `deny` — short-circuit, остальные не запускаются.
- `ask` запоминается, выполнение продолжается (более поздний `deny` сильнее);
  итог: `deny > ask > allow > None`.
- `updated_input`/`updated_output` применяются **цепочкой**: следующий handler видит
  уже изменённый `args`/`result`.
- `additional_context` всех handler-ов конкатенируется в порядке выполнения.
- Timeout — `asyncio.wait_for`. Поэтому handler **обязан быть async**:
  `inspect.iscoroutinefunction` (для объекта — его `__call__`) проверяется при
  загрузке, sync-handler — ошибка конфигурации (блокирующий код нельзя прервать).
- `HookEngine` **не владеет `EventEmitter`**. `run(event, on_record=...)` вызывает
  `on_record(HookDecisionRecord)` после каждого handler-а; loop передаёт свой
  `self._emit("hook.decided", ...)`. Причины:
  - `EngineResources.from_harness(..., events=...)` подменяет emitter (так делают
    `SessionRuntime.run_task` и `src/agent_loop/evals.py`), и hook-события должны
    попадать в тот же поток, что и остальные;
  - `GoalRunner._watch_progress` следит за `EventEmitter.sequence`
    (`progress_timeout_seconds`, по умолчанию 120 с) — событие после каждого
    handler-а означает, что длинная цепочка hook-ов не выглядит зависанием.
- При сборке `HookEngine` проверяется: `default_timeout_seconds` и каждый
  `timeout_seconds` < `settings.loop.progress_timeout_seconds`.

### 4.4 Stop только для `done` от модели

`done` рождается в нескольких местах:

- модель: `_classify_action` → `done_response(...)` (виды `answer` и `stop` со
  `status: done`) — **здесь и только здесь** вызывается stop-hook, и только при
  `completion_status == "done"`;
- guard-терминалы: `pre_turn_terminal` (`_terminal_guard`: лимиты replan /
  consecutive failures / stalled plan), unchanged-snapshot в
  `CompletionController.observation_terminal_update`, `blocked_response` — жёсткие
  лимиты, hook их не отменяет.

Реализация — в `_agent_step`, **до** того, как update попадёт в state:

```python
update = self._classify_action(state, actions[0], messages)
if update.get("decision") == "done" and update.get("completion_status") == "done":
    update = await self._stop_check(state, update)
return update
```

Почему не «применить done, а потом откатить»: `_done_response` помечает все шаги
плана `completed` и ставит `current_step = len(plan)` (`_complete_plan_update`);
откат этих полей хрупок. А `decision == ""` в `run_turn` уходит в ветку
«unexpected loop decision» и завершает run как `blocked`.

`_stop_check` при `deny` возвращает **новый** update:

```python
{
    "decision": "continue",
    "stop_blocks": state.stop_blocks + 1,
    # messages из done-update: там уже есть финальный ответ модели (append_final_ai_response)
    "messages": [*update["messages"], user_message(f"[harness] Completion rejected: {reason}\n{ctx}")],
}
```

- `final_answer`, `completion_status`, `plan`, `current_step` в state не попадают.
- `run_turn` получает ветку `if decision == "continue": return TurnResult(state=state)`.
  `decision` в промпт не передаётся (`ContextAssembler` его не читает), в следующий
  turn `_terminal_guard` его игнорирует.
- Бюджет: `stop_blocks >= settings.hooks.max_stop_blocks` → hook не вызывается, `done`
  принимается, эмитится `hook.decided` с `skipped: "stop_budget_exhausted"`.
  `stop_hook_active = stop_blocks > 0`. Каждый продолженный ход расходует `turn_cap`.
- `stop_blocks` — task-local **по построению**, как `action_history`: поля нет в
  `to_session_state()`/`SESSION_STATE_KEYS`, каждая задача стартует с `LoopState`
  по умолчанию. В `TASK_BOUNDARY_RESETS` добавлять не нужно.

### 4.5 Контекст не смешивается с результатом tool-а

- `additional_context` добавляется в `messages` **отдельным** user-сообщением с
  префиксом `[harness]` после `ObservationCompiler.compile`, а не дописывается в
  `result.content`. Причина: `BrowserStateReducer` (`observation.py`) считает
  fingerprint snapshot-а по content (`unchanged_snapshot_count`), `ProgressDetector` —
  успех/ошибку, а `record_action` (`progress.py`) — идентичность результата для
  `max_ineffective_actions`;
  меняющийся хвост сломал бы все три.
- `updated_output`, напротив, заменяет content **до** `state.apply({"tool_result": ...})`,
  чтобы все детекторы видели одну версию. Для `browser_snapshot` это означает, что
  переписанный текст становится `browser.snapshot` — источником ref-ов. Правило для
  ADR: hook, переписывающий snapshot, обязан сохранять строки с `ref=`; встроенные
  hook-и snapshot не переписывают.
- Сообщения `[harness]` — обычная durable-история: они переносятся в следующие
  задачи сессии, как и любые `messages`.

### 4.6 Размещение и время жизни

| Что | Где |
|---|---|
| `HookEventName`, `HookDecision`, `HookEvent`, `HookResult`, `HookHandler` | `src/contracts.py` |
| `HooksSettings`, `HookSpec`, `HookMatch` | `src/config.py` (импорт `HookEventName` из `src.contracts` допустим: оба — нейтральные листья, `contracts` ничего не импортирует из `config`) |
| `HookEngine`, `NullHookEngine`, `HookOutcome`, `HookDecisionRecord`, загрузка, matcher | `src/harness/hooks.py` |
| Общие handler-ы (`approve_tools`, `grounded_final_answer`) | `src/harness/builtin_hooks.py` |
| Браузерные handler-ы (`url_policy`, `prompt_injection_scan`) | `src/browser/hooks.py` |
| `PreparedToolCall`, `ToolBroker.prepare/invoke` | `src/agent_loop/execution/tools.py` |
| Точки вызова, эмиссия `hook.decided` | `src/agent_loop/execution/loop.py` |
| Время жизни `HookEngine` | **сессия**: строится в `SessionContext.start` (`src/harness/session.py`) из `get_settings().hooks`, ошибки конфигурации — ошибка старта сессии |
| Проброс | `EngineResources.hooks` (`field(default_factory=NullHookEngine)`); `from_harness(..., hooks=None)` по образцу `events=`; `SessionRuntime.run_task` передаёт `hooks=self.context.hooks` |

Почему не `BrowserHarness`: его строят и сессия (`harness_factory(...)` с
фиксированным набором kwargs, в тестах — `FakeHarness`), и `src/agent_loop/evals.py`,
и тесты через `BrowserHarness()`. Дефолт «из настроек» в `BrowserHarness`
притащил бы hook-и из личного `config.yaml` в evals и тесты. Evals и тесты, не
передающие `hooks=`, получают `NullHookEngine` при любом `config.yaml`.

### 4.7 Конфигурация

```python
# src/config.py
class HookMatch(_Section):
    server: str = ""   # exact
    tool: str = ""     # re.fullmatch; "a|b" работает как список (точку в имени экранировать)

class HookSpec(_Section):
    id: str
    event: HookEventName
    handler: str                       # "package.module:attr"
    match: HookMatch = Field(default_factory=HookMatch)
    options: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    fail_closed: bool | None = None    # None → колонка «при timeout» из §4.2

class HooksSettings(_Section):
    enabled: bool = False
    max_stop_blocks: int = 2
    default_timeout_seconds: float = 10.0
    registry: list[HookSpec] = Field(default_factory=list)
```

- `Settings.hooks: HooksSettings` — девятая **секция** (рядом с корневыми
  не-секциями `mcp_servers` и `browser_mcp_server`). YAML-источник принимает её
  автоматически: список допустимых секций — `Settings.model_fields`.
- **Все четыре поля документируются в `.env.example`**, включая
  `# AUTOBROWSER_HOOKS__REGISTRY=[]`: `test_env_example_documents_every_setting`
  требует каждое поле каждой секции, а `test_env_example_values_are_the_actual_defaults`
  — чтобы раскомментированное значение воспроизводило дефолт (`[]` разбирается
  pydantic-settings как JSON).
- В `config.example.yaml` секция `hooks:` — только с дефолтами; пример `registry`
  остаётся **закомментированным**: `test_the_example_config_file_reproduces_the_defaults`
  требует, чтобы дословная копия файла совпадала с дефолтами.
- `handler` резолвится `importlib` при старте сессии: ошибка импорта, дубликат `id`,
  sync-handler — ошибка запуска, а не тихий пропуск.
- Если `options` не пуст, `handler` — фабрика: один раз `handler(**options)` →
  `HookHandler`. Параметры handler-ов настраиваются через `options`, новых полей
  `Settings` под них не заводится.
- Валидатор `HookSpec`: `match` допустим только для tool-событий
  (`pre_tool_use`, `permission_request`, `post_tool_use*`).

## 5. Коммиты

Каждый коммит: `python -m pytest` зелёный; `.\.venv\Scripts\ruff check .` чистый
(ruff есть в `.venv`, но не в `requirements.txt`).

### Коммит 0 — ADR

- `docs/decisions/2026-09-30-lifecycle-hooks-engine.md` по `adr-template.md`:
  решения §4.1–4.6; отвергнутые альтернативы: event bus, параллельный запуск,
  вердикт `MODIFY`, hook-и в MCP Manager, авто-одобрение через `pre_tool_use`,
  `HookEngine` в `BrowserHarness`, собственный emitter у `HookEngine`.
- Строка в `docs/decisions/index.md`.

### Коммит 1 — контракты и настройки (без поведения)

- `src/contracts.py`: типы §4.1 + `__all__`.
- `src/config.py`: `HookMatch`, `HookSpec`, `HooksSettings`, `Settings.hooks`,
  экспорт в `__all__`.
- `.env.example`: четыре закомментированные строки секции `hooks` (§4.7).
- `config.example.yaml`: секция `hooks` с дефолтами + закомментированный пример `registry`.

Тесты:
- `tests/test_config.py`: дефолты секции; env-переопределение скаляров; `registry`
  из YAML; опечатка в `HookSpec` → ошибка (`extra="forbid"`); валидатор `match`;
  существующие drift-тесты `.env.example`/`config.example.yaml` проходят.
- `tests/test_contracts.py` (новый; такого теста сейчас нет):
  `json.dumps(asdict(HookEvent(...)))` работает; AST-проверка, что `src/contracts.py`
  импортирует только stdlib.

### Коммит 2 — `ToolBroker.prepare` / `invoke` (чистый рефакторинг)

```python
@dataclass(frozen=True)
class PreparedToolCall:
    request: ToolRequest            # после normalize_request
    tool: Any | None                # None → unknown tool / пустое имя
    server: str                     # getattr(tool, "server", "") or ""
    error_result: ToolResult | None = None   # готовый "No tool request" / unknown-tool результат

class ToolBroker:
    async def prepare(self, request, state=None) -> PreparedToolCall   # await registry.get() + normalizers
    async def invoke(self, prepared: PreparedToolCall) -> ToolResult    # invoke + normalize_result; error_result → только normalize
    async def execute(self, request, state=None) -> ToolResult          # = invoke(await prepare(...))
```

- `execute` остаётся композицией — текущие вызовы и тесты не меняются.
- Отдельного `renormalize` нет: нормализация требует `tools_by_name` из
  `await self._registry.get()`. Для `updated_input` loop вызывает
  `prepare({**prepared.request, "args": updated}, state)` ещё раз; если имя
  изменилось — это ошибка hook-а (§ коммит 5). Normalizers идемпотентны
  (`BrowserToolNormalizer`: точное имя резолвится само в себя, фильтр по схеме
  повторно ничего не меняет; `FakeBrowserProvider`: `setdefault`).

Тесты (`tests/test_tool_broker.py`, новый):
- `execute` ≡ `invoke(prepare())` (обычный `Tool` и `MCPTool` на fake MCP server);
- `prepare` отдаёт `server` для `MCPTool` и `""` для `Tool`;
- пустое имя / unknown tool → `error_result`, `invoke` его только нормализует;
- повторный `prepare` уже нормализованного запроса ничего не меняет.

### Коммит 3 — `HookEngine` (изолированно)

`src/harness/hooks.py`:

```python
@dataclass(frozen=True)
class HookDecisionRecord:
    hook_id: str
    event: HookEventName
    decision: HookDecision | None
    reason: str
    modified: bool
    duration_ms: int
    error: str = ""                  # "timeout" | "<ExcType>: msg"
    skipped: str = ""

@dataclass(frozen=True)
class HookOutcome:
    decision: HookDecision | None     # агрегированное: deny > ask > allow > None
    reason: str
    updated_input: dict[str, Any] | None
    updated_output: str | None
    additional_context: str
    records: tuple[HookDecisionRecord, ...]

class HookEngine:
    @classmethod
    def from_settings(cls, hooks: HooksSettings, *, progress_timeout_seconds: float) -> HookEngine | NullHookEngine
    def has(self, name: HookEventName) -> bool       # быстрый путь: без построения HookEvent
    async def run(self, event: HookEvent, *, on_record: Callable[[HookDecisionRecord], None] | None = None) -> HookOutcome
```

- `NullHookEngine`: `has()` → `False`, `run()` → пустой outcome; при
  `enabled=False` loop не строит `HookEvent` вообще.
- Matcher: пустой `match` — всё; `server` — exact; `tool` — `re.fullmatch`,
  компилируется при загрузке.
- Агрегация и fail-семантика — строго по §4.2–4.3.

Тесты `tests/test_harness_hooks.py` (только явные `HooksSettings(...)`):
- matcher: exact, `a|b`, regex, server+tool, пустой; не-tool событие игнорирует `match`;
- порядок = порядок registry; `deny` останавливает цепочку; `ask` + поздний `deny` → `deny`;
- цепочка `updated_input` / `updated_output`; конкатенация `additional_context`;
- timeout и исключение → по колонке §4.2 для каждого события; явный `fail_closed`
  переопределяет;
- `on_record` вызывается по разу на запущенный handler, в порядке;
- фабрика с `options`; ошибка импорта, дубликат `id`, sync-handler, timeout ≥
  `progress_timeout_seconds` → исключение в `from_settings`;
- `enabled=False` → `NullHookEngine`.

### Коммит 4 — событие `hook.decided` в телеметрии

- `src/agent_loop/events.py`: `"hook.decided"` в `EventType` и в
  `AGENT_TRACE_EVENT_TYPES`; ветка в `_project_agent_trace_record` (`hook_id`,
  `event`, `decision`, `reason`, `tool`).
- Payload (строит loop в коммите 5): `hook_id`, `event`, `tool`, `server`,
  `decision`, `reason`, `modified`, `duration_ms`, `error`, `skipped`. **Без `args`
  и `result`.** Про редакцию: `EventRecord.to_dict` уже прогоняет payload через
  `redact_json_safe`, но тот скрывает значения только по **имени ключа**
  (`token`, `password`, …) и не сканирует строки. Поэтому защита — в отсутствии
  аргументов в payload; в ADR — правило «reason не цитирует значения аргументов».
- `src/agent_loop/metrics.py` и `src/agent_loop/replay.py`: `hook.decided` с
  `decision == "deny"` на `pre_tool_use` засчитывается в **существующий**
  `policy_block_count` (с точки зрения задачи это блок политики; схема экспорта и
  eval-assertion `max_policy_blocks` не меняются). `_is_error_event` менять не нужно:
  он смотрит только `goal.failed` и `tool.finished`.
- `replay.py`: строка для `hook.decided` в выводе действий.
- Докстринг модуля `loop.py` («only existing EventType literals are emitted»)
  дополнить `hook.decided`.

Тесты: `tests/test_agent_loop_events.py` (проекция в agent trace, отсутствие `args`),
`tests/test_agent_loop_replay.py` и `tests/test_agent_loop_metrics.py` (deny
считается в `policy_block_count`, `allow` — нет).

### Коммит 5 — проброс, `pre_tool_use`, `post_tool_use`

Проброс:
- `EngineResources.hooks` + параметр `hooks=` у `from_harness` (дефолт —
  `NullHookEngine()`); обновить `test_engine_resources_from_harness_*` в
  `tests/test_harness_runtime.py`.
- `SessionContext.start`: `self.hooks = HookEngine.from_settings(settings.hooks,
  progress_timeout_seconds=settings.loop.progress_timeout_seconds)`;
  `SessionRuntime.run_task`: `EngineResources.from_harness(..., hooks=self.context.hooks)`.
- `SessionContext.snapshot()` (→ `session.json`): аддитивное поле
  `"hooks": {"enabled": ..., "registry_sha256": ...}` (sha256 от канонического JSON
  `registry`). Тесты экспорта пишут `session.json` сами и от него не зависят.
- `src/agent_loop/evals.py` не меняется — evals без hook-ов.

`TurnController._run_tool_turn` по схеме §1:
- `pre_tool_use` вызывается после built-in `classify_tool_request` при `approved`
  **и** при `needs_human` (идея pre-approval guardrails из Agents SDK: человеку не
  показываем заведомо запрещённое); при `blocked` — не вызывается.
- `deny` → `state.apply(policy_updates(state, "blocked", reason))` — тот же путь, что
  built-in блок: tool-message с причиной, `consecutive_failures + 1`, `policy_event`;
  затем `return state, None`.
- `ask` при built-in `approved` → `policy_updates(state, "needs_human", reason)` и
  ветка `needs_human`.
- `updated_input` → повторный `prepare`; если имя изменилось → `deny` с причиной
  «Hook <id> cannot change the tool name». Иначе `state.tool_request` обновляется
  на выполняемый запрос (чтобы `action_history` и identical-outcome policy видели
  реальные аргументы); assistant-сообщение в истории остаётся как предложила модель.
- `tool.started` несёт тот же `request`, что и сейчас (как предложила модель); только
  если hook изменил аргументы — `{**request, "args": updated}`. С выключенными
  hook-ами payload не меняется.
- `post_tool_use` при `result["status"] == "success"`, иначе `post_tool_use_failure`;
  для unknown tool (`prepared.tool is None`) post-hook-и не вызываются.
  `updated_output` заменяет `content` (для failure — `error`) до
  `state.apply({"tool_result": result})`.
- `additional_context` (pre + post) → одно сообщение `[harness]` после compile (§4.5).

Тесты `tests/test_agent_loop_hooks.py`. Инструменты: `FakeChatModel`
(`src/agent_loop/evals.py`, скриптованные ответы) + `FakeBrowserProvider`
(`src/browser/fake.py`) или обычный `Tool` со счётчиком вызовов; для `server`-matcher-а
— `MCPTool` на `tests/mcp_fixtures/fake_server.py` (tool `increment`). Tool-ы
`FakeBrowserProvider` — не `MCPTool`, их `server == ""`.
- deny → tool не вызван, причина в последнем tool-message, `consecutive_failures == 1`;
- deny на каждом ходу → `_terminal_guard` завершает run по
  `max_consecutive_failures` (`blocked`);
- `ask` → вызывается `human_input`; отказ → terminal `blocked`;
- `updated_input` доходит до tool-а, `state.tool_request` и `action_history`
  содержат изменённые аргументы; смена имени → блок;
- `updated_output` виден в observation и `action_history`;
- `additional_context` — отдельное сообщение; content tool-а не изменён;
  `unchanged_snapshot_count` растёт как без hook-а;
- built-in `blocked` (повторный snapshot) → `pre_tool_use` не вызывается;
- `hook.decided` по разу на handler, в том же emitter-е, что `tool.started`;
- `NullHookEngine` → список `EventRecord.type` и payload-ы идентичны текущим.

### Коммит 6 — `permission_request`

- В ветке `needs_human` после `approval.requested`: если
  `hooks.has("permission_request")` → `allow` — выполнить без `human_input`;
  `deny` — `blocked_response(state, f"Blocked: approval hook denied {name}: {reason}")`
  и `return state, "blocked"` (как отказ человека); нет решения (в т.ч. timeout) →
  `self._human_input` как раньше.
- `src/harness/builtin_hooks.py: approve_tools(tools: list[str], servers: list[str] = [])`
  — фабрика, одобряющая перечисленные tool-ы (batch/eval-профили).

Тесты: allow без вызова `human_input`; deny → terminal `blocked`; нет решения и
timeout → вызывается `human_input`.

### Коммит 7 — `stop`

- `LoopState.stop_blocks: int = 0` (попадает в `_LOOP_STATE_FIELDS` автоматически;
  `SESSION_STATE_KEYS`/`TASK_BOUNDARY_RESETS` не трогаем — §4.4).
- `_agent_step`: `_stop_check` после `_classify_action` (§4.4); `run_turn`: ветка
  `decision == "continue"`.
- `HookEvent` для `stop`: `final_answer` из update, `evidence = (state.observation,
  state.browser.snapshot)` (пустые строки отбрасываются), `task`, `stop_hook_active`.
- `src/harness/builtin_hooks.py: grounded_final_answer(min_chars=1)` —
  детерминированная проверка:
  - ответ не короче `min_chars`;
  - числа из ответа (цены, количества) встречаются в `evidence`; перед сравнением
    из чисел убираются пробелы, NBSP и узкие пробелы между разрядами
    (`1 299 ₽` ≡ `1299`), `,`/`.` в дробной части приравниваются;
  - числа, которые есть в `task`, не проверяются (`«найди 3 товара»`);
  - иначе `deny` с перечнем неподтверждённых значений.

Тесты:
- deny → loop продолжается; в state нет `final_answer`/`completion_status`, план не
  помечен выполненным; в `messages` — финальный ответ модели и фидбек `[harness]`;
- следующий `done` после исправления принимается;
- бюджет `max_stop_blocks` → `done` принимается, `hook.decided` со `skipped`;
- guard-терминалы (unchanged snapshot, лимит replan) и `stop` модели со статусом
  `blocked`/`cancelled` → stop-hook не вызывается;
- `turn_cap` ограничивает run при постоянном deny;
- `stop_blocks == 0` в начале второй задачи сессии (`tests/test_harness_session.py`);
- unit-тесты `grounded_final_answer` (форматы чисел, числа из задачи).

### Коммит 8 — `goal_start` / `goal_end`

- В `AgentLoopEngine.run`, не в `GoalRunner` (он по контракту не трогает state и
  решения).
- `goal_start` — после сборки начального state, до `_run_plan`:
  - `deny` → `state.apply(blocked_response(state, f"Blocked: {reason}"))` и
    `AgentLoopResult(status="blocked", ..., turns=0)`; модель не вызывается;
  - `additional_context` → сначала `messages = self._history(state)` (он добавляет
    «User request (task-…)»), затем `[*messages, user_message("[harness] …")]`.
    Иначе `ensure_message_history` в `_run_plan` допишет запрос пользователя **после**
    контекста.
- `goal_end` — после выхода из цикла, перед `return AgentLoopResult(...)`;
  наблюдательный: исключения/таймауты только в `hook.decided`. Вызывается только при
  нормальном терминальном результате: исключения движка, отмена и таймауты
  `GoalRunner` его не вызывают (для них есть `goal.failed`/`goal.cancelled`).

Тесты: deny не делает ни одного вызова `FakeChatModel`; контекст стоит после
«User request» и до plan-запроса; `goal_end` получает итоговый статус; исключение
в `goal_end` не меняет результат.

### Коммит 9 — браузерные hook-и

`src/browser/hooks.py`:

- `url_policy(allow_domains=(), deny_domains=(), deny_schemes=("file", "chrome",
  "javascript", "data"))` — для `pre_tool_use`, `match.tool: browser_navigate`
  (плюс другие tool-ы каталога Playwright MCP, принимающие URL, — сверить со схемой
  на момент реализации). Разбор `args["url"]` через `urllib.parse`, сравнение по
  суффиксу хоста; непарсящийся URL → `deny`. Ограничение: навигацию через
  `browser_evaluate` (`location = …`) или клик по ссылке hook не видит — это
  guardrail, а не граница безопасности (в ADR).
- `prompt_injection_scan(patterns=(...))` — для `post_tool_use`,
  `match.tool: browser_snapshot`; детерминированные регэкспы («ignore previous
  instructions», «system prompt», «you are now» и русские аналоги); **не** меняет
  content (§4.5), возвращает `additional_context` с предупреждением, что текст
  страницы — недоверенные данные.

Тесты `tests/test_browser_hooks.py`: таблицы URL (разрешён / запрещён / схема /
поддомен / мусор); сработавшие и несработавшие паттерны; content не изменён.

### Коммит 10 — документация и проверка

- `docs/diagrams/agent-runtime-flow.md`: hook-точки в turn и ветка `continue`;
  `harness-boundaries.md`: `HookEngine` в `SessionContext` → `EngineResources`;
  строка в `docs/diagrams/index.md`.
- `CLAUDE.md`: «eight sections» → «nine sections» (+ `hooks`), раздел про hooks
  (где живут, hook не отменяет built-in policy, disabled by default, тесты не
  читают hook-и из настроек). `AGENTS.md`: команды
  `python -m pytest tests\test_harness_hooks.py tests\test_agent_loop_hooks.py tests\test_browser_hooks.py`.
- `docs/research/index.md`: статус research → «implemented» после мёржа.
- `docs/glossary.md`: hook, hook event, handler, fail-closed.
- Прогон: полный `pytest`; `python scripts/run_evals.py --baseline
  tests\evals\baselines\agent_loop_v1.json` (без изменений — evals без hook-ов);
  ручной `python main.py --show-state` с `url_policy` + `grounded_final_answer` на
  задаче поиска — проверить отсутствие циклов.

## 6. Критерии готовности

- [ ] Без hook-ов (`enabled=false` или `NullHookEngine`) полный `pytest` и eval
      baseline без изменений; payload-ы существующих событий не меняются.
- [ ] `pre_tool_use` deny не доходит до tool-а; причина видна модели.
- [ ] Hook не может снять built-in `blocked` и не может одобрить `needs_human`
      иначе как через `permission_request`.
- [ ] Stop-hook не зацикливает run (`max_stop_blocks`, `turn_cap`) и не срабатывает на
      guard-терминалах.
- [ ] Упавший/зависший hook не роняет задачу; поведение по §4.2.
- [ ] Каждое решение hook-а есть в `events.jsonl` и `agent_trace.jsonl` как
      `hook.decided`, без аргументов; `replay_trace.py` его показывает.
- [ ] Ошибки конфигурации hook-ов видны при старте сессии.
- [ ] Диаграммы, ADR, `CLAUDE.md`/`AGENTS.md` обновлены.

## 7. Риски

| Риск | Митигирующее решение |
|---|---|
| Hook ломает snapshot/ref-инварианты | built-in policy до hook-ов и не отключается; `updated_input` проходит normalizers; имя tool-а менять нельзя; правило про `ref=` для переписанного snapshot (§4.5) |
| Контекст/перезапись ломает детекцию прогресса | `additional_context` отдельным сообщением; `updated_output` до apply |
| Бесконечное продолжение через `stop` | `max_stop_blocks`, `turn_cap`, только model-`done` |
| Утечка секретов (`browser_type` с паролем) в события | `hook.decided` без `args`; key-based редакция `EventRecord.to_dict`; правило для `reason` |
| Watchdog принимает цепочку hook-ов за зависание | `hook.decided` после каждого handler-а; timeout handler-а < `progress_timeout_seconds` |
| Недетерминизм в evals | последовательный порядок; evals не читают hook-и из настроек |
| Личный `config.yaml` влияет на тесты | тесты строят `HookEngine` только из явных `HooksSettings` |
| Обход `url_policy` через `browser_evaluate`/клики | задокументировано как ограничение; hook — guardrail |
| Выполнение произвольного кода из конфига | только `config.yaml`/`AUTOBROWSER_CONFIG_FILE` (git-ignored); по умолчанию выключено; хэш `hooks.registry` пишется в `session.json` |

## 8. После плана (backlog)

1. `command`-hook: JSON `HookEvent` в stdin, ответ в формате Claude Code
   (`hookSpecificOutput.permissionDecision` …), exec-форма без shell на Windows.
2. `mcp_tool`-hook: handler = вызов tool-а зарегистрированного MCP-сервера через
   `MCPManager` (внешний policy-сервер).
3. `SessionStart`/`SessionEnd` в `SessionRuntime`; `PreCompact` вокруг
   `compact_snapshot_history` в `src/harness/memory.py`.
4. `BeforeToolSelection` против зацикливания на search-affordance (открытый вопрос
   research §6).
5. Hook-и в eval-сценариях (поле в YAML-сценарии → явный `HookEngine` в `run_scenario`).
6. Перевести ли `classify_tool_request` в built-in hook-и с `removable=False`.

## 9. Правки ред. 2 (сверка с кодом)

| # | Было в ред. 1 | Что в коде | Исправление |
|---|---|---|---|
| 1 | Stop-deny ставит `decision: ""` поверх применённого done | `run_turn` считает неизвестный `decision` терминальным `blocked`; `_done_response` уже пометил план выполненным | stop-hook в `_agent_step` до apply; новый `decision: "continue"` и ветка в `run_turn` (§4.4) |
| 2 | Рефакторинг `_agent_step` → `AgentStepOutcome(origin)` | model-`done` рождается только в `_classify_action` | проверка сразу после `_classify_action`, без смены типа |
| 3 | «built-in `allow`» | `PolicyDecision`: `approved` / `needs_human` / `blocked` | терминология `approved` |
| 4 | `HookEngine` эмитит события сам через `events` из `from_settings` | `from_harness(events=...)` подменяет emitter (session, evals); watchdog следит за `sequence` | `on_record`-callback, эмитит loop через `self._emit` (§4.3) |
| 5 | `HookEngine` — дефолт `BrowserHarness` из `get_settings()` | нет `tests/conftest.py`, корневой `config.yaml` читается тестами; evals строят `BrowserHarness` сами | `HookEngine` живёт в `SessionContext`, проброс через `from_harness(hooks=)` (§4.6) |
| 6 | `registry` не документируется в `.env.example` | `test_env_example_documents_every_setting` требует каждое поле секции | `# AUTOBROWSER_HOOKS__REGISTRY=[]`; пример в YAML закомментирован (§4.7) |
| 7 | sync `renormalize(prepared, args, state)` | нормализации нужен `await registry.get()` | повторный `prepare` + проверка имени |
| 8 | `tool.started` несёт финальный запрос | сейчас — запрос модели до нормализации | payload прежний; меняется только при `updated_input`; `state.tool_request` обновляется |
| 9 | `stop_blocks` в `TASK_BOUNDARY_RESETS` | поле не переносится между задачами (как `action_history`) | не трогать `TASK_BOUNDARY_RESETS` |
| 10 | `grounded_final_answer` читает tool-сообщения задачи | их нет в `HookEvent`; `compact_snapshot_history` их сжимает; история общая на сессию | поле `evidence` (observation + snapshot), нормализация чисел, исключение чисел из задачи |
| 11 | `goal_start` контекст «до плана» | `ensure_message_history` допишет «User request» после него | сначала `_history(state)`, потом контекст |
| 12 | `permission_request` deny/timeout — «blocked», fail-closed | отказ человека — terminal `blocked` | deny — terminal (как человек); timeout → `human_input` |
| 13 | `goal_end` — «после цикла» без оговорок | исключения/отмена/таймауты обрабатывает `GoalRunner` | только при нормальном терминальном результате |
| 14 | «строки проходят `redact_json_safe`» | редакция только по имени ключа, уже встроена в `EventRecord.to_dict` | защита — отсутствие `args` в payload + правило для `reason` |
| 15 | «проверить `_is_error_event`» | смотрит только `goal.failed`/`tool.finished` | не менять; deny считается в существующий `policy_block_count` (схема экспорта фиксирована) |
| 16 | — | `AGENT_TRACE_EVENT_TYPES` фильтрует `agent_trace.jsonl`; докстринг `loop.py` перечисляет разрешённые события | добавить `hook.decided` в оба |
| 17 | `asyncio.wait_for` для любого handler-а | sync-код отменить нельзя | только async handler-ы, проверка при загрузке |
| 18 | «скриптованный ChatModel», «счётчик fake-сервера» без указания | есть `FakeChatModel`, `FakeBrowserProvider` (не `MCPTool`, `server == ""`), `increment` в fake MCP server | инструменты тестов названы явно |
| 19 | «девять секций» в `CLAUDE.md` и `AGENTS.md` | счётчик секций есть только в `CLAUDE.md` | правка `CLAUDE.md`; в `AGENTS.md` — команды тестов |
| 20 | `ruff check .` | ruff есть в `.venv`, но не в `requirements.txt` | `.\.venv\Scripts\ruff check .` |
| 21 | — | timeout handler-а может превысить `progress_timeout_seconds` (120 с) | валидация при сборке `HookEngine` |
| 22 | — | переписанный snapshot становится `browser.snapshot`; `url_policy` не видит навигацию через `browser_evaluate` | правило про `ref=`; ограничение в ADR и рисках |
