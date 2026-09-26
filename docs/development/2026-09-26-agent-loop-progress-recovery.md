# Восстановление после «пустого прогресса» в agent loop после миграции на MCP Manager

Дата: 2026-09-26 · Ветка: `feat/mcp-manager` · Основа: разбор трассы «Habr → статьи по ИБ»
(цикл `browser_evaluate → [] → browser_snapshot → browser_evaluate …`, в конце `done`).

## 0. Принципы

- **Модель считается сильной.** Harness не решает за неё, *какая* стратегия верна. Он даёт ей
  полную и честную картину: что она уже пробовала, что получила, изменилось ли что-то. Жёсткие
  блокировки остаются только страховкой от бесконечного цикла.
- **Никакого хардкода.** Никаких правил под конкретные сайты, инструменты или форматы вывода
  (`[]`, `ref=e…`, тексты ошибок Playwright). Всё выводится из протокола и данных:
  - повтор определяется сравнением *имени + аргументов + результата* вызова, какой бы это ни был
    инструмент;
  - «инструмент только читает» берётся из стандартной MCP-аннотации `readOnlyHint`, а не из
    списка имён.
- **Никакого хардкода в состоянии.** В `LoopState` не добавляются поля под конкретный инструмент
  (`evaluate_empty_count` и т.п.). Добавляются только общие поля: журнал действий и явный
  терминальный статус.
- Все пороги — в `src/config.py` (`AUTOBROWSER_<SECTION>__<FIELD>`), с `.env.example` и
  `config.example.yaml`.
- Архитектура harness (цепочка `SessionRuntime → GoalRunner → AgentLoopEngine → TurnController`)
  не меняется.

## 1. Диагноз (коротко)

| # | Где | Что сломано | Эффект на траекторию |
|---|---|---|---|
| A | `src/harness/memory.py::compact_snapshot_history` | При миграции удалён фильтр «только snapshot»: сжимается **любое** tool-сообщение, кроме последнего, с текстом «Snapshot superseded» | Результат `[]` исчезает из истории после следующего snapshot; snapshot исчезает, как только пришёл результат evaluate. Модель не видит, что гипотеза провалилась, и не видит страницу |
| B | `observation.py::BrowserStateReducer` | Любой успешный `browser_*` (включая только читающие) обнуляет `snapshot` | Policy «snapshot already current» не срабатывает; повторный snapshot всегда одобряется |
| C | `guards.py`, `policy.py`, `observation.py` | Удалён детектор неэффективных действий; repeat-guard видит только *подряд* идущие одинаковые вызовы | Паттерн A→B→A→B не детектируется ничем, кроме `turn_cap` |
| D | `guards.py::status_from_state`, `loop.py::_classify_action` | Статус выводится из префикса `Blocked:`. `Failed:`, `Cancelled:` и «Stopped because…» становятся `done` | Остановка по защите от цикла и явный отказ модели засчитываются как выполненная цель |
| E | `prompts.py`, `browser-agent-rules.md` | Правило против зацикливания *предписывает* «snapshot → попробуй evaluate»; нет исхода «не удалось», кроме `done` | Модель следует инструкции и крутит тот же цикл; неудачу оформляет как `done` |
| F | `resources.py` | `tool_normalizers=BrowserToolNormalizer()` — одиночный объект в `Sequence`, вне registry | Не влияет на сценарий; мёртвая/неверная связка |

## 2. План изменений

### Фаза 1 — память сохраняет доказательства (A)

`MemoryManager.compact_snapshot_history` → правило «вытеснения тем же инструментом»:

- tool-сообщение сжимается, только если **позже в истории есть результат того же инструмента**
  (сравнение по имени, без списка имён) **и** его тело длиннее
  `settings.memory.compact_tool_output_min_chars`;
- короткие результаты (ошибки, `[]`, короткий текст) не сжимаются никогда: они дешёвые и именно
  они несут информацию о провалившихся попытках;
- маркер нейтральный: «вывод `<tool>` с более раннего шага сжат (N символов); более новый
  результат `<tool>` есть дальше в истории». Для snapshot поведение прежнее: старые снимки
  вытесняются новым.

Новая настройка: `memory.compact_tool_output_min_chars` (по умолчанию 1000).

### Фаза 2 — журнал действий и сигнал «нет прогресса» (C)

Новый leaf-модуль `src/agent_loop/execution/progress.py`:

- `ActionRecord` (frozen): `tool`, `args` (компактный текст), `args_key`, `outcome_key`,
  `status`, `summary` (превью результата), `occurrence` — сколько раз *этот же вызов с этими же
  аргументами* уже дал *этот же результат*, включая текущий раз;
- ключи — хэш канонического JSON аргументов и нормализованного (по пробелам) текста
  результата/ошибки. Инструмент и формат вывода не важны;
- `LoopState.action_history: list[ActionRecord]` — task-local (не входит в
  `to_session_state`, поэтому сбрасывается между задачами).

Использование:

1. **Observation** (`ObservationCompiler`) добавляет запись в журнал. При `occurrence ≥ 2`
   к наблюдению и tool-сообщению дописывается нейтральная заметка: «этот вызов с теми же
   аргументами вернул тот же результат, что и N ранее; повтор не даст новой информации».
2. **Контекст** (`ContextAssembler`) рендерит блок `Action History`: последние
   `settings.observation.action_history_limit` записей — инструмент, аргументы, итог, счётчик
   повторов. Даже если история сообщений сжата, модель видит свою траекторию целиком.
3. **Guard** (`_guard_tool_request`): если запрошенный вызов (тот же инструмент + аргументы)
   уже `settings.loop.max_ineffective_actions` раз подряд давал одинаковый результат, вызов не
   исполняется, а turn уходит в replan с объяснением. Это ловит и A→B→A→B: считаются вхождения
   в журнал, а не соседние вызовы. Существующая настройка `max_ineffective_actions` получает
   обобщённый смысл (описание обновляется), новые пороги не нужны.

Новая настройка: `observation.action_history_limit` (по умолчанию 12).

### Фаза 3 — актуальность snapshot по MCP-аннотациям (B)

- `tool_is_read_only(tool)` в `src/harness/tools.py`: читает стандартную аннотацию MCP
  `annotations.readOnlyHint`;
- `TurnController` собирает множество read-only инструментов из registry и передаёт его в
  `ObservationCompiler`;
- `BrowserStateReducer` не сбрасывает `snapshot` после успешного read-only инструмента.
  Инструменты, которые сервер не пометил как read-only (например, `evaluate`, который может
  менять страницу), по-прежнему считаются меняющими состояние. Это консервативно и верно.

### Фаза 4 — честное завершение (D)

- `LoopState.completion_status` (`"" | "done" | "blocked" | "cancelled"`) — явный терминальный
  статус, который выставляет тот, кто принимает решение:
  - `done_response(..., status=...)`;
  - `blocked_response` → `blocked`;
  - `stop(failed|blocked)` от модели → `blocked`; `stop(cancelled)` → `cancelled`;
  - терминалы защиты от цикла («Stopped because …», «Blocked: …») → `blocked`;
- `CompletionController.status_from_state` читает поле; разбор префиксов остаётся только как
  fallback для carry-over состояний;
- обычный текст модели без tool call остаётся ответом (`done`) — это стандартная конвенция
  native tool-calling для сильных моделей.

### Фаза 5 — промпты без предписанного цикла (E)

- `AGENT_SYSTEM_PROMPT`:
  - блок «Preventing infinite loops» переписан: повтор вызова, который уже дал тот же результат,
    — это не прогресс; меняется *гипотеза или источник данных*, а не повторяется вызов после
    нового snapshot. Предписания «snapshot → попробуй evaluate» больше нет;
  - пустой или неизменившийся успешный результат — это доказательство того, что гипотеза
    неверна;
  - скрипты `browser.evaluate` опираются на наблюдаемую структуру (роли, видимый текст, цели
    ссылок), а не на угаданные классы;
  - появляется явный исход `{"decision":"blocked","reason":"…"}`; `done` — только когда ответ
    действительно удовлетворяет задаче;
  - `Action History` и заметки о повторах — надёжный сигнал.
- `docs/development/browser-agent-rules.md` («Non-Progress Signals»): добавлен общий паттерн
  «повтор вызова с идентичным результатом после повторного снятия состояния».

### Фаза 6 — мелкие правки (F)

- `EngineResources.from_harness`: `tool_normalizers` берутся из
  `tool_registry.get_normalizers()` (тот же источник, что и у `ToolBroker`).

## 3. Вне рамок этого шага (зафиксировано)

- **Тесты не трогаются в этом шаге** (решение пользователя). Baseline ветки до изменений:
  57 failed / 142 passed / 1 collection error. Это долг миграции: удалённый
  `CANONICAL_TO_PLAYWRIGHT`, fake-сценарии с именами `browser.*`, старые сигнатуры
  `SessionContext.initialize`, локальная модель по умолчанию в `test_config`. Нужен отдельный
  шаг: обновить тесты и добавить unit-тесты на журнал, сжатие памяти и статусы.
- Мёртвые браузер-специфичные поля состояния (`invalid_ref_recovery_count`,
  `stale_snapshot_retries`, `ineffective_action_count`, `BrowserState.ineffective_*`,
  `snapshot_before_last_browser_action`, `last_browser_action`) и no-op
  `stale_snapshot_retry_update`. Их удаление затрагивает `SESSION_STATE_KEYS` и carry-forward
  — это отдельный шаг.
- `steps_without_plan_advance` нигде не инкрементируется, поэтому guard мёртв. Чтобы его
  оживить, нужно решить, что такое «продвижение плана» на native-пути (сейчас план двигает
  только replan).
- Ref-валидация, удалённая при миграции (`_ref_action_snapshot_guard`), всё ещё заявлена как
  инвариант в `CLAUDE.md` и промптах. Её возврат в server-neutral виде требует решения, как
  описывать «ссылки на элементы» без схемы Playwright.
- Переключение вкладок (`pending_browser_tab_index` больше никто не выставляет).

## 4. Статус (2026-09-26)

| Фаза | Статус | Файлы |
|---|---|---|
| 1 — память | сделано | `src/harness/memory.py` |
| 2 — журнал действий | сделано | `src/agent_loop/execution/progress.py` (новый), `state.py`, `observation.py`, `policy.py`, `loop.py`, `src/agent_loop/context.py` |
| 3 — `readOnlyHint` | сделано | `src/harness/tools.py` (`tool_is_read_only`), `observation.py`, `loop.py` |
| 4 — статус завершения | сделано | `guards.py`, `loop.py` |
| 5 — промпты | сделано | `src/agent_loop/prompts.py`, `docs/development/browser-agent-rules.md` |
| 6 — normalizers | сделано | `src/agent_loop/execution/resources.py` |
| Настройки | сделано | `src/config.py`, `.env.example`, `config.example.yaml`: `memory.compact_tool_output_min_chars`, `observation.action_history_limit`, `observation.action_history_preview_chars`; обновлён смысл `loop.max_ineffective_actions` |
| Диаграммы | сделано | `docs/diagrams/agent-runtime-flow.md` |

Проверено ad-hoc прогоном engine в памяти (scripted-модель, повторяющая цикл
`snapshot → evaluate → []`, без браузера):

- `[]` остаётся в истории на всех turn'ах;
- со 2-го повтора появляется «Repeat detected», в prompt'е есть `Action History`;
- 4-й идентичный вызов отклоняет policy («Not executed»);
- run завершается `blocked` за 10 turn'ов (раньше — до `turn_cap` или `done`).

Терминальные исходы: `{"decision":"blocked"}` → `blocked`, `stop(failed)` → `blocked`,
`stop(cancelled)` → `cancelled`, JSON `done` или обычный текст → `done`. Тесты не запускались и
не обновлялись (см. раздел 3).

## 5. Проверка на живом браузере

- `python main.py --show-state --task "найди статьи по информационной безопасности на habr"`
  и просмотр `events.jsonl`:
  - результат `browser_evaluate` виден в истории на всех следующих turn'ах;
  - после повторного идентичного вызова в observation есть заметка о повторе;
  - блок `Action History` присутствует в prompt'е;
  - повтор сверх `max_ineffective_actions` уходит в replan;
  - если задача не решена, итоговый статус — `blocked`, а не `done`.
