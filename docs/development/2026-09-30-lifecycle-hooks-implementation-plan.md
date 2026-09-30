# Lifecycle hooks — план реализации

Дата: 2026-09-30 · Ветка: `feat/lifecycle-hooks` · Статус: **Planned**

Основа: [Lifecycle Hooks Research](../research/2026-09-30-lifecycle-hooks-research.md)
(далее — «research»). Продолжает «Phase 6: Hooks» из
[Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md).

## 1. Цель

Добавить в engine-native loop детерминированный `HookEngine`: внешне
конфигурируемые проверки, которые в фиксированных точках жизненного цикла могут
**запретить / эскалировать / переписать** tool call, **переписать результат**,
**добавить контекст** модели и **отклонить преждевременное завершение**.

Целевая форма turn-а:

```text
TurnController._run_tool_turn
  action.proposed
  classify_tool_request                  built-in policy, не отключается
    blocked ─────────────────────────────► policy_updates → return
  ToolBroker.prepare(request, state)     normalize_request + резолв tool/server
    unknown tool ────────────────────────► error result → observe (как сейчас)
  hooks.run(PreToolUse)
    deny ────────────────────────────────► policy_updates("blocked") → return
    ask  → needs_human
    updated_input → re-normalize, имя tool-а менять нельзя
  needs_human:
    hooks.run(PermissionRequest) allow/deny, иначе self._human_input
  ToolBroker.invoke(prepared)            invoke + normalize_result
  hooks.run(PostToolUse | PostToolUseFailure)
    updated_output → заменяет result.content
  ObservationCompiler.compile
  additional_context → отдельное сообщение в messages (НЕ в content tool-а)
```

## 2. Не цели (вне этого плана)

- Типы hook-ов `command`, `http`, `mcp_tool`, `prompt`, `agent` — только Python
  callables. Контракт проектируется так, чтобы их можно было добавить без ломки (§4.1).
- События `SessionStart`/`SessionEnd`, `PreCompact`, `BeforeModel`/`AfterModel`,
  `BeforeToolSelection`.
- Перенос `classify_tool_request` в «built-in hooks» — остаётся отдельным шагом.
- Интерактивный HITL в CLI — `_deny_human_input` остаётся дефолтом.
- Параллельный запуск hook-ов.

## 3. Рабочие правила

- **С выключенными hook-ами поведение байт-в-байт прежнее**: `pytest` зелёный,
  eval baseline `tests/evals/baselines/agent_loop_v1.json` не меняется, в
  `events.jsonl` не появляется новых записей.
- Каждый коммит — одна граница; рефакторинг (`ToolBroker`) отдельно от нового поведения.
- Слои по `CLAUDE.md`: контракты — нейтральный лист; движок/runner — в
  `src/harness/`; браузерные hook-и — в `src/browser/`; движок только вызывает.
- Hook никогда не получает и не возвращает `LoopState`; движок сам переводит
  `HookOutcome` в `LoopState.apply(...)`.
- Никаких модульных констант: всё тюнингуемое — в `src/config.py`.

## 4. Решения, зафиксированные планом

Уточняют research там, где он оставлял выбор.

### 4.1 Контракт

```python
# src/contracts.py (нейтральный лист)
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
    server: str = ""               # MCPTool.server, "" для не-MCP tool-ов
    args: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)   # ToolResult (post_*)
    reason: str = ""               # причина built-in needs_human (permission_request)
    final_answer: str = ""         # stop / goal_end
    observation: str = ""          # последнее observation (stop)
    stop_hook_active: bool = False
    status: str = ""               # terminal status (goal_end)

@dataclass(frozen=True)
class HookResult:
    decision: HookDecision | None = None
    reason: str = ""               # видно модели
    updated_input: Mapping[str, Any] | None = None     # pre_tool_use
    updated_output: str | None = None                  # post_tool_use*
    additional_context: str = ""   # отдельное сообщение модели
    user_message: str = ""         # только в события / CLI

HookHandler = Callable[[HookEvent], Awaitable[HookResult | None]]
```

`None` от handler-а — «нет мнения» (как в ADK). Поля события — только
JSON-сериализуемые, чтобы будущий `command`-hook получил тот же объект через stdin.

### 4.2 Семантика решений по событиям

| Событие | `decision` | `updated_*` | `additional_context` | fail по умолчанию |
|---|---|---|---|---|
| `goal_start` | `deny` — задача не запускается, `blocked` | — | ✅ добавляется до плана | closed |
| `pre_tool_use` | `deny`, `ask` (→ needs_human); `allow` = «нет возражений» | `updated_input` | ✅ | **closed** |
| `permission_request` | `allow` / `deny`; `None` → `human_input` | — | — | **closed** (= deny) |
| `post_tool_use(_failure)` | игнорируется | `updated_output` | ✅ | open |
| `stop` | `deny` = не завершать, `reason` → фидбек | — | ✅ | open (= нет решения) |
| `goal_end` | игнорируется | — | игнорируется | open |

**Уточнение к research §3.2:** `allow` в `pre_tool_use` **не** обходит built-in
`needs_human`. Единственный путь авто-одобрения — `permission_request`. Два пути к
одобрению — лишняя поверхность атаки; так же разделяют Claude Code и Codex
(`PreToolUse` vs `PermissionRequest`).

### 4.3 Агрегация

- Handler-ы события выполняются **последовательно в порядке `hooks.registry`**.
- Первый `deny` — short-circuit, остальные не запускаются.
- `ask` запоминается, выполнение продолжается (более поздний `deny` сильнее).
- `updated_input`/`updated_output` применяются **цепочкой**: следующий handler видит
  уже изменённый `args`/`result`.
- `additional_context` всех handler-ов конкатенируется в порядке выполнения.
- Timeout (`asyncio.wait_for`) и исключение трактуются одинаково — по `fail_closed`
  спецификации, иначе по дефолту события из §4.2. Fail-closed на `pre_tool_use` =
  `deny` с причиной `"Hook <id> failed: <timeout|error>"`.

### 4.4 Stop только для `done` от модели

`done` в движке рождается в двух местах: `done_response` из `_classify_action`
(модель ответила) и guard-терминалы (`_terminal_guard`, unchanged-snapshot в
`CompletionController.observation_terminal_update`, `blocked_response`). Stop-hook
вызывается **только** для первого и только при `completion_status == "done"`.
Guard-терминалы — жёсткие лимиты, hook их не отменяет.

При `deny` движок применяет:

```python
{
    "decision": "",               # иначе _terminal_guard сразу завершит по done+final_answer
    "final_answer": "",
    "completion_status": "",
    "stop_blocks": state.stop_blocks + 1,
    "messages": [..., user_message(f"Completion rejected: {reason}\n{additional_context}")],
}
```

Бюджет: `stop_blocks >= settings.hooks.max_stop_blocks` → hook не вызывается, `done`
принимается, в события пишется `hook.decided` с `skipped: "stop_budget_exhausted"`.
`stop_hook_active = stop_blocks > 0`. Каждый продолженный ход расходует `turn_cap`.

### 4.5 Контекст не смешивается с результатом tool-а

`additional_context` добавляется в `messages` **отдельным** user-сообщением с
префиксом `[harness]` после `ObservationCompiler.compile`, а не дописывается в
`result.content`. Причина: `ProgressDetector` и unchanged-snapshot-детекция
сравнивают content; меняющийся хвост сломал бы их. `updated_output`, напротив,
заменяет content **до** compile, чтобы все детекторы видели одну и ту же версию.

### 4.6 Размещение

| Что | Файл |
|---|---|
| `HookEventName`, `HookDecision`, `HookEvent`, `HookResult`, `HookHandler` | `src/contracts.py` |
| `HooksSettings`, `HookSpec`, `HookMatch` | `src/config.py` |
| `HookEngine`, `NullHookEngine`, `HookOutcome`, загрузка handler-ов, matcher | `src/harness/hooks.py` |
| Общие handler-ы (`approve_tools`, `grounded_final_answer`) | `src/harness/builtin_hooks.py` |
| Браузерные handler-ы (`url_policy`, `prompt_injection_scan`) | `src/browser/hooks.py` |
| `PreparedToolCall`, `ToolBroker.prepare/invoke` | `src/agent_loop/execution/tools.py` |
| Точки вызова | `src/agent_loop/execution/loop.py` |
| Проброс | `EngineResources.hooks` (`resources.py`), `BrowserHarness.hooks` (`runtime.py`) |

### 4.7 Конфигурация

```python
# src/config.py
class HookMatch(_Section):
    server: str = ""   # exact
    tool: str = ""     # "a|b" — exact list; иначе regex (fullmatch)

class HookSpec(_Section):
    id: str
    event: HookEventName
    handler: str                       # "package.module:attr"
    match: HookMatch = Field(default_factory=HookMatch)
    options: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    fail_closed: bool | None = None    # None → дефолт события (§4.2)

class HooksSettings(_Section):
    enabled: bool = False
    max_stop_blocks: int = 2
    default_timeout_seconds: float = 10.0
    registry: list[HookSpec] = Field(default_factory=list)
```

- `Settings.hooks: HooksSettings` — **девятая** секция; env
  `AUTOBROWSER_HOOKS__ENABLED`, `AUTOBROWSER_HOOKS__MAX_STOP_BLOCKS`,
  `AUTOBROWSER_HOOKS__DEFAULT_TIMEOUT_SECONDS`. `registry` задаётся через YAML
  (через env возможен JSON, но в `.env.example` не документируется).
- `handler` резолвится `importlib` **при старте сессии**: ошибка импорта, дубликат
  `id` или неизвестное событие — ошибка запуска, а не тихий пропуск.
- Если `options` не пуст, `handler` — фабрика: движок один раз вызывает
  `handler(**options)` и использует результат как `HookHandler`.
- Matcher применяется только к tool-событиям; для остальных `match` должен быть
  пустым (валидатор модели).

## 5. Коммиты

Каждый коммит: `python -m pytest` зелёный; `ruff check .` чистый.

### Коммит 0 — ADR

- `docs/decisions/2026-09-30-lifecycle-hooks-engine.md` по `adr-template.md`:
  решения §4.1–4.5, отвергнутые альтернативы (event bus, параллельный запуск,
  `MODIFY`-вердикт, hook-и в MCP Manager, auto-approve через `pre_tool_use`).
- Строка в `docs/decisions/index.md`.

### Коммит 1 — контракты и настройки (без поведения)

- `src/contracts.py`: типы §4.1 + `__all__`.
- `src/config.py`: `HookMatch`, `HookSpec`, `HooksSettings`, `Settings.hooks`.
- `.env.example`: три скалярных поля секции `hooks`.
- `config.example.yaml`: закомментированный пример `hooks.registry`.

Тесты:
- `tests/test_config.py`: дефолты секции; env-переопределение; `extra="forbid"` на
  опечатке в `HookSpec`; валидатор «match только для tool-событий»; drift
  `.env.example` (существующий тест должен пройти после обновления шаблона).
- `tests/test_contracts.py` (новый или в существующем): `HookEvent` сериализуется
  `json.dumps(asdict(...))`; `src/contracts.py` по-прежнему ничего не импортирует из
  `src/agent_loop`, `src/harness`, `src/browser`.

### Коммит 2 — `ToolBroker.prepare` / `invoke` (чистый рефакторинг)

```python
@dataclass(frozen=True)
class PreparedToolCall:
    request: ToolRequest        # после normalize_request
    tool: Any | None            # None → unknown tool
    server: str                 # getattr(tool, "server", "")
    error_result: ToolResult | None = None   # готовый unknown-tool/no-name результат

class ToolBroker:
    async def prepare(self, request, state=None) -> PreparedToolCall: ...
    async def invoke(self, prepared: PreparedToolCall) -> ToolResult: ...   # invoke + normalize_result
    async def execute(self, request, state=None) -> ToolResult:             # = invoke(await prepare(...))
    def renormalize(self, prepared, args, state) -> PreparedToolCall: ...   # для updated_input
```

- `execute` сохраняется как композиция — все текущие вызовы и тесты не меняются.
- `renormalize` прогоняет normalizers по `{**request, "args": args}` и **отклоняет**
  смену `name` (возвращает `error_result` «Hook cannot change the tool name»).

Тесты (`tests/test_mcp_tools_bridge.py` / новый `tests/test_tool_broker.py`):
- `execute` ≡ `invoke(prepare())` на fake MCP server;
- `prepare` отдаёт `server` для `MCPTool` и `""` для простого callable;
- unknown tool → `error_result`, `invoke` его только нормализует;
- `renormalize` применяет `BrowserToolNormalizer` (отбрасывание запрещённых схемой
  аргументов) и запрещает смену имени.

### Коммит 3 — `HookEngine` (изолированно)

`src/harness/hooks.py`:

```python
@dataclass(frozen=True)
class HookOutcome:
    decision: HookDecision | None      # агрегированное (deny > ask > allow > None)
    reason: str
    updated_input: Mapping[str, Any] | None
    updated_output: str | None
    additional_context: str
    decisions: tuple[HookDecisionRecord, ...]   # по одному на запущенный handler

class HookEngine:
    @classmethod
    def from_settings(cls, settings: HooksSettings, *, events: EventEmitter | None) -> HookEngine | NullHookEngine
    def has(self, name: HookEventName) -> bool       # быстрый путь без построения события
    async def run(self, event: HookEvent, *, event_ctx: Mapping[str, Any]) -> HookOutcome
```

- `NullHookEngine`: `has()` всегда `False`, `run()` возвращает пустой outcome —
  при `enabled=False` движок не строит `HookEvent` вообще.
- Matcher: пустой `match` — всё; `server` — exact; `tool` — `a|b` exact list, иначе
  `re.fullmatch` (компилируется при загрузке).
- Агрегация и fail-семантика — строго по §4.2–4.3.
- Каждый запущенный handler → `HookDecisionRecord(hook_id, event, decision, reason,
  duration_ms, modified, error)`; эмиссия — в коммите 4.

Тесты `tests/test_harness_hooks.py`:
- matcher: exact, list, regex, server+tool, пустой;
- порядок = порядок registry; `deny` останавливает цепочку; `ask` + поздний `deny` → `deny`;
- цепочка `updated_input` / `updated_output`; конкатенация `additional_context`;
- timeout и исключение: fail-closed на `pre_tool_use` → `deny`, fail-open на
  `post_tool_use` → нет решения; явный `fail_closed` переопределяет дефолт;
- фабрика с `options`; ошибка импорта handler-а / дубликат `id` → исключение в
  `from_settings`;
- `enabled=False` → `NullHookEngine`.

### Коммит 4 — аудит: событие `hook.decided`

- `src/agent_loop/events.py`: `"hook.decided"` в `EventType`.
- `HookEngine.run` эмитит по одной записи на handler; `args`/`result` не пишутся
  целиком — только `tool`, `server`, решение, причина, `modified`, `duration_ms`,
  `error`; строки проходят `redact_json_safe`.
- `src/agent_loop/replay.py`: читаемая строка для `hook.decided`; `metrics.py` —
  проверить, что новый тип не считается ошибкой в `_is_error_event` (кроме
  `error != ""`).

Тесты: `tests/test_agent_loop_events.py` (редакция секретов в `reason`),
`tests/test_agent_loop_replay.py` (рендер), `tests/test_agent_loop_metrics.py`.

### Коммит 5 — проброс и `pre_tool_use` / `post_tool_use`

- `EngineResources`: поле `hooks: HookEngine | NullHookEngine = NullHookEngine()`;
  `from_harness` берёт `getattr(harness, "hooks", NullHookEngine())`.
- `BrowserHarness`: инжектируемый коллаборатор `hooks`, по умолчанию
  `HookEngine.from_settings(get_settings().hooks, events=...)`; строится один раз на
  сессию (handler-ы импортируются при старте).
- `TurnController._run_tool_turn` по схеме §1:
  - `pre_tool_use` вызывается после built-in `classify_tool_request` **и** для
    `allow`, и для `needs_human` (идея pre-approval guardrails из Agents SDK: человеку
    не показываем заведомо запрещённое);
  - `deny` → `policy_updates(state, "blocked", reason)` — тот же путь, что built-in
    блок: tool-message с причиной, `consecutive_failures + 1`;
  - `ask` при built-in `allow` → ветка `needs_human`;
  - `post_tool_use` при `result.status == "success"`, иначе `post_tool_use_failure`;
    `updated_output` заменяет `result["content"]` (или `error` для failure) до
    `ObservationCompiler.compile`;
  - `additional_context` (pre + post) → одно сообщение в `messages` после compile (§4.5).
- `tool.started` / `tool.finished` эмитятся как раньше; `tool.started` несёт
  финальный (после `updated_input`) запрос.

Тесты `tests/test_agent_loop_hooks.py` (fake MCP server из `tests/mcp_fixtures/`,
скриптованный `ChatModel`):
- deny → tool не вызван (счётчик вызовов fake-сервера), причина в последнем
  tool-message, `consecutive_failures == 1`;
- deny N раз подряд упирается в существующий лимит consecutive failures → `blocked`;
- `ask` → вызывается `human_input`; при отказе → `blocked`;
- `updated_input` доходит до сервера; попытка сменить имя → error result;
- `updated_output` виден в observation и в `action_history`;
- `additional_context` — отдельное сообщение, content tool-а не изменён,
  unchanged-snapshot-детекция не ломается;
- built-in `blocked` (stale snapshot) → `pre_tool_use` не вызывается;
- `hooks.enabled=False` → последовательность событий идентична текущей
  (регрессионный тест на список `EventRecord.type`).

### Коммит 6 — `permission_request`

- В ветке `needs_human`: если `hooks.has("permission_request")` →
  `allow` — выполнить без `human_input`; `deny` — `blocked` с причиной; `None` →
  `self._human_input` как раньше. `approval.requested` эмитится всегда.
- `src/harness/builtin_hooks.py: approve_tools(tools: list[str], servers: list[str] = [])`
  — фабрика, одобряющая перечисленные tool-ы (для batch/eval).

Тесты: allow без вызова `human_input`; deny; `None` → `human_input`;
fail-closed по timeout = deny.

### Коммит 7 — `stop`

- `LoopState.stop_blocks: int = 0`; `TASK_BOUNDARY_RESETS["stop_blocks"] = 0`
  (`src/harness/session.py`); в `SESSION_STATE_KEYS` **не** добавляется.
- Разметка источника `done`: `_agent_step` возвращает
  `AgentStepOutcome(update, origin: Literal["guard", "model"])` вместо голого dict
  (guard — `pre_turn_terminal`, stale-snapshot и прочие ранние выходы; model — всё из
  `_classify_action`). `run_turn` вызывает stop-hook только при
  `origin == "model"` и итоговом статусе `done`.
- Применение `deny` — по §4.4; `HookEvent.observation` = `state.observation`,
  `final_answer` = ответ модели.
- `src/harness/builtin_hooks.py: grounded_final_answer(min_chars=1)` — детерминированная
  проверка: ответ непустой; числа (цены, количества) из ответа встречаются в
  последнем observation или в tool-сообщениях текущей задачи; иначе `deny` с
  перечнем неподтверждённых значений.

Тесты:
- deny → loop продолжается, `final_answer`/`decision` сброшены, фидбек в messages;
- бюджет `max_stop_blocks` → `done` принимается, `hook.decided` со `skipped`;
- guard-терминал (unchanged snapshot, лимит replan) → stop-hook не вызывается;
- `turn_cap` по-прежнему ограничивает run при постоянном deny;
- `stop_blocks` сбрасывается между задачами одной сессии
  (`tests/test_harness_session.py`);
- unit-тесты `grounded_final_answer`.

### Коммит 8 — `goal_start` / `goal_end`

- В `AgentLoopEngine.run` (не в `GoalRunner` — он по контракту не трогает state и
  решения): `goal_start` до `_run_plan`; `deny` → сразу `AgentLoopResult(status="blocked")`
  с причиной как `final_answer`, модель не вызывается; `additional_context` →
  user-сообщение `[harness]` до плана.
- `goal_end` — после выхода из цикла, наблюдательный: исключения/таймауты
  логируются, на результат не влияют.

Тесты: deny не делает ни одного вызова `ChatModel`; контекст попадает в историю
до plan-запроса; `goal_end` получает итоговый статус; исключение в `goal_end` не
меняет результат.

### Коммит 9 — браузерные hook-и

`src/browser/hooks.py`:

- `url_policy(allow_domains=[], deny_domains=[], deny_schemes=["file", "chrome",
  "javascript", "data"])` — для `pre_tool_use` с `match.tool: browser_navigate`;
  разбор `args["url"]` через `urllib.parse`, сравнение по суффиксу хоста;
  непарсящийся URL → `deny`.
- `prompt_injection_scan(patterns=[...])` — для `post_tool_use` с
  `match.tool: browser_snapshot`; детерминированные регэкспы («ignore previous
  instructions», «system prompt», «you are now» и русские аналоги); **не** меняет
  content (ref-ы и детекция прогресса должны остаться целыми), а возвращает
  `additional_context` с предупреждением, что текст страницы — недоверенные данные.

Тесты: `tests/test_browser_hooks.py` — таблицы URL (разрешён/запрещён/схема/поддомен/
мусор); сработавшие и несработавшие паттерны; content не изменён.

### Коммит 10 — документация и проверка

- `docs/diagrams/agent-runtime-flow.md`: hook-точки в turn; `harness-boundaries.md`:
  `HookEngine` в `BrowserHarness` → `EngineResources`; строка в `docs/diagrams/index.md`.
- `CLAUDE.md` и `AGENTS.md`: «девять секций» настроек, раздел про hooks (где живут,
  что hook не может отменить built-in policy, disabled by default), команда
  `python -m pytest tests\test_agent_loop_hooks.py tests\test_harness_hooks.py`.
- `docs/research/index.md`: ссылка на этот план; статус research → «implemented» после мёржа.
- `docs/glossary.md`: термины hook, hook event, handler, fail-closed.
- Прогон: полный `pytest`, `python scripts/run_evals.py --baseline
  tests\evals\baselines\agent_loop_v1.json` с hooks off (без изменений) и один
  ручной `python main.py --show-state` с включёнными `url_policy` +
  `grounded_final_answer` на задаче поиска — проверить отсутствие циклов.

## 6. Критерии готовности

- [ ] С `hooks.enabled=false` — полный `pytest` и eval baseline без изменений.
- [ ] `pre_tool_use` deny не доходит до MCP-сервера; причина видна модели.
- [ ] Hook не может снять built-in `blocked` и не может одобрить `needs_human`
      иначе как через `permission_request`.
- [ ] Stop-hook не зацикливает run: ограничен `max_stop_blocks` и `turn_cap`.
- [ ] Упавший/зависший hook не роняет задачу; fail-closed/open по §4.2.
- [ ] Каждое решение hook-а есть в `events.jsonl` как `hook.decided`, секреты
      отредактированы; `replay_trace.py` его показывает.
- [ ] Ошибки конфигурации hook-ов видны при старте сессии.
- [ ] Диаграммы, ADR, `CLAUDE.md`/`AGENTS.md` обновлены.

## 7. Риски

| Риск | Митигирующее решение |
|---|---|
| Hook ломает snapshot/ref-инварианты | built-in policy до hook-ов и не отключается; `updated_input` проходит normalizers; имя tool-а менять нельзя |
| Контекст/перезапись ломает детекцию прогресса | `additional_context` отдельным сообщением (§4.5); `updated_output` до compile |
| Бесконечное продолжение через `stop` | `max_stop_blocks`, `turn_cap`, только model-`done` |
| Утечка секретов (`browser_type` с паролем) в события | в `hook.decided` нет `args`; `redact_json_safe` на строках |
| Медленный hook тормозит каждый turn | `default_timeout_seconds`, `duration_ms` в событиях, matcher отсекает лишние вызовы |
| Недетерминизм в evals | последовательный порядок registry; hooks выключены в baseline |
| Выполнение произвольного кода из конфига | только `config.yaml`/`AUTOBROWSER_CONFIG_FILE` (git-ignored, личный профиль); по умолчанию выключено; хэш `hooks.registry` пишется в `session.json` |

## 8. После плана (backlog)

1. `command`-hook: JSON `HookEvent` в stdin, ответ в формате Claude Code
   (`hookSpecificOutput.permissionDecision` …), exec-форма без shell на Windows.
2. `mcp_tool`-hook: handler = вызов tool-а зарегистрированного MCP-сервера через
   `MCPManager` (внешний policy-сервер).
3. `SessionStart`/`SessionEnd` в `SessionRuntime`; `PreCompact` в `src/harness/memory.py`.
4. `BeforeToolSelection` для борьбы с зацикливанием на search-affordance (открытый
   вопрос research §6).
5. Решить, переводить ли `classify_tool_request` в built-in hook-и с `removable=False`.
