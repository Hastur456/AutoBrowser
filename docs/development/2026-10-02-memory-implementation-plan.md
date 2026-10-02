# Memory — план реализации

Дата: 2026-10-02 · Ветка: `ref/memory-manager` · Статус: **Implemented** (кроме коммитов 6 и 6b)

> **Итог реализации.** Сделаны коммиты 1–5 и 7–10, решение зафиксировано в
> [ADR 2026-10-02 Layered Agent Memory](../decisions/2026-10-02-layered-agent-memory.md),
> руководство — [memory.md](memory.md). Отклонения от плана:
>
> - `max_tool_message_refs` не ломает старт: старый ключ принимается и игнорируется с
>   `FutureWarning` (вариант «мягкости» из §14; он стоит в личном `config.yaml`).
> - `notes` (фаза 4) — необязательный **аргумент tool**, а не поле JSON-решения: модель
>   действует нативными tool calls, у которых JSON-решения нет. Механизм тот же, что у
>   `approval_request`.
> - `NullMemoryContext` живёт в `src/harness/memory.py`, а не в `memory_store.py`: иначе
>   `execution/resources.py` нарушил бы инвариант 1 из §12.
> - Консолидация не перезаписывает записи `user` и `verified` (иначе фоновая догадка
>   понижала бы подтверждённое знание) и выполняется синхронно после задачи с таймаутом.
> - Eval-сравнение: `scripts/run_evals.py --memory-seed tests/evals/memory_seed` прогоняет
>   сценарии с seed-памятью и печатает размер промпта; сценарии скриптованные, поэтому исход
>   обязан совпасть с baseline.
> - Seed-пример Ozon лежит в `tests/evals/memory_seed/sites/ozon.ru.md` (каталог
>   `examples/` не версионируется).
> - Коммиты 6 (включение по умолчанию) и 6b (перенос Ozon-подсказок) ждут прогонов на
>   реальной модели.

Основа: [Memory Harness Research](../research/2026-10-02-memory-harness-research.md)
(далее «research»), входной черновик — [Memory System Design](../research/2026-09-14-memory-system-design.md).
Продолжает [Session-Scoped Agent Context Memory](../decisions/2026-07-25-session-scoped-agent-context-memory.md)
и [Server-Neutral Progress Journal](../decisions/2026-09-28-server-neutral-progress-journal.md).

## 1. Цель

Довести память AutoBrowser до четырёх слоёв из research §5.1:

```text
L1 Working context   бюджет истории (детерминированная очистка tool results)
L2 Task state        plan + action_history (есть) + working notes (фаза 4)
L3 Session           дайджест завершённых задач вместо их полной истории
L4 Persistent        .autobrowser/memory/: MEMORY.md + sites/<domain>.md + procedures/
```

Порядок: фаза 0 (L1/L3) → фаза 1 (L4 только чтение) → фаза 2 (memory tools, запись через
hooks и permissions) → фаза 3 (staged trust и консолидация) → фаза 4 (working notes и evals).
Каждая фаза — отдельный набор коммитов, который можно мержить сам по себе.

**Всё новое выключено по умолчанию.** Это соответствует правилу «каждое поведенческое
изменение аддитивно». Значения по умолчанию включаются отдельным коммитом после сравнения
на evals (коммит 6).

Не цели: векторный или семантический поиск, внешние memory-серверы (Mem0 и т. п.), resume
задачи между процессами (`/memory/state/<task-id>.md` из черновика), общая память между
профилями пользователей, суммаризация истории через LLM.

## 2. Найдено при подготовке плана (дополняет research §2)

| Находка | Где | Что делаем |
|---|---|---|
| `memory.max_tool_message_refs` объявлен, но нигде не читается | `src/config.py:367` | Удаляем (коммит 1). Риск для env — §12 |
| `ContextBlock.token_budget` объявлен, но не используется | `src/agent_loop/context.py:41` | Используем для усечения блока `Memory` (коммит 4) |
| `AssembledContext.system_prompt` нигде не читается: до модели доходит только `turn_prompt`, то есть блоки `role="user"`. `Tool Inventory` и `Browser Rules` (`role="system"`) собираются, но в запрос не попадают | `context.py:92`, `loop.py:385` | Блок `Memory` делаем `role="user"`. Починка system-блоков — отдельная задача вне плана (затрагивает промпт и evals), в этом плане только фиксируем |
| `Page URL:`-резолвер живёт внутри permissions-адаптера | `src/browser/permissions.py:33,54` | Выносим в общий browser-helper (коммит 3) |
| Ozon-подсказки зашиты в промпт в 4 местах | `src/agent_loop/prompts.py:46,102,217,247`, `browser-agent-rules.md:57` | Seed-пример site-файла (коммит 4). Удаление из промпта — только после evals (коммит 6b) |

## 3. Целевая форма хода

```text
_history(state)                                   MemoryManager.ensure_history
  compact_snapshot_history                        (есть) superseded + длинные
  apply_history_budget                            (новое) старые tool results → [cleared]
turn_prompt = context.user_turn_prompt(
    mapping, tools,
    memory=resources.memory.render(mapping))      (новое) блок "Memory", role=user
model → action → (tool turn без изменений: progress guard → prepare → pre_tool_use
                  → PermissionEngine → invoke → post_tool_use → observe)
                  memory_view / memory_write — обычные tools в этом же конвейере

task boundary (SessionRuntime, после goal_end)
  context.state.replace(latest_state)
  _task_state_overrides → MemoryManager.digest_tasks(messages)    (новое)
  MemoryStore.record_outcome(task_id, status)                     (фаза 3)
  MemoryConsolidator (opt-in, model call harness-а)               (фаза 3)
```

Engine не открывает файлы, не знает доменов и не знает имён memory tools. Он вызывает только
`resources.memory.render(mapping) -> str`, так же как вызывает `resources.hooks` и
`resources.permissions`.

## 4. Контракты (`src/contracts.py`, нейтральные)

```python
MemoryKind = Literal["site", "procedure"]
MemoryStatus = Literal["user", "verified", "unverified", "stale"]

@dataclass(frozen=True)
class MemoryEntry:
    path: str                 # относительно корня памяти, POSIX: "sites/ozon.ru.md"
    kind: MemoryKind
    scope: str                # нормализованный домен (normalize_domain) или "*"
    status: MemoryStatus
    source: str               # "user" | "agent:<task_id>"
    description: str          # одна строка для индекса
    body: str
    verified_at: str = ""     # ISO-дата
    uses: int = 0
    failures: int = 0

class MemoryScopeResolver(Protocol):
    def scope(self, state: Mapping[str, Any]) -> str: ...        # "" — домен неизвестен

class MemoryContentPolicy(Protocol):
    def violation(self, text: str) -> str | None: ...            # причина отказа или None
```

Резолвер и политика — протоколы: их browser-реализации живут в `src/browser/`. Harness
получает их инъекцией, как `PermissionResourceResolver`.

## 5. Настройки (`src/config.py`, секция `memory`, без новой секции)

| Поле | Тип / умолчание | Фаза | Смысл |
|---|---|---|---|
| ~~`max_tool_message_refs`~~ | удаляется | 0 | мёртвая настройка |
| `history_budget_chars` | `int ≥ 0` = `0` | 0 | `0` — выключено; иначе потолок суммы символов истории |
| `keep_recent_tool_results` | `int ≥ 0` = `3` | 0 | последние N tool results бюджет не трогает |
| `keep_recent_tasks` | `int ≥ 0` = `0` | 0 | `0` — не сворачивать; иначе сколько последних задач остаются в истории целиком |
| `persistent_enabled` | `bool` = `False` | 1 | загрузка L4 |
| `dir` | `str` = `"memory"` | 1 | относительно `storage.root_dir` |
| `index_max_lines` / `index_max_chars` | `200` / `25_000` | 1 | лимит рендера индекса |
| `block_max_chars` | `12_000` | 1 | лимит всего блока `Memory` (→ `token_budget`) |
| `file_max_chars` | `8_000` | 1/2 | лимит одного файла (усечение при чтении, отказ при записи) |
| `tool_enabled` | `bool` = `False` | 2 | регистрация `memory_view` / `memory_write` |
| `promote_after_successes` | `int ≥ 1` = `2` | 3 | `unverified` → `verified` |
| `stale_after_failures` | `int ≥ 1` = `2` | 3 | подряд неуспешных задач с записью → `stale` |
| `stale_after_days` | `int ≥ 0` = `90` | 3 | `0` — без TTL |
| `consolidate_on_goal_end` | `bool` = `False` | 3 | фоновый model call после `done` |
| `consolidation_timeout_seconds` | `float > 0` = `30` | 3 | потолок вызова |

Каждый коммит с настройками обновляет `.env.example`, `config.example.yaml` (закомментированный
пример) и `tests/test_config.py`.

## 6. Фаза 0 — бюджет истории и дайджест задач (`src/harness/memory.py`)

### 6.1 `MemoryManager.apply_history_budget(messages) -> list[Message]`

Аналог `clear_tool_uses` из Claude API, server-neutral, чистая функция:

1. `budget = settings.memory.history_budget_chars`; если `0` или
   `_history_chars(messages) <= budget`, вернуть копию.
2. Защищены: все не-`tool` сообщения, последние `keep_recent_tool_results` tool-сообщений и
   уже очищенные (`[compacted]` / `[cleared]`).
3. Остальные tool-сообщения идут от старых к новым: каждое заменяется на
   `tool_message(tool_call_id=…, name=…, content="[cleared] <name> output from an earlier step
   (<n> chars) was removed to fit the context budget.")`, пока сумма не станет ≤ бюджета.
4. Если и после этого бюджет превышен, ничего больше не трогаем: системный промпт,
   запросы пользователя и вызовы модели неприкосновенны.

`_history_chars` считает `content` + JSON аргументов `tool_calls`. Вызывается из
`ensure_history` после `compact_snapshot_history`. Пара `assistant(tool_calls)` → `tool`
сохраняется, потому что `tool_call_id` не меняется. Сигнал повтора не теряется: его
независимо несёт `Action History`.

### 6.2 `MemoryManager.digest_tasks(messages) -> list[Message]`

Граница задачи в истории — user-сообщение `User request (<task_id>):` (`USER_REQUEST_PREFIX`).

1. `keep = settings.memory.keep_recent_tasks`; если `0` или сегментов задач не больше `keep`,
   вернуть копию.
2. Каждый сегмент старше последних `keep` (от его user-сообщения до следующего) заменяется
   **одним** сообщением `user_message("[harness] Previous task digest:\n- request: …\n-
   answer: …\n- tools used: browser_navigate×2, browser_click×1")`:
   - `request` — текст задачи (усечённый до 300 символов);
   - `answer` — содержимое последнего `assistant` без `tool_calls` в сегменте (до 500
     символов) или `"(no final answer)"`;
   - `tools used` — счётчик имён из `tool_calls`. Это нейтрально, имена не интерпретируются.
3. Подряд идущие дайджесты не склеиваются: по одному на задачу, порядок сохраняется.

Сегмент целиком уходит вместе со своими tool calls и tool results, поэтому пары остаются
валидными. Вызывается **один раз на границе задач** из `session._task_state_overrides`
(сессия владеет переносом, `MemoryManager` — формой истории), а не на каждом ходе.
Follow-up сценарий из ADR 2026-07-25 («открой первый товар») не ломается: при `keep ≥ 1`
последняя задача остаётся целиком, а текущий snapshot и так переносится в `browser.snapshot`.

## 7. Фаза 1 — persistent memory, только чтение

### 7.1 `MemoryStore` (`src/harness/memory_store.py`)

- `MemoryStore.from_settings(memory: MemorySettings, storage: StorageSettings)` →
  корень `storage.root_dir / memory.dir`. Каталог создаётся лениво, только при записи.
- Формат файла: markdown с YAML-frontmatter (`yaml.safe_load`, PyYAML уже есть через
  `src/config.py`). Битый frontmatter или неизвестный `kind` / `status` — файл пропускается с
  событием `memory.skipped`. Startup это не валит: память — данные, а не конфигурация.
- `entries() -> tuple[MemoryEntry, ...]` с кэшем по `(path, mtime_ns, size)`, так что
  ручная правка подхватывается без рестарта, а повторный ход не читает диск заново.
- `entries_for_scope(domain) -> tuple[MemoryEntry, ...]`: `scope == domain` или
  `domain.endswith("." + scope)`; `scope == "*"` допускается только для `source: user`.
  Это закрывает research §7 вопрос 3: суффиксное сопоставление по нормализованному host, без
  Public Suffix List.
- `render_index() -> str`: индекс **генерируется из frontmatter**, а не пишется моделью
  (`- sites/ozon.ru.md [verified] — Ozon: поиск, фильтры`). Это снимает проблему раздувания
  `MEMORY.md`, которую Claude Code решает напоминаниями. Файл `MEMORY.md` на диске
  перезаписывается при каждой записи (фаза 2), чтобы человек видел его в редакторе. Источник
  истины — frontmatter.
- Безопасность путей: единственный вход `_resolve(rel) -> Path` — отказ от абсолютных
  путей, `..`, `\`, `%2e`, затем `resolve().relative_to(root)`; разрешены только `*.md` в
  `sites/` и `procedures/`.

### 7.2 Browser-адаптеры (`src/browser/memory.py`)

- `current_page_url(state)` выносится из `src/browser/permissions.py` в `src/browser/pages.py`
  (permissions-резолвер импортирует его оттуда, поведение не меняется).
- `BrowserMemoryScope.scope(state)` → `normalize_domain(host(current_page_url(state)))` или `""`.
- `BrowserMemoryPolicy.violation(text)` (используется в фазе 2): refs (`\bref=?e\d+\b`,
  `\[ref=`), CSS/XPath-подобные строки (`^\s*[#.][\w-]+\s*[>{]`, `//\w+\[`, `xpath`,
  `querySelector`), совпадения с `DEFAULT_INJECTION_PATTERNS` из `src/browser/hooks.py`,
  секрето-подобные пары (`(?i)(password|пароль|token|api[_-]?key|secret)\s*[:=]\s*\S`).

### 7.3 `MemoryContext` и блок `Memory`

- `src/harness/memory_store.py`: `MemoryContext(store, scope_resolver)` с
  `render(state) -> str`, а также `NullMemoryContext` (рендерит `""`).
- Рендер:
  ```text
  Persistent memory (hints from earlier sessions; the current snapshot always wins):
  Index:
  <render_index(), усечён до index_max_lines / index_max_chars>
  For <domain>:
  <тела entries_for_scope(domain); unverified/stale с префиксом
   "[unverified — verify against the current snapshot]">
  ```
  Общий лимит — `block_max_chars`: сначала усекаются тела `unverified` / `stale`, потом
  `verified`; `user` усекаются последними.
- `ContextAssembler.user_turn_prompt(state, *, tools=None, memory="")` и `plan_prompt(state,
  *, memory="")`: непустой `memory` становится блоком
  `ContextBlock(name="Memory", role="user", priority=15, source="memory",
  token_budget=block_max_chars)` — между `Task` (10) и `Plan` (20). В планировщике это
  отдельная секция после observation. На этапе плана домена ещё нет, поэтому видна только
  index-часть.
- `EngineResources.memory: Any = field(default_factory=NullMemoryContext)` +
  `from_harness(..., memory=None)`. Loop: `self._resources.memory.render(self._prompt_mapping(state))`
  в `_agent_step` и при построении плана.
- `SessionContext.memory` строится в `initialize` рядом с hooks и permissions, если
  `persistent_enabled`, иначе `NullMemoryContext`. В `session.json` пишется
  `{"memory": {"enabled": bool, "root": str, "entries": n}}`.

### 7.4 Seed-пример

`examples/memory/sites/ozon.ru.md` (`source: user`, `status: user`) содержит поисковый
URL-шаблон и подсказку про фильтры из `prompts.py`. В git он лежит как пример, пользователь
копирует его в `.autobrowser/memory/sites/`. Из промпта подсказки **не удаляются** (см. 6b).

## 8. Фаза 2 — memory tools

### 8.1 Инструменты (`src/harness/memory_tool.py`)

Два harness-native объекта с атрибутами в форме `MCPTool` (`name`, `description`,
`input_schema`, `server="memory"`, `annotations`, `invoke`): broker, permissions
(`check.server`) и `tool_is_read_only` работают с ними без изменений.

| Tool | `annotations` | Команды |
|---|---|---|
| `memory_view` | `{"readOnlyHint": True}` | `path` пусто → индекс; иначе файл (с `view_range`) |
| `memory_write` | `{}` | `create(path, description, body, scope?)`, `str_replace(path, old, new)`, `delete(path)` |

Отдельный `memory_view` с `readOnlyHint` позволяет в режиме `read_only` читать, но не писать.
Схема намеренно уже, чем у Anthropic memory tool: нет `insert` и `rename`, frontmatter модели
не виден и ею не редактируется.

Запись (`memory_write`):

1. путь через `_resolve`; `kind` выводится из каталога; `scope` — из аргумента или из имени
   файла `sites/<domain>.md`, через `normalize_domain`;
2. `MemoryContentPolicy.violation(description + body)` → отказ с причиной (это tool error,
   модель её читает, ход не терминален);
3. `len(body) > file_max_chars` → отказ;
4. frontmatter выставляет harness: `status: unverified`, `source: agent:<task_id>`,
   `verified_at: ""`, `uses: 0`. Правка файла со статусом `user` запрещена (только человек);
5. атомарная запись (`tmp` + `replace`, как `_write_json`), затем перегенерация `MEMORY.md`.

`task_id` берётся из `MemoryStore.bind_task(task_id)`, который вызывает `SessionRuntime.reset_task`
(у tool-функции нет state).

### 8.2 Регистрация и конвейер

- `tool_enabled and persistent_enabled` → `SessionRuntime` добавляет оба tool в
  `ToolRegistry` как статический provider. Engine их имён не знает.
- Ревью человеком на запись — **пользовательское правило** (никаких встроенных правил, см.
  ADR name-free defaults). В `config.example.yaml` есть закомментированный пример:
  ```yaml
  permissions:
    rules:
      - {id: memory-write-review, decision: ask, server: memory, tool: memory_write}
  ```
- Отдельных событий записи не нужно: `tool.started` / `tool.completed` и
  `permission.decided` уже пишутся в `events.jsonl` с редакцией.
- Подсказка модели: заголовок блока `Memory` дополняется одной строкой про `memory_view` /
  `memory_write`, **только если tools зарегистрированы**. Что писать: URL-шаблоны, названия
  контролов, шаги. Что не писать: refs, селекторы, значения форм. Это текст блока, а не
  статичного промпта, поэтому в `tests/test_prompts.py` добавляется проверка рендера блока.

## 9. Фаза 3 — staged trust и консолидация

- `MemoryStore.note_loaded(task_id, paths)` — из `MemoryContext.render` (какие файлы реально
  попали в блок за задачу).
- `MemoryStore.record_outcome(task_id, status)` — из `SessionRuntime` после `goal_end`:
  - `done` → `uses += 1`, `failures = 0`; `unverified` с `uses ≥ promote_after_successes` →
    `verified`, `verified_at = today`;
  - `blocked` → `failures += 1`; `failures ≥ stale_after_failures` → `stale`;
  - `cancelled` → ничего;
  - `user`-записи счётчики не меняют.
- TTL: при загрузке `verified` с `verified_at` старше `stale_after_days` рендерится как `stale`
  (файл не переписывается).
- `MemoryConsolidator` (`src/harness/memory_consolidation.py`, opt-in
  `consolidate_on_goal_end`): после `done` один вызов `ChatModel` с task, final answer,
  отрендеренным `Action History` (без сырых snapshot-ов) и доменами задачи. Ответ — JSON со
  списком `{path, description, body}`, не больше 3 элементов. Каждый кандидат проходит ту же
  `MemoryContentPolicy` и пишется как `unverified`. Ошибка или таймаут — событие
  `memory.consolidation_failed`, задача не страдает. Модель вызывает session-слой, а не
  engine и не `GoalRunner`, поэтому правило «engine never calls a model» сохраняется.
  Промпт лежит в `src/agent_loop/prompts.py` и проверяется в `tests/test_prompts.py`.

## 10. Фаза 4 — working notes и измерение

- Optional `notes` в JSON-решении модели: снимается при разборе, как `approval_request`.
  Хранится в `LoopState.working_notes: str` (task-local, не входит в `to_session_state`),
  рендерится блоком `Working Notes` (priority 26), лимит `memory.working_notes_max_chars`.
- Eval-сравнение `tests/evals/`: baseline без памяти против seed-памяти из
  `tests/evals/memory_seed/` (явный `MemoryStore` из настроек теста, не из `get_settings()`).
  Метрики: success rate, ходы, символы промпта.

## 11. План коммитов

| # | Коммит | Содержимое | Тесты |
|---|---|---|---|
| 0 | `docs(memory): ADR and implementation plan` | ADR `2026-10-02-layered-agent-memory.md` (Proposed), этот план, ссылки в `docs/decisions/index.md` | — |
| 1 | `feat(memory): history budget for tool results` | §6.1, настройки `history_budget_chars`, `keep_recent_tool_results`, удаление `max_tool_message_refs` | `tests/test_harness_memory.py` (новый): бюджет 0 = no-op; очищаются старейшие; последние N и не-tool не трогаются; пары `tool_call_id` валидны; идемпотентность; `test_config.py` |
| 2 | `feat(memory): digest finished tasks at the task boundary` | §6.2, `keep_recent_tasks`, вызов в `_task_state_overrides` | сегментация по `task_id`; дайджест содержит request / answer / tools; пары валидны; `keep=0` no-op; `test_harness_session.py`: follow-up видит последнюю задачу целиком |
| 3 | `refactor(browser): share the page URL resolver` | `src/browser/pages.py`, импорт в permissions | существующие permissions-тесты без изменений + юнит `current_page_url` |
| 4 | `feat(memory): read-only persistent memory block` | §4 контракты, §7.1–7.4, настройки фазы 1, `EngineResources.memory`, `SessionContext.memory`, seed-пример | `tests/test_memory_store.py`: frontmatter, битые файлы, кэш mtime, scope-сопоставление, `*` только user, path traversal (`..`, абсолютный, `%2e`, `\`), генерация индекса, лимиты; `test_context_assembler.py`: блок `Memory` user-role, priority, усечение, пусто → нет блока; `test_harness_session.py`: выключено → `NullMemoryContext` |
| 5 | `feat(memory): memory_view and memory_write tools` | §8, `tool_enabled`, `bind_task`, пример правила в `config.example.yaml` | `tests/test_memory_tool.py`: отказ по policy (ref, CSS, injection, секрет), лимит размера, harness-frontmatter, запрет правки `user`, атомарность, перегенерация индекса; `test_permission_*`: `read_only` разрешает view и запрещает write; правило `ask` на `memory_write` → approval; `tests/test_prompts.py`: подсказка в блоке |
| 6 | `feat(memory): enable history budget defaults` | `history_budget_chars` и `keep_recent_tasks` включаются по результатам evals; обновляется baseline | `run_evals.py --baseline …`, `test_config.py` |
| 6b | `refactor(prompts): move Ozon hints to site memory` *(опционально)* | только если evals с seed-памятью не хуже; правка `prompts.py`, `browser-agent-rules.md`, `test_prompts.py` | `test_prompts.py`, evals |
| 7 | `feat(memory): staged trust and outcome tracking` | §9 без консолидатора | promote / stale / TTL / user-иммунитет; `cancelled` не меняет счётчики |
| 8 | `feat(memory): opt-in consolidation after goal_end` | `MemoryConsolidator`, промпт | фейковый `ChatModel`: кандидаты проходят policy и пишутся `unverified`; таймаут и битый JSON → событие, задача `done` |
| 9 | `feat(memory): working notes block` | §10 | разбор `notes`, сброс между задачами, лимит |
| 10 | `docs(memory): guide, diagrams, CLAUDE.md` | `docs/development/memory.md` (guide), `docs/diagrams/` (context assembly + task boundary), `docs/glossary.md`, раздел в `CLAUDE.md`, ADR → Accepted | — |

После каждого коммита — `python -m pytest`; после 4, 5, 8 — ещё
`python -m pytest tests\test_prompts.py` и один `--show-state` прогон на Ozon-задаче
(проверка на зацикливание, как требует `CLAUDE.md`).

## 12. Тестовые инварианты (сквозные)

1. Engine (`src/agent_loop/execution/`) не импортирует `memory_store`, `memory_tool`, `src/browser/memory`
   и не содержит строк `memory_view` / `memory_write`.
2. Тесты и evals никогда не читают память или её настройки через `get_settings()`: только
   явные `MemorySettings(...)` и `tmp_path`.
3. Пары `assistant(tool_calls)` → `tool` валидны после любого сочетания compact, budget и digest
   (property-тест на случайных историях).
4. С выключенными флагами промпт байт-в-байт совпадает с текущим (регрессионный снапшот в
   `test_context_assembler.py`).
5. Ни одна запись в память не меняет вердикт `PermissionEngine` или hooks.
6. Файлы памяти не содержат refs и селекторов (policy-тест на всех путях записи, включая
   консолидатор).

## 13. Решения, принятые в плане (закрывают research §7)

- **Токены:** бюджет в символах; `prompt_eval_count` провайдера пока не используем, потому
  что он приходит после вызова. Адаптивный бюджет по нему — возможное развитие.
- **Hot path против фона:** нужны оба, но hot path (фаза 2) идёт раньше. Его можно ревьюить
  через permissions уже сейчас, а консолидацию без staged trust (фаза 3) включать рано.
- **Ключ домена:** нормализованный host с суффиксным сопоставлением, без PSL.
- **Профили:** память одна на `storage.root_dir`; профиль через `config.yaml` может задать
  другой `memory.dir`.
- **Evals:** отдельный seed-каталог в `tests/evals/memory_seed/` и отдельный прогон.
- **Индекс** генерирует harness, а не модель.

## 14. Риски

| Риск | Митигирование |
|---|---|
| Удаление `max_tool_message_refs` ломает старт у тех, у кого задан `AUTOBROWSER_MEMORY__MAX_TOOL_MESSAGE_REFS` (секции `extra="forbid"`) | Упомянуть в теле коммита и в `.env.example`; настройка никогда не работала. Если нужна мягкость — принимать и игнорировать с предупреждением один релиз |
| Бюджет очищает свидетельство, которое модели ещё нужно | Защищены последние N результатов; `Action History` дублирует исходы; по умолчанию выключено до evals |
| Дайджест теряет результаты прошлой задачи, на которые ссылается пользователь | `keep_recent_tasks ≥ 1`; snapshot переносится отдельно; тест follow-up |
| Injection через memory_write | policy-фильтр, harness-frontmatter, `unverified`-префикс, рекомендуемое правило `ask`, snapshot первичен |
| Память раздувает промпт и ухудшает gemma4 | `block_max_chars`, ленивые site-файлы, eval-гейт перед включением по умолчанию |
| Две REPL-сессии пишут один файл | атомарная запись, last-write-wins; блокировки не вводим (локальный однопользовательский инструмент) |
| Site-знание расходится между промптом и памятью | до 6b промпт остаётся источником, seed повторяет его дословно; 6b переносит только вместе с тестами |
