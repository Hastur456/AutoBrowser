# Memory в harness-системах — research для AutoBrowser

Дата: 2026-10-02. Скоуп: как устроена память в agent harness-ах (Claude Code, Claude API
memory tool + context editing, OpenAI Codex CLI, Gemini CLI, OpenAI Agents SDK, LangGraph,
Letta, Browser Use) и в исследованиях по памяти web-агентов, и что из этого переносится в
engine-native loop AutoBrowser. Документ исследовательский: решение оформляется отдельным
ADR. Входной материал — [Системный дизайн памяти Browser-Harness агента](2026-09-14-memory-system-design.md);
ниже он сверен с текущим кодом и с документацией вендоров, расхождения собраны в §3.

Продолжает [Session-Scoped Agent Context Memory](../decisions/2026-07-25-session-scoped-agent-context-memory.md)
и [Server-Neutral Progress Journal](../decisions/2026-09-28-server-neutral-progress-journal.md).

---

## 0. Идея в одном абзаце

Слово «память» в harness-е закрывает четыре разные задачи с разными сроками жизни:
**(1)** что модель видит на этом шаге (working context), **(2)** что переживает сжатие
внутри задачи (task state), **(3)** что переносится между задачами одной сессии (session
carry-forward), **(4)** что переживает процесс (persistent memory). AutoBrowser сейчас
хорошо закрывает (2) и (3), частично (1) и совсем не закрывает (4). Индустрия сошлась на
одной схеме: **закреплённые блоки, которые harness заново рендерит каждый ход, + файловая
память в markdown с маленьким индексом, который грузится всегда, и ленивой подгрузкой
остального + детерминированная очистка старых tool results вместо LLM-суммаризации, где
это возможно**. Для браузерного агента к этому добавляется жёсткое ограничение: в памяти
нельзя хранить refs и CSS-селекторы, а всё, что пришло со страницы, считается недоверенным.

---

## 1. Обзор harness-ов

### 1.1 Claude Code

Два механизма, оба грузятся в начале каждой сессии и оба — **контекст, а не принудительная
конфигурация** (чтобы что-то запретить, нужен `PreToolUse` hook):

- **CLAUDE.md / AGENTS.md** пишет человек. Иерархия managed → user (`~/.claude/CLAUDE.md`) →
  project → `CLAUDE.local.md`. Файлы выше рабочей директории грузятся при запуске, файлы в
  поддиректориях — по требованию, когда Claude читает файлы оттуда. `@path`-импорты (до 4
  уровней вложенности) раскрываются при запуске и **не уменьшают** стоимость контекста.
  Рекомендация — до 200 строк на файл.
- **`.claude/rules/*.md` с frontmatter `paths:`** — правила с областью действия, грузятся только
  когда Claude работает с подходящими файлами. Это прямой аналог «site-файла, который
  грузится только для своего домена».
- **Auto memory** пишет сам Claude: `~/.claude/projects/<project>/memory/`, индекс `MEMORY.md`
  (одна строка на запись) + topic-файлы. При старте грузятся **первые 200 строк или 25 КБ**
  индекса, topic-файлы Claude читает сам по необходимости. После записи harness проверяет
  размер индекса: рядом с лимитом напоминает сократить, сверх лимита возвращает ошибку
  «перепиши индекс», потому что хвост при следующей загрузке потеряется. Память исключена
  из retention-очистки транскриптов.
- Компакция: CLAUDE.md и память лежат вне истории, поэтому после компакции они берутся с
  диска заново, а не из summary.

### 1.2 Claude API: memory tool, context editing, compaction

- **Memory tool** (`memory_20250818`) — client-side файловый инструмент над префиксом
  `/memories`: `view` (каталог на 2 уровня или файл с номерами строк и `view_range`), `create`,
  `str_replace` (должен быть уникальным), `insert`, `delete`, `rename`. Хранилище целиком на
  стороне приложения. API добавляет в системный промпт протокол «ALWAYS VIEW YOUR MEMORY
  DIRECTORY BEFORE DOING ANYTHING ELSE … ASSUME INTERRUPTION». Требования безопасности из
  документации: защита от path traversal (`resolve()` + `relative_to()`, отказ от `../`, `%2e%2e`),
  лимиты на размер файлов и на вывод `view`, удаление давно не использованных файлов,
  очистка чувствительных данных перед записью.
- **Context editing** `clear_tool_uses_20250919` — детерминированная очистка tool results:
  `trigger` (по умолчанию 100k input tokens или число tool uses), `keep` (последние N пар,
  по умолчанию 3), `clear_at_least` (чтобы инвалидация prompt cache окупалась),
  `exclude_tools`, `clear_tool_inputs`. Очищенный результат заменяется плейсхолдером. В связке
  с memory tool модель получает предупреждение до очистки и успевает сохранить важное.
- **Compaction** — серверная суммаризация всей истории у границы окна. Документация
  советует использовать оба механизма: компакция держит контекст маленьким, а память
  хранит то, что должно пережить суммаризацию.
- Из [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents):
  очистка tool results — «самая безопасная и лёгкая форма компакции»; хранить лёгкие
  идентификаторы (пути, URL) и подгружать данные just-in-time; structured note-taking вне окна.

### 1.3 OpenAI Codex CLI

- **AGENTS.md** — цепочка global (`~/.codex/AGENTS.md`) → корень проекта → директории, лимит
  32 KiB по умолчанию.
- **Memories** (выключены по умолчанию, `[features] memories = true`; флаги
  `generate_memories` / `use_memories`) — **фоновая генерация из завершённых сессий**:
  - фаза 1 (extraction): по каждой простаивающей сессии параллельно, дешёвой моделью,
    получается `raw_memory` + `rollout_summary`; активные и короткие сессии пропускаются;
  - фаза 2 (consolidation): одна на всю систему, последовательно, сильной моделью. Отбор по
    частоте использования и свежести, отсев по `max_unused_days`, diff added/retained/removed;
    sub-agent консолидации запускается без approvals, без сети, только с локальной записью;
  - артефакты в `~/.codex/memories/`: `MEMORY.md`, `memory_summary.md`, `raw_memories.md`,
    `rollout_summaries/`, `skills/` (процедурная память);
  - автоматическая редакция секретов в сгенерированных полях; генерация ставится на паузу
    при низком остатке rate limit.
- Позиция OpenAI: «обязательные правила держите в AGENTS.md; memories — вспомогательный
  слой recall, а не единственный источник правил, которые должны применяться всегда».
  Memories не переопределяют AGENTS.md.

(Детали фаз взяты из стороннего разбора исходников, официальная документация описывает их
коротко — см. источники.)

### 1.4 Gemini CLI

`GEMINI.md`: global → workspace (с подъёмом по родителям) → **just-in-time**: когда
инструмент обращается к файлу или директории, CLI ищет `GEMINI.md` в ней и в её предках до
trusted root. Все найденные файлы конкатенируются. `/memory show`, `/memory reload`,
`@file.md`-импорты, имя файла настраивается (`context.fileName`). JIT-загрузка по месту
обращения — та же идея, что подгрузка site-памяти при навигации на домен.

### 1.5 OpenAI Agents SDK

`Session` — протокол хранения **истории** (`get_items`, `add_items`, `pop_item`,
`clear_session`) с бэкендами SQLite, Redis, SQLAlchemy, Encrypted и др.
`session_input_callback` меняет, что попадает в модель, но не то, что сохраняется.
`OpenAIResponsesCompactionSession` оборачивает любую сессию и компактит историю (можно
отключить автотриггер и вызывать `run_compaction()` в простое). Вывод: **хранение истории
и формирование входа модели — разные операции**. У нас это уже так (`LoopState.messages`
против `ContextAssembler`).

### 1.6 LangGraph, Letta, Mem0

- **LangGraph**: short-term = thread + checkpointer; long-term = `Store` с namespace-ами.
  Типы semantic / episodic / procedural; semantic как **profile** (один документ, который
  обновляется) или **collection** (много мелких записей, которые сложнее обновлять и удалять).
  Запись **в hot path** (сразу доступна, но добавляет задержку и решение модели) или **в фоне**
  (проще основной цикл, но нужно выбрать момент).
- **Letta (MemGPT)**: core memory blocks — `label`, `value`, `limit` (символы), `description`,
  `read_only`. Блоки всегда в контексте, агент правит их инструментами; запись заменяет
  значение целиком, при конкурентной записи побеждает последняя.
- **Mem0**: memory как сервис с извлечением фактов и отдельным `procedural_memory`. Их же
  эксперимент поверх «compaction cliff»: правила в рабочем контексте после трёх компакций
  сохранились в 3–4 случаях из 7, правила в памяти — все; при ~10 правилах простой файл
  справляется не хуже Mem0.

### 1.7 Browser Use

- Каждый шаг модели — `AgentOutput` с полями `thinking`, `evaluation_previous_goal`, **`memory`**,
  `next_goal`, `action[]`. Поле `memory` — это заметки модели самой себе («что сделано, что
  осталось; считай повторы»), сжатый журнал, который переживает обрезку истории.
- `max_history_items` ограничивает число шагов в LLM-памяти; есть `message_compaction`.
- Ранняя long-term «procedural memory» на Mem0 (`enable_memory`, `MemoryConfig`) была
  источником багов (issues #1245, #1663, #1747); есть ли она в актуальных релизах, по
  открытым источникам не видно.
- Известный провал: «агент забывает конечную цель» (#1763). Предложенное решение —
  отдельные отслеживаемые блоки вместо одного свободного поля.

### 1.8 Исследования по памяти web-агентов

- **Agent Workflow Memory** (Wang et al., 2024): workflows, индуцированные из успешных
  траекторий, дают +24.6% (Mind2Web) и +51.1% (WebArena) относительного success rate и меньше
  шагов. Online-режим индуцирует workflows из собственных прогонов, которые evaluator признал
  успешными. Ключевые workflows по сайту набираются примерно за 40 запросов.
- **Procedural Memory Under Change** (2026): несовпадающая процедура не мешает, **если
  свидетельства текущей задачи явные и достаточные**. Общей безопасности авторы не
  утверждают. Для нас это довод в пользу snapshot-first: процедура — подсказка, а истина —
  текущий snapshot.
- **The Compaction Cliff** (2026): при уравнительной суммаризации после одного раунда
  сохраняется 53% safety-правил, после пяти — 10%. Решение — triage по типу знания: правила
  закреплены и не суммаризуются.
- «Are Online Skill and Memory Modules Always Worth Their Tokens?» (2026): при фиксированном
  токен-бюджете память окупается не всегда. Память нужно мерить на evals, а не включать по
  умолчанию.

### 1.9 Сводка

| | Пишет | Где живёт | Что грузится всегда | Что лениво | Сжатие истории |
|---|---|---|---|---|---|
| Claude Code | человек + модель | markdown-файлы | CLAUDE.md, 200 строк / 25 КБ `MEMORY.md` | поддиректории, `rules/` по `paths`, topic-файлы | компакция, память с диска |
| Claude API | модель (tool) | приложение | ничего (протокол «view first») | всё через `view` | context editing + compaction |
| Codex | человек + фон | `~/.codex/memories` | AGENTS.md, `MEMORY.md` | grep по памяти | компакция |
| Gemini CLI | человек (+ `/memory`) | markdown | global + workspace | JIT по месту обращения | — |
| Agents SDK | runtime | Session-бэкенд | история | — | compaction session |
| Letta | модель (tools) | БД | core blocks | archival / recall | summarization |
| Browser Use | модель (поле `memory`) | история | последние N шагов | — | `max_history_items` |

Общие паттерны:

1. **Маленький индекс всегда, детали лениво**, с жёстким лимитом размера и проверкой его
   на записи.
2. **Скоупинг по месту** (директория, файл, домен) и загрузка по факту обращения.
3. **Закреплённое знание живёт вне истории** и рендерится заново, а не суммаризуется.
4. **Детерминированная очистка tool results** раньше LLM-компакции.
5. **Две скорости записи**: явный tool в hot path и фоновая консолидация из завершённых
   прогонов.
6. **Память — контекст, а не полномочия**: она не снимает запретов и не заменяет правила.
7. **Редакция секретов, лимиты, TTL и забывание по неиспользованию**.

---

## 2. Текущее состояние AutoBrowser (аудит кода)

| Слой | Где | Что есть | Пробелы |
|---|---|---|---|
| Working context | `ContextAssembler.assemble` (`src/agent_loop/context.py`) | Блоки `Task`, `Plan`, `Action History`, `Observation`, `Tool Inventory`, `Browser Rules` **рендерятся заново каждый ход** и не лежат в истории | `ContextBlock.token_budget` объявлен, но не используется |
| История | `LoopState.messages` + `MemoryManager` (`src/harness/memory.py`) | Функциональный сервис; `compact_snapshot_history` заменяет вывод инструмента плейсхолдером `[compacted]`, если он длиннее `memory.compact_tool_output_min_chars` и позже есть результат того же инструмента | **Нет бюджета**: короткие результаты, tool calls и все прошлые задачи сессии копятся без ограничений. `memory.max_tool_message_refs` объявлен в `src/config.py`, но **нигде не читается** |
| Task state | `LoopState.action_history` (`execution/progress.py`), `plan` | Журнал вызовов с `args_key`/`outcome_key` переживает компакцию (рендерится блоком) | Нет «заметок модели» о прогрессе (аналог `memory` у Browser Use) |
| Session carry-forward | `LoopState.to_session_state` → `SessionContext.state` | messages, observation, snapshot, browser state переносятся; task-local поля сбрасываются | Предыдущие задачи остаются в истории целиком, без дайджеста |
| Episodic log | `.autobrowser/sessions/<id>/events.jsonl`, `tasks.json` | Полный журнал с редакцией секретов (`redact_json_safe`) | Никто его не читает обратно, кроме replay/export |
| Persistent memory | — | **Нет** | Знание о сайтах зашито в промпт (см. ниже) |

Знание о сайтах, которое сейчас лежит в промптах: Ozon search-URL fallback
(`https://www.ozon.ru/search/?text=…`) встречается в `src/agent_loop/prompts.py` в четырёх местах
(строки 46, 102, 217, 247) и в `docs/development/browser-agent-rules.md:57`. Оно попадает в
каждый промпт на любом домене. Это ровно тот контент, для которого в Claude Code
существуют path-scoped rules.

Архитектурное преимущество, которое надо сохранить: `ContextAssembler` уже работает как
«закреплённые блоки вне истории». Значит, persistent memory встраивается **ещё одним
блоком**, и правило «после компакции перечитать с диска» выполняется бесплатно, потому что
блок каждый ход строится заново.

---

## 3. Что во входном research устарело или не подходит

| Тезис входного документа | Статус | Почему |
|---|---|---|
| Site-файл хранит «надёжные локаторы элементов», «дрейф селекторов», fallback на визуальный поиск при провале селектора | **Противоречит инварианту** | Агент snapshot-driven: refs эфемерны, CSS/XPath запрещены (`browser-agent-rules.md`). В памяти можно хранить только **URL-шаблоны, роли и accessible names контролов, последовательности шагов и особенности сайта**. Исполнение всегда идёт через свежий snapshot. Staleness-политика остаётся, но для процедур и фактов, а не для селекторов |
| `/memory/global/AGENTS.md` | Переименовать | Путаница с `AGENTS.md` репозитория. Глобальные правила агента уже есть: системный промпт + `browser-agent-rules.md`. Persistent memory нужен индекс `MEMORY.md` |
| Корень `/memory` | Уточнить | Runtime-данные живут под `storage.root_dir` (`.autobrowser/`, git-ignored). Память: `.autobrowser/memory/`, путь через `src/config.py` |
| «Фаза 0 — сжатие наблюдений» как предусловие | Частично сделано | `compact_snapshot_history`, `--compress-tools`, `Action History` уже есть. Не хватает **бюджета** и сжатия на границе задач, а не самого сжатия |
| Компакция на 60% окна | Нужна адаптация | Провайдер — Ollama, токенайзера на нашей стороне нет. Бюджет считается в символах (`chars/4` как оценка), а `num_ctx` берётся из настроек LLM |
| Отдельный `/memory/episodic/` | Избыточно | `events.jsonl` + `tasks.json` уже episodic. Консолидация читает их, вторую копию не заводим |
| `/memory/state/<task-id>.md` для многошаговых флоу | Отложить | Задача не переживает процесс (resume нет), а внутри процесса состояние держит `LoopState`, который мы не суммаризуем. Вернуться к этому вместе с resume |
| Playbook промоутится после ≥2 успешных прогонов | Сохранить | Совпадает со staged trust у Codex и с online-AWM (только прогоны, подтверждённые evaluator) |
| Первая запись site-файла — с ревью человеком | Сохранить, механизм есть | Запись в память — обычный tool call, и `PermissionEngine` (`ask`) уже даёт ревью |

---

## 4. Модель угроз памяти браузерного агента

1. **Prompt injection → persistence.** Текст страницы попадает в заметку и затем исполняется
   в каждой будущей сессии на этом домене. Это хуже разовой инъекции: она превращается в
   постоянную.
2. **Секреты и PII**: значения форм, токены в URL, адреса доставки.
3. **Утечка scope**: заметка про домен A применяется на домене B.
4. **Отравление процедуры**: ошибочный сценарий повторяется уверенно и многократно.
5. **Stale facts**: сайт сменил URL-схему или флоу.
6. **Память как обход политики**: заметка «оплату подтверждать не нужно».

Митигирование — в §5.5.

---

## 5. Предлагаемая модель

### 5.1 Четыре слоя

```text
L1 Working context   ContextAssembler blocks + бюджетированная история      (каждый ход)
L2 Task state        plan, action_history, + working notes                 (одна задача)
L3 Session           to_session_state + task digest вместо полной истории  (процесс)
L4 Persistent        .autobrowser/memory/: MEMORY.md, sites/, procedures/  (между процессами)
```

### 5.2 L1 — бюджет истории (детерминированный, без LLM)

Аналог `clear_tool_uses`, server-neutral, без имён инструментов:

- `memory.history_budget_chars` (0 = выключено): когда сумма символов истории выше бюджета,
  самые старые tool results (кроме последних `memory.keep_recent_tool_results`) заменяются
  плейсхолдером с `tool_call_id`, именем инструмента и длиной, по той же схеме, что и
  текущий `[compacted]`. Пара `assistant(tool_calls)` → `tool` остаётся валидной.
- Короткие ошибки не трогаем, пока бюджет позволяет: progress-ADR требует, чтобы модель
  видела, какие попытки уже провалились. При этом ключевое свидетельство дублируется в
  `Action History`, поэтому очистка не теряет сигнал повтора.
- Удалить или подключить мёртвую настройку `memory.max_tool_message_refs`.
- Живёт в `MemoryManager` (функционально), вызывается из `ensure_history`. Это
  infrastructure, а не engine-логика.

### 5.3 L2/L3 — working notes и дайджест задачи

- **Working notes** (опционально, за флагом): необязательное поле `notes` в JSON-решении
  модели (как `approval_request`: снимается при разборе, не попадает в аргументы
  инструмента). Хранится в `LoopState.working_notes` (task-local), рендерится блоком
  `Working Notes` с лимитом символов. Это паттерн `memory` из Browser Use, но в виде блока с
  лимитом, а не свободного текста в истории. Риск для gemma4 — лишний токен-шум; проверять
  на evals.
- **Task digest на границе задач**: при переносе в следующую задачу история завершённой задачи
  сворачивается в одну детерминированную запись `[previous task] <task> → <status>:
  <final_answer>; last page: <url>` (без LLM). Сырыми сохраняются только последние tool
  results, нужные для follow-up («открой первый товар»): последний snapshot уже лежит в
  `browser.snapshot`. Это закрывает неограниченный рост `messages` между задачами, не ломая
  сценарий ADR 2026-07-25.

### 5.4 L4 — persistent memory

**Раскладка** (`storage.root_dir/memory/`, путь в `src/config.py`):

```text
.autobrowser/memory/
  MEMORY.md                 индекс: одна строка на файл — путь, описание, статус, дата
  sites/<domain>.md         факты о сайте (registrable domain, normalize_domain)
  procedures/<slug>.md      параметризованные сценарии со scope по домену
```

**Формат записи** — markdown с frontmatter:

```markdown
---
scope: ozon.ru
kind: site            # site | procedure
status: verified      # user | verified | unverified | stale
source: user          # user | agent:<task_id>
verified_at: 2026-10-02
uses: 3
---
- Поиск: если поле поиска не реагирует, перейти на https://www.ozon.ru/search/?text=<query>
- Фильтры применяются без перезагрузки; после клика сделать snapshot, чтобы увидеть новый список.
```

**Что можно хранить**: URL-шаблоны, роли и accessible names контролов («кнопка "Войти"»),
порядок шагов, особенности сайта (cookie-баннер, капча, нужен логин), параметры. **Что
нельзя**: refs, CSS/XPath, значения форм, секреты, дословные фрагменты страницы.

**Загрузка** (новый блок `ContextAssembler`, приоритет между `Browser Rules` и `Task`):

- `MEMORY.md` — всегда, с усечением до `memory.index_max_lines` / `memory.index_max_chars`
  (по образцу 200 строк / 25 КБ).
- `sites/<domain>.md` и подходящие `procedures/*` — **лениво**, когда текущий домен
  (из `Page URL:` последнего результата) совпадает со `scope`. Домен разрешает
  browser-адаптер из `src/browser/`. Резолвер `Page URL:` уже есть в `src/browser/permissions.py`,
  его нужно вынести в общий helper. Engine домена не знает.
- Записи `unverified` / `stale` рендерятся с пометкой «hint — verify against the current
  snapshot». Записи `user` и `verified` — как факты.
- Перечитывание с диска каждый ход (с кэшем по mtime) даёт и «re-read after compaction», и
  подхват ручных правок без рестарта.

**Запись — две скорости**, обе выключены по умолчанию:

1. **Hot path — memory tool** (`memory.tool_enabled`). Harness-native `Tool` (не MCP-сервер),
   регистрируется в `ToolRegistry`. Команды по мотивам memory tool Anthropic: `view`
   (`readOnlyHint`), `create`, `str_replace`, `delete`, ограниченные корнем памяти. Проходит
   **через весь существующий конвейер**: `pre_tool_use` hooks → `PermissionEngine` → broker. Так
   пользователь правилом `ask` получает ревью каждой записи, а режим `read_only` запрещает
   запись. Engine имени инструмента не знает: всё как у любого другого tool.
2. **Фон — consolidation** (`memory.consolidate_on_goal_end`, фаза 3). После `goal_end`
   со статусом `done` отдельный дешёвый model call по дайджесту задачи (из `tasks.json` /
   события `goal.*`, без сырых snapshot-ов) предлагает кандидатов в память со статусом
   `unverified` и `source: agent:<task_id>`. Промоут в `verified` — после
   `memory.promote_after_successes` (по умолчанию 2) успешных прогонов с использованием
   записи. Разжалование в `stale` — после провала задачи, в которой запись использовалась,
   или по `memory.stale_after_days` без использования. Модель вызывает harness, а не engine
   (правило «engine never calls a model» сохраняется, как у approval judge).

### 5.5 Защиты

| Угроза | Мера |
|---|---|
| Injection → persistence | Санитайзер записи переиспользует паттерны `prompt_injection_scan` (`src/browser/hooks.py`) и отклоняет запись с совпадением; запрещены refs (`ref=e\d+`) и CSS-подобные строки; лимит длины записи; `source` / `status` выставляет harness, не модель |
| Секреты / PII | `redact_json_safe` + отказ записывать значения из аргументов `type`/`fill`; в frontmatter нет полей для значений |
| Утечка scope | Загрузка строго по совпадению registrable domain; `scope: *` разрешён только для `source: user` |
| Отравление процедуры | Staged trust (`unverified` → `verified`); `unverified` — только hint; snapshot первичен (вывод Procedural Memory Under Change) |
| Stale facts | `stale_after_days`, разжалование при провале, `uses` / `verified_at` в frontmatter |
| Обход политики | Память — только контекст. `PermissionEngine` и hooks читают правила только из настроек, и никакая запись их не снимает (как в Claude Code: CLAUDE.md ≠ enforced config) |
| Path traversal | Все пути через `resolve()` + `relative_to(memory_root)`, отказ от `..` / абсолютных путей / URL-encoded форм |
| Compaction cliff | Память и правила — блоки `ContextAssembler`, они никогда не суммаризуются; L1-бюджет трогает только tool results |

### 5.6 Слои и владение

- Контракты (`MemoryEntry`, `MemoryScope`, `MemoryStatus`) — в `src/contracts.py`.
- Настройки — секция `memory` в `src/config.py` + `.env.example` + `tests/test_config.py`.
- `MemoryStore` (файлы, индекс, санитайзер, лимиты) — `src/harness/memory_store.py`,
  session-scoped (`SessionContext`), до engine доходит через `EngineResources.memory`, по
  образцу hooks и permissions. В тестах строится из явных настроек, никогда не из
  `get_settings()` (персональный `config.yaml` протечёт).
- Резолвер домена — `src/browser/`; engine получает его инъекцией.
- События `memory.loaded` / `memory.written` / `memory.rejected` — в `events.jsonl` (с редакцией).
- Ozon-подсказки переезжают из `prompts.py` в seed-файл `sites/ozon.ru.md` (`source: user`) **только
  вместе** с обновлением `tests/test_prompts.py` и evals. Иначе нарушится правило «не убирать
  инвариант из промпта, пока его не держит другой слой».

---

## 6. Этапы внедрения

| Фаза | Что | Зависимости | Риск |
|---|---|---|---|
| **0** | L1-бюджет истории + task digest на границе задач + уборка `max_tool_message_refs` | — | Низкий: детерминированно, покрывается тестами `MemoryManager` |
| **1** | L4 read-only: `MemoryStore`, загрузка `MEMORY.md` + `sites/<domain>.md` блоком, ручные файлы (`source: user`) | 0 не обязательна | Низкий: агент ничего не пишет. Сразу закрывает site-знание из промптов |
| **2** | Memory tool (hot path) через hooks + permissions, санитайзер, события | 1 | Средний: injection; mitigated by `ask`-правилом и санитайзером |
| **3** | Фоновая консолидация на `goal_end`, staged trust, stale | 2 (формат и санитайзер) | Средний: стоимость model calls, качество фактов |
| **4** | Procedures + working notes, eval-сравнение «с памятью / без» | 1–3 | Мерить токены и success rate (вывод budget-constrained study) |

Критический путь: 1 → 2 → 3. Фаза 0 независима и полезна сама по себе (неограниченный рост
`messages` — уже существующая проблема долгих REPL-сессий).

---

## 7. Открытые вопросы

1. Оценка токенов для Ollama: `chars/4` или реальный `prompt_eval_count` из ответа провайдера
   для адаптивного бюджета?
2. Нужен ли memory tool, если фаза 3 работает хорошо? Codex обходится без hot-path записи;
   Claude Code и Claude API делают ставку на неё.
3. Ключ домена: registrable domain (`ozon.ru`) или host (`seller.ozon.ru`)? Предварительно
   registrable с опциональным host-уточнением в `scope`.
4. Делить ли память между пользователями или профилями (`config.yaml` как профиль) или
   хранить её per-profile?
5. Как evals фиксируют влияние памяти: отдельный baseline с seed-памятью из `tests/`?

---

## Источники

- Claude Code — [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- Claude API — [Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool),
  [Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing),
  [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Codex — [Memories](https://learn.chatgpt.com/docs/customization/memories?surface=app),
  [Codex Built-In Memory Deep Dive](https://codex.danielvaughan.com/2026/04/18/codex-built-in-memory-system-deep-dive/) (сторонний разбор),
  [How memory works in Codex CLI (Mem0)](https://mem0.ai/blog/how-memory-works-in-codex-cli)
- Gemini CLI — [GEMINI.md](https://geminicli.com/docs/cli/gemini-md/)
- OpenAI Agents SDK — [Sessions](https://openai.github.io/openai-agents-python/sessions/)
- LangGraph — [Memory](https://docs.langchain.com/oss/python/langgraph/memory)
- Letta — [Memory blocks](https://docs.letta.com/guides/agents/memory-blocks)
- Mem0 — [Testing memory placement against compaction cliff](https://mem0.ai/blog/testing-memory-placement-against-compaction-cliff)
- Browser Use — [All parameters](https://docs.browser-use.com/customize/agent/all-parameters),
  issues [#424](https://github.com/browser-use/browser-use/issues/424),
  [#1763](https://github.com/browser-use/browser-use/issues/1763),
  [#5063](https://github.com/browser-use/browser-use/issues/5063)
- Papers — [Agent Workflow Memory](https://arxiv.org/abs/2409.07429),
  [Procedural Memory Under Change](https://arxiv.org/abs/2609.09774),
  [The Compaction Cliff](https://arxiv.org/abs/2608.22752),
  [Are Online Skill and Memory Modules Always Worth Their Tokens?](https://arxiv.org/pdf/2606.15017),
  [Building Browser Agents](https://arxiv.org/pdf/2511.19477)
