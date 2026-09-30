# Lifecycle hooks — research для AutoBrowser

Дата: 2026-09-30. Скоуп: как устроены hooks в современных agent harness
(Claude Code / Claude Agent SDK, OpenAI Codex CLI, Gemini CLI, Cursor, LangChain v1
middleware, OpenAI Agents SDK guardrails, Google ADK callbacks) и как перенести идею в
engine-native loop AutoBrowser. Документ исследовательский: он не принимает решение —
решение оформляется отдельным ADR. Продолжает «Phase 6: Hooks» из
[Codex-Claude Runtime Migration Plan](2026-07-26-codex-claude-runtime-migration-plan.md)
и раздел «Hooks» из [Agent Loop Runtime Research](2026-07-26-agent-loop-runtime-research.md).

---

## 0. Идея в одном абзаце

Hook — это **детерминированный middleware вокруг недетерминированного agent loop**.
Модель решает *что* сделать (tool call, «я закончил»), hook решает *что обязано
произойти вокруг* этого решения: разрешить / запретить / переписать аргументы /
добавить контекст / не дать завершиться. Hook — не tool: tool вызывает модель,
hook вызывает harness в фиксированной точке жизненного цикла. MCP даёт агенту
**возможности** (capability), hooks — **контроль** (control).

---

## 1. Обзор harness-ов

### 1.1 Claude Code (CLI) и Claude Agent SDK

Эталонная и самая полная реализация; остальные CLI (Codex, Gemini, Cursor) явно копируют
её контракт (JSON в stdin, JSON/exit code наружу, `matcher`, `hookSpecificOutput`).

**События** (≈30, основные): `SessionStart`/`SessionEnd`, `UserPromptSubmit`,
`PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`,
`Stop`/`StopFailure`, `SubagentStart`/`SubagentStop`, `PreCompact`/`PostCompact`,
`Notification`, `Elicitation`/`ElicitationResult` (MCP), `TaskCreated`/`TaskCompleted`,
`PreModelSwitch`/`PostModelSwitch`, `ConfigChange`, `FileChanged` и др.

**Типы hook-ов:** `command` (shell, JSON через stdin), `http` (POST), `mcp_tool`
(hook реализован как вызов tool-а MCP-сервера), `prompt` (однократный LLM-вызов),
`agent` (субагент с Read/Grep; experimental). В Agent SDK — ещё и **in-process
Python/TS callback-и** `async def hook(input_data, tool_use_id, context) -> dict`,
регистрируемые через `HookMatcher(matcher="Write|Edit", hooks=[...], timeout=...)`.

**Контракт решения для `PreToolUse`:**

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow | deny | ask | defer",
    "permissionDecisionReason": "видно модели",
    "updatedInput": { "...": "переписанные аргументы" },
    "additionalContext": "инъекция в контекст модели"
  },
  "systemMessage": "видно пользователю",
  "continue": true
}
```

Ключевые семантики:

- **Deny побеждает.** Все подходящие hook-и запускаются **параллельно**; при
  конфликте `deny > defer > ask > allow`. Порядок завершения недетерминирован,
  поэтому каждый hook обязан быть независимым.
- **`updatedInput` ортогонален решению**, а не отдельный вердикт `MODIFY`:
  `allow + updatedInput` — авто-одобрить переписанный вызов; `ask + updatedInput` —
  показать человеку; без решения — переписанный вызов идёт через обычную
  permission-оценку.
- **Причина отказа — это фидбек модели** (`permissionDecisionReason`), модель
  перестраивает действие. Отсюда паттерн *propose → validate → fail → fix → pass*.
- **`PostToolUse` не может отменить побочный эффект**, но может переписать результат
  (`updatedToolOutput`) или добавить контекст — это redaction/sanitization-слой.
- **`Stop` можно заблокировать** (`decision: "block"` + `reason`) — агент продолжает
  работу с причиной как фидбеком. Защита от бесконечного цикла — флаг
  `stop_hook_active` во входе.
- **Отказоустойчивость по событию:** timeout `PreToolUse` → tool **не** выполняется
  (fail-closed, с v2.1.210); timeout `Stop` → считается «нет решения»;
  timeout `UserPromptSubmit` → промпт блокируется; `PostToolUse` → результат сохраняется.
- **Matcher** — по имени tool-а (exact list `A|B` или regex); MCP-tools имеют имена
  `mcp__<server>__<tool>`, поэтому матчинг по серверу — это regex `mcp__playwright__.*`.
  Дополнительно `if`-условие по аргументам (`Bash(rm *)`) для tool-событий.
- **Async hook-и** (`async: true`) — fire-and-forget, не могут блокировать; только
  для логов/метрик.
- **Доверие:** проектные hook-и требуют workspace trust; есть `allowManagedHooksOnly`,
  `disableAllHooks`, allowlist URL для http-hook-ов и env-переменных в заголовках.

### 1.2 OpenAI Codex CLI

С v0.114 (март 2026), конфиг `~/.codex/hooks.json` или TOML `[[hooks.PreToolUse]]`.
События: `SessionStart`/`SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`/`PostCompact`,
`SubagentStart`/`SubagentStop`, `Stop`. Контракт почти 1:1 с Claude Code.

Отличия, полезные нам:

- **Trust по хэшу hook-а:** новый/изменённый hook требует явного review (`/hooks`),
  managed hook-и (requirements.toml, MDM) — без review.
- **`Stop` с `decision: "block"` превращает `reason` в continuation prompt** —
  не отклоняет ход, а создаёт следующий. Вход содержит `stop_hook_active` и
  `last_assistant_message`.
- **`PostToolUse` явно описан как advisory**, а не enforcement: «Treat tool hooks as
  a useful guardrail, not a complete enforcement boundary».
- Background hook-и ограничены (8 на сессию) и доставляют вывод в «следующей
  безопасной точке» разговора.

### 1.3 Gemini CLI

Та же модель JSON-over-stdin, но **больше точек вокруг модели**:

- `BeforeModel` — может переписать `llm_request` или вернуть синтетический
  `llm_response` (пропуск LLM-вызова — кэш/мок).
- `AfterModel` — переписать/отбросить ответ модели.
- `BeforeToolSelection` — сузить набор доступных tool-ов на этот ход
  (`toolConfig.allowedFunctionNames`, `mode: NONE|ANY|AUTO`); несколько hook-ов
  объединяются **по union**.
- `BeforeAgent` / `AfterAgent` — аналог `UserPromptSubmit` / `Stop`; `AfterAgent`
  с `decision: "deny"` запускает retry, `reason` становится новым промптом.
- `AfterTool` может вернуть `tailToolCallRequest` — harness сам вызывает ещё один tool.
- Есть флаг `sequential`: hook-и выполняются по очереди (каждый видит результат
  предыдущего) или параллельно с merge-правилами.

### 1.4 Cursor

`hooks.json`, события: `beforeShellExecution`, `beforeMCPExecution`,
`afterMCPExecution`, `preToolUse`/`postToolUse`, `beforeReadFile`, `afterFileEdit`,
`beforeSubmitPrompt`, `stop`, `afterAgentResponse` и др.

- Отдельные hook-и **на MCP-вызовы** с `tool_name`, `tool_input` и транспортом
  сервера (`url` или `command`).
- `stop` возвращает `followup_message` → авто-сабмит следующего сообщения; лимит
  итераций `loop_limit` (по умолчанию 5) — явный бюджет вместо флага.
- Флаг `failClosed` на hook — выбор fail-open/fail-closed для security-hook-ов.
- Разделение `user_message` (человеку) и `agent_message` (модели).

### 1.5 LangChain v1 middleware (in-process фреймворк)

Ближе всего к Python-реализации внутри нашего процесса.

- **Node-style:** `before_agent`, `before_model`, `after_model`, `after_agent` —
  возвращают частичное обновление state; могут сделать `jump_to: "end" | "model" | "tools"`.
- **Wrap-style:** `wrap_model_call`, `wrap_tool_call` — получают `handler` и решают,
  вызвать его 0, 1 или N раз (retry, cache, fallback, short-circuit).
- **Порядок детерминирован:** before — по порядку списка, wrap — вложенно (первый —
  самый внешний), after — в обратном порядке.
- Middleware может расширять схему state.
- Встроенные: `HumanInTheLoopMiddleware` (approve/edit/reject), `ToolCallLimit`,
  `ModelCallLimit`, `ToolRetry`, `ModelRetry`, `ModelFallback`, `PIIMiddleware`,
  `LLMToolSelector`, `Summarization`, `ContextEditing`.

### 1.6 OpenAI Agents SDK — guardrails

- **Input guardrails** — параллельно с агентом (быстро, но токены уже потрачены) или
  blocking; **output guardrails** — после финального ответа.
- **Tool guardrails** (input/output) на `FunctionTool` и локальных MCP-серверах:
  `allow()`, `reject_content(message)` (не выполнять, сообщение уходит модели),
  `raise_exception()` (tripwire, прерывание run-а).
- Опция прогонять input-guardrail-ы **до** human approval
  (`pre_approval_tool_input_guardrails`), чтобы человеку не показывали заведомо
  запрещённое.

### 1.7 Google ADK callbacks

`before_agent/after_agent`, `before_model/after_model`, `before_tool/after_tool`.
Правило одно и очень простое: **callback вернул `None` → шаг выполняется как обычно;
вернул объект → шаг пропускается и объект используется как его результат**
(например, `before_tool_callback` возвращает dict — это и есть результат tool-а).

### 1.8 Сводка

| Концепция | Claude Code / SDK | Codex | Gemini CLI | Cursor | LangChain | Agents SDK | ADK |
|---|---|---|---|---|---|---|---|
| Pre-tool deny + причина модели | ✅ | ✅ | ✅ | ✅ | wrap | reject_content | return obj |
| Переписать аргументы | `updatedInput` | `updatedInput` | `tool_input` | `updated_input` | wrap | — | mutate |
| Переписать результат | `updatedToolOutput` | block+feedback | exit 2 | — | wrap | output guardrail | return obj |
| Отдельное событие «нужно одобрение» | `PermissionRequest` | `PermissionRequest` | — | ask | HITL mw | pre-approval | — |
| Блок завершения | `Stop` | `Stop` → continuation | `AfterAgent` retry | `followup_message` | `after_agent` | output guardrail | `after_agent` |
| Защита от цикла на Stop | `stop_hook_active` | `stop_hook_active` | — | `loop_limit` | — | — | — |
| Хуки вокруг модели | — | — | Before/AfterModel, ToolSelection | — | before/after/wrap model | — | before/after model |
| Порядок нескольких hook-ов | параллельно, deny wins | параллельно | seq / parallel | — | детерминированный stack | — | цепочка |
| Fail-closed по таймауту | PreToolUse — да | — | — | `failClosed` | — | — | — |
| Trust | workspace trust | trust по хэшу | enable flag | — | код | код | код |

---

## 2. Что уже есть в AutoBrowser (hook-подобные точки)

Движок уже содержит «hook-и», просто зашитые, а не расширяемые:

| Точка | Где | Что делает сейчас | Аналог |
|---|---|---|---|
| Классификация tool call | `classify_tool_request` в `src/agent_loop/execution/policy.py:64`, вызов в `_run_tool_turn` (`src/agent_loop/execution/loop.py:399`) | `blocked` / `needs_human` / allow по `BLOCKED_TOOL_MARKERS`, stale-snapshot, identical-outcome | `PreToolUse` (built-in) |
| Отказ → фидбек модели | `policy_updates` (`policy.py`) | tool-message с причиной, `consecutive_failures + 1` | `permissionDecisionReason` |
| Human approval | `self._human_input` (`loop.py:408`), по умолчанию `_deny_human_input` (`loop.py:111`) | всегда deny в headless | `PermissionRequest` |
| Переписывание запроса/результата | `ToolCallNormalizer` в `ToolBroker.execute` (`src/agent_loop/execution/tools.py:111`) | адаптация схемы под MCP-сервер | wrap_tool_call (но схемный, не policy) |
| Завершение | `CompletionController` (`src/agent_loop/execution/guards.py:166`), ветка `done` в `run_turn` (`loop.py:240`) | terminal status | `Stop` |
| Наблюдение | `ObservationCompiler.compile` (`observation.py:267`) | tool result → observation/messages | `PostToolUse` |
| Телеметрия | `EventEmitter` + `EventType` (`src/agent_loop/events.py:14`) | `action.proposed`, `policy.decided`, `approval.requested`, `tool.started/finished` … | async hooks (observational) |

Вывод: **поток уже имеет правильную форму** (`action.proposed → policy → approval →
execute → observe`), не хватает (1) внешней регистрации проверок, (2) единого
контракта решения, (3) хука на завершение, (4) аудита решений hook-ов.

---

## 3. Рекомендуемая модель для AutoBrowser

### 3.1 Принципы

1. **Hooks ≠ events.** `EventEmitter` остаётся односторонней телеметрией (sync `emit`,
   без возвращаемого значения). Hook-и — это `await`-вызовы в **явных** точках движка,
   возвращающие решение. Не делать pub/sub-шину «emit(ToolCallRequested) → кто-то
   решит» — решение должно быть детерминированным и видимым в коде loop-а.
2. **Built-in policy не отключается hook-ами.** Snapshot/ref-инварианты
   (`classify_tool_request`) — это жёсткие правила, продублированные в промптах,
   observer-е и тестах. Hook-и **аддитивны**: могут только ужесточить (`deny`) или
   разрешить то, что built-in отправил на human approval. `allow` от hook-а не
   отменяет built-in `blocked`.
3. **Детерминизм важнее латентности.** В отличие от Claude Code (параллельно,
   недетерминированный порядок), у нас есть evals с baseline и `replay_trace.py`.
   Hook-и одного события выполняются **последовательно в порядке регистрации**,
   первый `deny` — short-circuit. Браузерное действие всё равно на порядки дольше
   Python-проверки.
4. **Hook получает проекцию, а не `LoopState`.** Вход — frozen, JSON-сериализуемый
   event (`session_id`, `goal_id`, `turn`, `tool`, `server`, `args`, …). `LoopState.apply`
   строгий, и hook не должен его знать; движок сам переводит `HookResult` в
   обновление state. Это же делает возможными command/http hook-и позже.
5. **Решение + ортогональные эффекты**, как у Claude, а не enum с `MODIFY`:
   `decision ∈ {allow, deny, ask, None}` + опциональные `updated_input`,
   `updated_output`, `additional_context`, `user_message`.
6. **Дешёвое раньше дорогого.** Python callable → command/mcp_tool → prompt.
   Agent-hook-и не нужны: при `gpt-oss:20b` это ещё один недетерминированный loop.
7. **Выключено по умолчанию**, включается только через `config.yaml`/env.

### 3.2 Где hook-и встают в turn

```text
TurnController._run_tool_turn
  action.proposed
  classify_tool_request            ← built-in policy (не отключается)
    blocked → policy_updates → return
  ToolBroker.prepare(request)      ← НОВОЕ: normalize_request + резолв tool/server
  hooks.run(PreToolUse)            ← видит ровно то, что будет выполнено
    deny  → policy_updates("blocked", reason) → return      (тот же путь, что built-in)
    ask   → needs_human
    allow → «нет возражений» (needs_human НЕ обходит — только через PermissionRequest,
            см. план реализации §4.2)
    updated_input → повторная schema-валидация, hook-и НЕ перезапускаются
  needs_human → hooks.run(PermissionRequest) → иначе self._human_input
  ToolBroker.invoke(prepared)
  normalize_result
  hooks.run(PostToolUse | PostToolUseFailure)
    updated_output / additional_context
  ObservationCompiler.compile
```

Почему hook-и **после** нормализации: normalizer переименовывает tool и фильтрует
аргументы под схему сервера (`BrowserToolNormalizer`). Hook, проверивший
*до-нормализационный* запрос, проверил не то, что реально выполнится. Отсюда
предложение разрезать `ToolBroker.execute` на `prepare` / `invoke` — единственное
изменение в существующем контракте.

**Hook-и не живут в MCP Manager** — это совпадает с
[MCP Manager system design](2026-09-23-mcp-manager-system-design-2_0.md), где
hooks/middleware/permissions явно вынесены за пределы Manager. Manager —
capability/execution, hook engine — lifecycle/control.

**Matcher по серверу.** Браузерные tool-ы экспонируются без префикса
(`MCPToolSource(unprefixed_servers=[browser server])`), поэтому regex по имени
(`mcp__server__.*` в стиле Claude) у нас не сработает. Matcher должен смотреть на
метаданные каталога: `{"server": "playwright", "tool": "browser_navigate"}`, а
`prepare()` должен возвращать происхождение tool-а.

### 3.3 Минимальный набор событий (MVP)

Не копировать 30 событий Claude Code. Для AutoBrowser достаточно:

| Событие | Точка | Может | Зачем |
|---|---|---|---|
| `GoalStart` | `GoalRunner.run` до `native_task_runner` | deny задачи, `additional_context` | доменные подсказки, запрет задач |
| `PreToolUse` | см. §3.2 | allow/deny/ask, `updated_input`, context | URL-политика, rate-limit, аргументы |
| `PermissionRequest` | вместо/до `_human_input` | allow/deny | авто-одобрение в batch/eval по allowlist |
| `PostToolUse` / `PostToolUseFailure` | после `normalize_result` | `updated_output`, context | redaction, prompt-injection, артефакты |
| `Stop` | ветка `done` в `run_turn`, до `CompletionController` | block + reason (с бюджетом) | проверка финального ответа |
| `GoalEnd` | `GoalRunner` после terminal status | только наблюдение | trace summary, уведомления |

Позже, по мере необходимости: `SessionStart`/`SessionEnd` (`SessionRuntime`),
`PreCompact` (`src/harness/memory.py` — архив перед обрезкой истории),
`BeforeModel`/`AfterModel` (Gemini/LangChain-стиль) — только если `ModelDriver`
не покрывает валидацию.

### 3.4 Stop hook и `CompletionController`

`CompletionController` остаётся **единственным** владельцем terminal-решения.
`Stop`-hook не завершает run сам, а может только **отклонить `done`**:

- причина добавляется в `messages` как фидбек (как continuation prompt в Codex),
  решение возвращается в `tool_call`/`replan`;
- бюджет — новый счётчик `stop_blocks` в `LoopState` (task-local, сбрасывается между
  задачами) и настройка `hooks.max_stop_blocks` (≈3, как `loop_limit` у Cursor);
  во входе hook-а — `stop_hook_active: bool` (как у Claude/Codex);
- после исчерпания бюджета — `done` принимается, а факт отказа попадает в события;
- каждый продолжающий ход расходует общий `turn_cap`.

Дешёвые детерминированные Stop-проверки для браузерного агента: непустой
`final_answer`; числа/цены/названия из ответа присутствуют в последнем observation
(grounding-проверка без LLM); в eval-режиме — assertions сценария.

### 3.5 Агрегация, ошибки, таймауты

- Последовательно, в порядке регистрации; `deny` — short-circuit.
- `updated_input` нескольких hook-ов применяется цепочкой (как wrap-stack у LangChain),
  каждый следующий видит уже переписанный запрос.
- Timeout/исключение: per-hook `fail_closed` (Cursor). По умолчанию `PreToolUse`,
  `PermissionRequest` — fail-closed (tool не выполняется, причина модели);
  `PostToolUse`, `GoalEnd` — fail-open; `Stop` — «нет решения» (как Claude).
- Отказ hook-а идёт через тот же `policy_updates`, что и built-in `blocked`, значит
  учитывается в `consecutive_failures` и `max_ineffective_actions` — hook не может
  зациклить loop сильнее, чем built-in policy.

### 3.6 Аудит

- Новый `EventType` `hook.decided` с `{event, hook_id, decision, reason, duration_ms,
  modified: bool}`; аргументы — через существующий `redact_json_safe`
  (`browser_type` может нести пароль).
- `replay_trace.py` должен показывать решения hook-ов; eval baseline не должен
  меняться при выключенных hook-ах.

### 3.7 Типы hook-ов и доверие

1. **Python callable** (MVP) — `async def(event) -> HookResult | None`, регистрируется
   в коде или через `module:function` в конфиге.
2. **command** — JSON в stdin / JSON в stdout, совместимый с форматом Claude Code
   (`hookSpecificOutput.permissionDecision` …), чтобы переиспользовать чужие скрипты.
   На Windows — exec-форма без shell.
3. **mcp_tool** — hook = вызов tool-а зарегистрированного MCP-сервера (как в Claude
   Code). Естественно ложится на MCP Manager: внешний policy-сервер без нового
   транспорта.
4. **prompt** — только для редких дорогих проверок и только после дешёвых.

Доверие: hook-и берутся только из `config.yaml` / `AUTOBROWSER_CONFIG_FILE`
(git-ignored, личный профиль); command/mcp_tool hook-и — явный opt-in. Хэш-trust
в стиле Codex избыточен для однопользовательского REPL, но стоит логировать хэш
конфигурации hook-ов в `session.json`.

### 3.8 Размещение по слоям

| Что | Где | Почему |
|---|---|---|
| Контракты: `HookEvent`, `HookResult`, `HookDecision`, имена событий | `src/contracts.py` (или нейтральный `src/hooks/contracts.py`) | нейтральный лист, не импортирует engine/harness/browser |
| `HookEngine`: registry, matcher, runner, timeouts, агрегация | `src/harness/hooks.py` | инфраструктура — в harness, по правилам слоёв |
| Проброс в движок | `EngineResources.from_harness` → `TurnController` | как `tool_registry`, `events` |
| Точки вызова | `TurnController._run_tool_turn`, ветка `done`, `GoalRunner` | движок владеет только reasoning/routing/execution/observation |
| Браузерные hook-и (URL-политика, injection-скан снапшота) | `src/browser/hooks.py` | браузерная специфика не попадает в engine |
| Настройки | новая секция `hooks` в `src/config.py` (`enabled`, `max_stop_blocks`, `default_timeout_seconds`, `registry`) + `.env.example` + `tests/test_config.py` | no scattered constants |

Пример конфигурации:

```yaml
hooks:
  enabled: true
  max_stop_blocks: 3
  registry:
    - event: pre_tool_use
      match: { server: playwright, tool: browser_navigate }
      handler: src.browser.hooks:url_policy
      fail_closed: true
    - event: post_tool_use
      match: { tool: browser_snapshot }
      handler: src.browser.hooks:prompt_injection_scan
    - event: stop
      handler: src.agent_loop.hooks:grounded_final_answer
```

---

## 4. Кандидаты на первые hook-и

1. **URL-политика** (`PreToolUse`, `browser_navigate`): allow/deny-список доменов,
   запрет `file://`, `chrome://`, `javascript:`. Сейчас есть только
   `BLOCKED_TOOL_MARKERS` по имени tool-а — это не про URL.
2. **Авто-одобрение в batch/eval** (`PermissionRequest`): заменяет
   `_deny_human_input` конфигурируемым allowlist-ом, без интерактивного HITL.
3. **Prompt-injection / PII в снапшоте** (`PostToolUse`, `browser_snapshot`): страница —
   недоверенный вход. Hook помечает или вырезает подозрительные инструкции и
   персональные данные до того, как их увидит модель. Главный специфичный для
   браузерного агента выигрыш.
4. **Доменные подсказки** (`GoalStart` / `PostToolUse` после навигации):
   `additional_context` вида «для ozon.ru используй `/search/?text=`» — детерминированная
   инъекция вместо роста системного промпта.
5. **Grounded final answer** (`Stop`): см. §3.4.
6. **Артефакт при ошибке** (`PostToolUseFailure`): скриншот/снапшот в
   `workspace/` сессии для диагностики.

---

## 5. Расхождения с исходной формулировкой (ChatGPT-контекст)

С общей идеей (hooks = lifecycle control, MCP = capability, hook-и не в MCP Manager,
matcher-ы, дешёвые проверки раньше дорогих, Stop hook) — согласие. Уточнения:

- **`Decision.MODIFY` как отдельный вердикт — не нужен.** У всех зрелых реализаций
  модификация ортогональна решению (`allow + updatedInput`).
- **Hook engine стоит не «перед MCP Manager», а между нормализацией и вызовом** внутри
  `ToolBroker` — иначе проверяется не тот запрос (§3.2).
- **Не event bus.** `emit(ToolCallRequested)` с подписчиками смешивает телеметрию и
  управление; у нас уже есть `EventEmitter` для первого.
- **Параллельный запуск (как в Claude Code) нам не подходит** из-за evals/replay —
  выбираем последовательный детерминированный порядок.
- **Agent-hook-и откладываем** — для 20B-модели это дорого и недетерминированно.
- **Built-in policy остаётся** и не отключается hook-ами (§3.1 п.2).

---

## 6. Открытые вопросы

- Перевести ли `classify_tool_request` в «built-in hook-и» с `removable=False`
  (единый pipeline) или оставить отдельным шагом перед hook-ами?
- Нужен ли `BeforeToolSelection` (сужение набора tool-ов на ход, как в Gemini) для
  борьбы с зацикливанием на search-affordance?
- Как `Stop`-block взаимодействует с `max_unchanged_snapshots` terminal-ом, который
  тоже выдаёт `done` через `observation_terminal_update`? Вероятно, `Stop` должен
  срабатывать только на `done` от модели, а не на guard-terminal.
- Формат command-hook-ов: полная совместимость с Claude Code JSON или свой
  упрощённый?

## 7. Следующие шаги

Детальный план: [Lifecycle hooks — план реализации](../development/2026-09-30-lifecycle-hooks-implementation-plan.md).

1. ADR «Lifecycle hooks engine» (MVP: Python callables, 6 событий, sequential,
   disabled by default).
2. Spike: разрезать `ToolBroker.execute` на `prepare`/`invoke` без изменения
   поведения (тесты `tests/test_mcp_tools_bridge.py`).
3. `HookEngine` + `hook.decided` + секция `hooks` в настройках;
   `tests/test_agent_loop_hooks.py` на fake MCP server.
4. Первый hook — URL-политика; второй — `PermissionRequest` для batch.
5. Обновить `docs/diagrams/agent-runtime-flow.md` и `harness-boundaries.md`.

---

## Источники

- [Claude Code — Hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Agent SDK — Hooks](https://code.claude.com/docs/en/agent-sdk/hooks)
- [OpenAI Codex — Hooks](https://learn.chatgpt.com/docs/hooks) (ранее developers.openai.com/codex/hooks)
- [Codex CLI Hooks: Complete Guide](https://codex.danielvaughan.com/2026/04/15/codex-cli-hooks-complete-guide-events-policy-patterns/)
- [Gemini CLI — Hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md)
- [Cursor — Hooks](https://cursor.com/docs/hooks), [GitButler: Cursor hooks deep dive](https://blog.gitbutler.com/cursor-hooks-deep-dive)
- [LangChain — Custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom), [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- [OpenAI Agents SDK — Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- [Google ADK — Callbacks](https://adk.dev/callbacks/types-of-callbacks/)
- [Habr: Hooks в LLM-агентах](https://habr.com/ru/companies/rostelecom/articles/1028570/)
