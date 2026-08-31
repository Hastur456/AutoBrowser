# Итог исследования

С учётом Glossary и Architecture Overview текущий проект уже имеет почти все необходимые границы для миграции:

- `SessionRuntime` — session lifecycle;
- `GoalRunner` — task/goal lifecycle;
- `BrowserHarness` — composition root;
- `PolicyEngine` — policy boundary;
- `ToolRegistry` — tool resolution;
- `Executor` — execution;
- `Observer` — observation;
- `MemoryManager` — checkpoint/history;
- `FakeBrowserProvider` — deterministic test backend.

Главная проблема не в отсутствии компонентов, а в том, что **LangGraph всё ещё владеет control flow**, а переходы между `agent → policy → executor → observe` зашиты в граф.

Целевой `AgentLoopEngine` должен заменить именно эту часть, не поглощая session lifecycle, browser provider lifecycle или artifact management.

Codex и Claude Code подтверждают, что основной runtime loop — это не постоянная последовательность `plan → agent → policy → executor → observe`, а повторяемый цикл:

```text
assemble context
→ invoke model
→ classify model response
→ execute requested tools
→ return tool results
→ invoke model again
→ finish on response without tool calls
```

Claude SDK прямо описывает такой порядок: Claude получает запрос, system prompt, tools и историю; возвращает текст и/или tool calls; SDK выполняет tools; результаты возвращаются модели; цикл продолжается до ответа без вызовов tools. [code.claude](https://code.claude.com/docs/ru/agent-sdk/agent-loop)

Codex реализует тот же паттерн через последовательное расширение input/output items Responses API: результат tool call добавляется в следующий запрос модели. [openai](https://openai.com/ru-RU/index/unrolling-the-codex-agent-loop/)

***

# 1. Границы будущего AgentLoopEngine

## Что остаётся снаружи

```text
SessionRuntime
    → SessionContext
        → GoalRunner
            → AgentLoopEngine
```

### SessionRuntime

Остаётся владельцем:

- process-scoped session;
- CLI event loop;
- MCP lifecycle;
- task history;
- session workspace;
- session-scoped resources;
- session cancellation;
- переключения между задачами.

Текущая документация правильно подчёркивает, что `SessionRuntime` не является task solver. Его не нужно переносить в `AgentLoopEngine`.

### GoalRunner

Остаётся границей одной задачи:

```text
one user task
    → one GoalRunRequest
    → one GoalRunResult
```

`GoalRunner` должен:

- создать и завершить `GoalState`;
- вызвать `AgentLoopEngine`;
- преобразовать результат engine в `GoalRunResult`;
- обработать `completed`, `blocked`, `failed`, `cancelled`;
- поддержать resume после approval;
- вернуть итог в `SessionRuntime`.

Текущая формулировка, что `GoalRunner` не должен выбирать actions, вызывать tools и менять routing, корректна. Его не следует расширять до второго agent engine.

### AgentLoopEngine

`AgentLoopEngine` должен владеть только execution loop одной цели:

```text
GoalState + transcript
    → model turns
    → actions
    → tool executions
    → observations
    → final turn result
```

Он не должен владеть:

- session ID;
- MCP process;
- browser connection;
- CLI streaming;
- task history;
- artifact filesystem;
- provider lifecycle.

***

# 2. Что происходит с текущим графом

Текущий graph:

```text
START
  → plan
  → agent
  → policy
  → executor
  → observe
  → agent
```

не нужно переносить в engine один к одному.

Правильное отображение:

| LangGraph-компонент | Целевой компонент |
|---|---|
| `plan` | Optional `PlanState` / `PlanController` |
| `agent` | `ModelDriver` + response normalization |
| `policy` | `ActionExecutionGate` |
| `human_input` | `ApprovalController` |
| `executor` | `ToolBroker` |
| `observe` | `ObservationCompiler` |
| Graph routers | `AgentLoopEngine` |
| Graph checkpoint | `TranscriptStore` / `EventJournal` / optional state snapshot |
| `AgentState` | Разделённые `LoopState`, `GoalState`, `BrowserState`, `TurnState` |

Ключевой вывод: **graph nodes становятся адаптируемыми компонентами, а graph edges заменяются engine control flow**.

***

# 3. Планирование не должно быть обязательной стадией

Текущая документация определяет flow как:

```text
1. Plan
2. Select action
3. Apply policy
4. Execute
5. Observe
6. Repeat
```

Для текущего browser agent это полезная domain-модель. Но в Codex и Claude Code отдельный planner не является обязательной частью каждого прохода. Claude может сразу вернуть tool call, несколько tool calls, текст и tool calls или финальный ответ. [code.claude](https://code.claude.com/docs/ru/agent-sdk/agent-loop)

Поэтому в новом engine:

```text
plan
```

не должен быть обязательным шагом перед каждым action.

Нужна такая семантика:

```text
initial goal
    → optional initial plan
        → model/action loop
            → optional replan
                → model/action loop
```

## Когда нужен PlanController

`PlanController` оправдан, если:

- задача сложная;
- план уже есть в `GoalState`;
- модель явно запросила replan;
- произошла ошибка или потеря прогресса;
- нужно изменить стратегию после ineffective action;
- обнаружен stale ref или динамическое изменение страницы.

## Когда planner не нужен

Planner не нужен для:

- уже однозначного snapshot lookup;
- простой browser action;
- продолжения после обычного успешного tool result;
- финального ответа;
- каждого нового snapshot.

Иначе перенос `plan → agent → ... → plan` в engine сохранит главную проблему graph-first архитектуры: лишние turns и раздробленные microsteps.

***

# 4. Новая внутренняя структура

Рекомендуемая схема:

```text
GoalRunner
    │
    └── AgentLoopEngine
            │
            ├── LoopState
            ├── TurnController
            │       │
            │       ├── ContextAssembler
            │       ├── ModelDriver
            │       ├── ResponseNormalizer
            │       ├── ResponseClassifier
            │       ├── ActionBatchDispatcher
            │       ├── ObservationCompiler
            │       ├── TranscriptAppender
            │       └── TurnCompletionResolver
            │
            ├── PlanController
            ├── ActionExecutionGate
            │       ├── ActionValidator
            │       ├── HookRunner
            │       ├── PermissionEngine
            │       ├── ApprovalController
            │       └── ToolBroker
            │
            ├── ContextManager
            ├── GoalCompletionResolver
            └── EventJournal
```

## Почему нужен TurnController

Один `GoalRunner` execution может содержать много model turns:

```text
goal
  → model asks for snapshot
  → snapshot observation
  → model asks for click
  → click observation
  → model asks for extraction
  → extraction observation
  → model returns final answer
```

`TurnController` должен обрабатывать один вызов модели и его immediate tool batch. `AgentLoopEngine` должен повторять turns до terminal outcome.

Это лучше, чем один большой `while`, потому что появляется явная граница:

```text
TurnResult
```

Например:

```text
TurnResult:
    continue
    completed
    waiting_for_approval
    blocked
    failed
    cancelled
    limit_reached
```

***

# 5. Состояние нужно разделить

Сейчас `AgentState` исторически смешивает:

- task;
- plan;
- final answer;
- browser state;
- observation;
- policy state;
- retry counters;
- tool request/result;
- errors.

Для нового engine нужен минимум следующий набор.

## SessionState

Принадлежит `SessionContext`.

```text
session_id
workspace
artifacts
task_history
shared durable messages
browser runtime handles
```

## GoalState

Принадлежит `GoalRunner`.

```text
goal_id
task_id
task_text
terminal_status
acceptance_status
goal_started_at
goal_finished_at
turn_count
tool_call_count
budget
```

`GoalState` не должен хранить временный raw model response или последний low-level provider exception.

## LoopState

Принадлежит `AgentLoopEngine`.

```text
current_turn_id
loop_status
last_model_response
pending_actions
pending_approval
last_observations
retry_state
context_version
compaction_state
```

## BrowserState

Принадлежит browser/domain layer:

```text
current_url
current_snapshot
snapshot_fingerprint
available_refs
last_browser_action
invalid_ref_state
```

`ref` должен оставаться browser-specific ephemeral data. Он не должен становиться универсальным идентификатором agent state.

## Transcript

Принадлежит model context layer:

```text
user messages
assistant output items
tool calls
tool results
observations
compaction markers
```

## EventJournal

Содержит immutable lifecycle records, но не является source-of-truth для каждого runtime read.

***

# 6. ContextAssembler и текущий ContextBuilder

Текущий `ContextBuilder` строит initial graph state и inject-ит system prompt. Для нового engine его лучше разделить на две ответственности:

```text
InitialStateFactory
    → initial Goal/Loop state

ContextAssembler
    → model-visible context перед каждым model call
```

## InitialStateFactory

Отвечает за:

- создание initial `GoalState`;
- task metadata;
- начальное browser state;
- стартовые counters;
- initial transcript entry.

## ContextAssembler

Перед каждым вызовом модели собирает:

```text
stable context:
    core instructions
    browser semantics
    tool definitions
    active skill metadata

dynamic context:
    user task
    compacted transcript
    latest observation
    current snapshot
    refs
    plan state
    policy hints
    retry/recovery state
```

Codex использует устойчивый input prefix, к которому добавляются новые output items и tool results; это важно для prompt caching и контекстной эффективности. [openai](https://openai.com/ru-RU/index/unrolling-the-codex-agent-loop/)

Для AutoBrowser это означает:

- не пересобирать browser rules хаотично;
- не передавать каждый раз весь raw snapshot;
- сохранять свежий snapshot и refs только в нужном context block;
- отделить transcript от current observation;
- явно маркировать snapshot version.

***

# 7. Action model

Текущий набор:

```text
AnswerAction
ToolCallAction
AskUserAction
UpdatePlanAction
DelegateAction
CompactMemoryAction
StopAction
```

нужно разделить на три категории.

## Model-proposed actions

```text
ToolCallAction
AskUserAction
DelegateAction
AnswerAction
```

## Runtime decisions

```text
Continue
Complete
Blocked
WaitingForApproval
Cancelled
LimitReached
```

Они не должны приходить от модели как обычные actions.

## Runtime commands

```text
CompactContext
Replan
RetryModel
AbortTool
```

Они могут возникать из engine policy, limits или recovery logic.

### Почему `StopAction` лучше убрать

В Codex и Claude Code отсутствие дальнейших tool calls является нормальным сигналом окончания model loop. Claude SDK возвращает финальный результат после assistant response без tool calls. [code.claude](https://code.claude.com/docs/ru/agent-sdk/agent-loop)

Поэтому обычный финальный ответ должен быть:

```text
Assistant response without tool calls
    → FinalAnswer
```

а не:

```text
model → StopAction
```

`StopAction` можно оставить только как internal normalized outcome для cancellation или explicit runtime stop.

### Почему `CompactMemoryAction` лучше убрать

Compaction обычно запускается из-за token budget/context limit. Это решение runtime, а не domain action модели. Codex отдельно обрабатывает compaction как runtime behavior при приближении к лимиту контекста. [openai](https://openai.com/ru-RU/index/unrolling-the-codex-agent-loop/)

***

# 8. Browser-specific semantics внутри общего loop

Browser semantics должны остаться в `src/browser/`, но подключаться к общему engine через typed contracts.

```text
Model
    → canonical BrowserAction
        → ToolBroker
            → BrowserProvider
                → PlaywrightMCPBrowserProvider
                    → raw MCP tool
```

## Что должно быть provider-neutral

```text
BrowserAction
BrowserResult
BrowserErrorCode
ToolSpec
Observation
```

## Что должно оставаться Playwright-specific

```text
ref
target
element
snapshot line format
MCP tool name
```

`PlaywrightMCPBrowserProvider` правильно находится за provider boundary. Он должен заниматься:

- mapping canonical action name;
- argument adaptation;
- `ref`/`target` normalization;
- provider errors;
- invalid-ref normalization.

Он не должен решать:

- является ли действие ineffective;
- достигнута ли цель;
- нужен ли replan;
- следует ли показать approval.

Эти решения должны быть разделены:

```text
Provider
    → raw normalized BrowserResult

ObservationCompiler
    → compact Observation

GoalCompletionResolver
    → goal decision
```

***

# 9. Observer нужно разделить на два слоя

Сейчас `Observer` одновременно:

- переводит tool result в compact state;
- сохраняет snapshot;
- очищает stale snapshot;
- отслеживает invalid refs;
- обнаруживает ineffective actions;
- иногда вызывает observer LLM.

Для миграции это слишком много ответственности.

Рекомендуемое разделение:

```text
ToolResult
    → BrowserObservationAdapter
        → provider-neutral Observation
            → BrowserStateReducer
            → ProgressDetector
            → ContextProjection
```

## BrowserObservationAdapter

Только нормализует результат:

```text
BrowserResult
    → Observation
```

## BrowserStateReducer

Обновляет:

- current snapshot;
- visible fingerprint;
- available refs;
- current URL;
- last action metadata.

## ProgressDetector

Определяет:

- ineffective action;
- repeated action;
- stale ref;
- page state changed;
- no progress;
- recoverable browser error.

## ContextProjection

Создаёт компактное представление для следующего model call:

```text
compact observation
```

Это важно: `ObservationCompiler` не должен одновременно быть и runtime state reducer, и prompt summarizer.

***

# 10. Policy должен быть execution gate

Текущий `PolicyEngine` уже является хорошей границей, но в новой архитектуре он должен быть включён в `ActionExecutionGate`.

```text
ProposedAction
    → validate
    → resolve ToolSpec
    → run pre-hooks
    → PolicyEngine
    → approval if required
    → ToolBroker
```

Политика должна получать не только имя инструмента, но и структурированный контекст:

```text
PolicyInput:
    action
    tool_spec
    goal_state
    browser_state
    session_config
    previous_actions
    risk_context
```

Это позволит заменить marker-based logic:

```text
if "payment" in tool_name
```

на scoped policy:

```text
tool scopes:
    browser.read
    browser.action
    browser.navigation
    external_post
    credential
    destructive
```

## Текущие browser checks в новой модели

| Текущая проверка | Новая ответственность |
|---|---|
| Missing tool request | `ActionValidator` |
| Sensitive marker | `PermissionEngine` |
| Repeated snapshot | `ProgressDetector` + policy |
| Ineffective browser actions | `ProgressDetector` + policy |
| Invalid ref | `BrowserProvider` + `ActionExecutionGate` |
| Human input | `ApprovalController` |
| Tool result error | `ToolResult` + `ObservationCompiler` |

Отказ должен быть представлен как структурированный результат:

```text
ToolResult(status="denied")
```

и попасть в следующий model context, а не просто изменить внутренний graph route.

***

# 11. Goal completion и legacy outcomes

Текущая transitional схема:

```text
AgentState
    → LegacyAgentStateObservationCompiler
        → GoalState
```

действительно является migration debt.

Целевой путь:

```text
AgentLoopEngine
    → AgentLoopResult
        → GoalRunner
            → GoalRunResult
```

## Новые terminal types

Нужен provider-neutral результат:

```text
AgentLoopResult:
    status
    final_answer
    last_observation
    latest_snapshot
    transcript_ref
    usage
    turn_count
    tool_call_count
    pending_approval
    error
```

`GoalRunner` должен преобразовать его в публичный `GoalRunResult`, но не восстанавливать terminal state через inspection старого `AgentState`.

## Completion должен иметь два уровня

### Loop completion

Завершился текущий модельный turn:

```text
final response without tool calls
```

### Goal completion

Цель действительно завершена:

```text
loop completed
+ task acceptance condition satisfied
```

Для AutoBrowser это может означать:

- данные извлечены;
- результат сохранён в ArtifactRegistry;
- browser operation завершена;
- есть финальный текст;
- нет unresolved approval/error;
- task status установлен.

Нельзя считать любое `final_answer` доказательством достижения цели.

***

# 12. ArtifactRegistry в новой архитектуре

`ArtifactRegistry` не должен быть частью core loop state, но должен быть доступен через tool/runtime context.

```text
Tool execution
    → artifact produced
        → ArtifactRegistry
        → ToolResult.artifact_refs
        → Observation
        → final GoalRunResult
```

Модель не должна получать raw artifact content, если достаточно reference:

```text
artifact_id
artifact_type
path
mime_type
size
```

`ArtifactRegistry` должен быть session-owned, как сейчас, а `AgentLoopEngine` должен только:

- принимать artifact references из `ToolResult`;
- добавлять их в observation;
- передавать их в `GoalRunResult`.

***

# 13. Event model для нового engine

Текущий набор событий хороший, но для нового loop нужны более точные границы.

## Session-level

```text
session.started
session.resumed
session.closed
```

## Goal-level

```text
goal.started
goal.completed
goal.blocked
goal.failed
goal.cancelled
```

## Turn-level

```text
turn.started
turn.context_assembled
turn.model_requested
turn.model_responded
turn.response_normalized
turn.response_classified
turn.completed
turn.interrupted
```

## Action-level

```text
action.proposed
action.validated
action.rejected
action.dispatched
```

## Policy-level

```text
policy.decided
approval.requested
approval.granted
approval.denied
approval.expired
```

## Tool-level

```text
tool.started
tool.finished
tool.failed
tool.denied
```

## Observation-level

```text
observation.compiled
browser.snapshot_updated
browser.progress_detected
browser.stale_ref_detected
```

## Context-level

```text
context.updated
context.compacted
context.compaction_failed
```

`EventJournal` должен фиксировать события до и после side effect, чтобы можно было отличить:

```text
tool.proposed
tool.denied
```

от:

```text
tool.started
tool.failed
```

Это особенно важно для проверки утверждения:

```text
denied action never reached provider
```

***

# 14. Пересмотр migration phases

## Phase 0 — architecture fence

Оставить без принципиальных изменений.

Добавить явное решение:

```text
LangGraph owns v1 execution only.
AgentLoopEngine owns v2 execution.
```

Также нужно зафиксировать mapping:

```text
AgentState → transitional compatibility only
```

## Phase 1 — event stream

Добавить `turn.*`, `response.*`, `context.*` и `action.*` события.

Не ограничиваться событиями graph nodes, иначе trace будет отражать текущую реализацию, а не будущую agent semantics.

## Phase 2 — scenario replay

Добавить к существующим browser evals:

- initial planning;
- direct tool call without planning;
- replan after stale ref;
- ineffective action block;
- approval pause/resume;
- model final answer without tools;
- malformed action;
- tool error recovery;
- context compaction;
- task boundary reset.

## Phase 3 — context split

Разделить:

```text
ContextBuilder
    → InitialStateFactory
    + ContextAssembler
```

Старый prompt можно сначала рендерить как один block, но публичный контракт должен уже быть block-based.

## Phase 4 — response/action contracts

Ввести:

```text
RawModelResponse
NormalizedModelResponse
ResponseClassification
ProposedAction
ActionBatch
```

На этом этапе LangGraph `agent` node можно адаптировать к этим контрактам, не меняя graph routing.

## Phase 5 — execution gate

Объединить:

```text
PolicyEngine
human_input
Executor
ToolRegistry
```

в adapter-level execution path:

```text
ActionExecutionGate
    → ToolBroker
```

Старые policy tests должны проходить через новый gate.

## Phase 6 — observation split

Разделить старый `observe` на:

```text
ToolResultNormalizer
BrowserStateReducer
ProgressDetector
ObservationCompiler
```

Это наиболее важная browser-specific migration phase.

## Phase 7 — explicit Goal/Loop result

Сначала новый engine должен возвращать provider-neutral `AgentLoopResult`, но `GoalRunner` может пока оставаться прежним публичным boundary.

Удалить зависимость от:

```text
LegacyAgentStateObservationCompiler
CompletionGuard
AgentState.final_answer
AgentState.decision
```

после появления parity tests.

## Phase 8 — AgentLoopEngine behind feature flag

Рекомендуемые режимы:

```text
v1 = LangGraph
v2 = AgentLoopEngine + existing adapters
```

На этом этапе v2 может использовать:

- текущий `ToolRegistry`;
- текущий `BrowserProvider`;
- текущий `PolicyEngine`;
- текущий `FakeBrowserProvider`;
- новый `ObservationCompiler`;
- новый `EventJournal`.

Не нужно одновременно переписывать provider layer и engine.

## Phase 9 — remove graph ownership

После прохождения evals:

```text
AgentLoopEngine
    → ModelDriver
    → ActionExecutionGate
    → ToolBroker
    → ObservationCompiler
```

LangGraph может остаться только:

- как v1 compatibility engine;
- как optional subgraph;
- как migration adapter;
- либо быть удалён.

***

# 15. Что не следует переносить в первый AgentLoopEngine

В первую реализацию не нужно включать:

- полноценную semantic memory;
- сложный planner;
- dynamic subagents;
- arbitrary hooks из shell/HTTP;
- multi-agent coordination;
- automatic self-reflection;
- универсальный `UpdatePlanAction`;
- runtime-generated browser policy из prompt;
- service API и UI control plane.

Минимальная v2 должна доказать только:

```text
model
→ action
→ policy
→ tool
→ observation
→ next model
→ terminal result
```

Если сразу добавить skills, subagents, hooks, memory и API, будет невозможно определить, какая часть миграции улучшила или сломала browser reliability.

***

# 16. Конечный mapping для AutoBrowser

Итоговая модель должна выглядеть так:

```text
SessionRuntime
    owns:
        session lifecycle
        task history
        MCP/browser resources
        workspace/artifacts
        CLI event loop

GoalRunner
    owns:
        one task/goal lifecycle
        GoalState
        terminal GoalRunResult
        resume/cancel boundary

AgentLoopEngine
    owns:
        repeated model turns
        loop status
        turn budget
        next-step control flow

ContextAssembler
    owns:
        model-visible context

ModelDriver
    owns:
        provider API and streaming

ResponseNormalizer
    owns:
        API-specific response conversion

ResponseClassifier
    owns:
        final vs tool calls vs invalid output

ActionExecutionGate
    owns:
        validation
        hooks
        policy
        approval

ToolBroker
    owns:
        tool resolution and execution

ObservationCompiler
    owns:
        model-facing observation

BrowserStateReducer
    owns:
        snapshot, refs, fingerprints, browser state

CompletionController
    owns:
        turn and goal completion decisions

EventJournal
    owns:
        durable lifecycle trace

TranscriptStore
    owns:
        model-visible history

ArtifactRegistry
    owns:
        durable task outputs
```

Финальная control flow:

```text
GoalRunner
    → AgentLoopEngine
        → ContextAssembler
        → ModelDriver
        → ResponseNormalizer
        → ResponseClassifier

        if final response:
            → CompletionController
            → AgentLoopResult
            → GoalRunner

        if tool batch:
            → ActionExecutionGate
            → ToolBroker
            → ToolResult
            → BrowserStateReducer
            → ObservationCompiler
            → TranscriptStore
            → ContextAssembler
            → next model turn
```

# Главный архитектурный вывод

Текущий проект уже имеет правильные domain boundaries. Миграция должна не создавать новый runtime с нуля, а заменить graph control flow на explicit turn controller.

Наиболее безопасная стратегия:

```text
GoalRunner остаётся
BrowserHarness временно остаётся
ToolRegistry остаётся
PolicyEngine сначала адаптируется
BrowserProvider остаётся
FakeBrowserProvider остаётся
Observer разделяется
AgentState постепенно исчезает
Graph edges заменяются AgentLoopEngine
```

То есть целевая миграция:

```text
LangGraph graph as runtime
    ↓
LangGraph components as adapters
    ↓
AgentLoopEngine as runtime
```

При этом `plan`, `policy`, `executor` и `observe` не должны исчезать как способности. Они должны перестать быть обязательными graph nodes и стать компонентами явного engine loop.