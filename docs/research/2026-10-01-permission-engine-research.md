# PermissionEngine — research для AutoBrowser

Дата: 2026-10-01. Скоуп: как устроены permissions/approvals в agent harness-ах
(Claude Code, OpenAI Codex CLI, Gemini CLI, OpenAI Agents SDK, MCP tool annotations,
Playwright MCP) и что из этого переносится в engine-native loop AutoBrowser.
Документ исследовательский: решение оформляется отдельным ADR. Продолжает
«Phase 5: ToolBroker And PermissionEngine» из
[Codex-Claude Runtime Migration Plan](2026-07-26-codex-claude-runtime-migration-plan.md)
и опирается на [Lifecycle Hooks Research](2026-09-30-lifecycle-hooks-research.md) /
[ADR lifecycle hooks](../decisions/2026-09-30-lifecycle-hooks-engine.md).

Входной материал — внешний research (Perplexity). Ниже он сверен с текущим кодом и с
актуальной документацией вендоров; расхождения собраны в §3.

---

## 0. Идея в одном абзаце

PermissionEngine — это **детерминированная функция авторизации** перед каждым tool call:
`(подготовленный запрос, контекст, правила, режим) → allow | ask | deny` + причина + id
сработавшего правила. Он не исполняет, не наблюдает, не судит о прогрессе и не является
sandbox-ом. Модель влияет на то, *что* агент пытается сделать; permissions — на то, *что
runtime разрешит*. Инструкции в промпте не меняют разрешения (формулировка Claude Code).

---

## 1. Обзор harness-ов

### 1.1 Claude Code

- **Правила** `permissions.allow | ask | deny` в settings; синтаксис `Tool`,
  `Tool(specifier)`, для MCP — `mcp__server`, `mcp__server__*`, `mcp__server__tool`.
  Deny/ask-правила поддерживают glob в имени инструмента и `Tool(param:value)`.
- **Порядок: deny → ask → allow; первое совпадение в этом порядке решает, специфичность
  не важна.** Узкий allow не вырезает исключение из широкого deny; ask побеждает allow.
- **Режимы:** `default` (Manual), `acceptEdits`, `plan`, `auto` (фоновый классификатор
  вместо человека), `dontAsk` (всё, что спросило бы, — deny; allow-правила работают),
  `bypassPermissions` (без промптов, кроме «actions no mode auto-approves»). Bypass/auto
  можно запретить (`disableBypassPermissionsMode`), обычно из managed settings.
- **Hooks не обходят правила:** deny- и ask-правила вычисляются независимо от ответа
  `PreToolUse`; hook с exit 2 блокирует до правил (сильнее allow). `PermissionRequest` —
  отдельная точка, где hook стоит вместо человека.
- **Слои настроек:** managed > CLI > local > project > user; project-`allow` применяется
  только после workspace trust, а `deny`/`ask` — всегда («они только ограничивают»).
- **Кэш одобрений** зависит от типа инструмента: правка файла — до конца сессии, Bash —
  навсегда «per repository and command», WebFetch — per domain.
- **Permissions ≠ sandbox:** sandbox (ОС-уровень) ограничивает только shell-процессы;
  permissions — все инструменты. «Defense-in-depth: sandbox работает даже если prompt
  injection обошёл принятие решений». MCP-инструмент с `requiresUserInteraction` спросит
  всегда (а в `dontAsk` — deny).

### 1.2 OpenAI Codex CLI

- Два ортогональных слоя: **`sandbox_mode`** (`read-only`, `workspace-write`,
  `danger-full-access`) и **`approval_policy`** (`on-request`, `never` или объект
  `granular` с ключами `sandbox_approval`, `rules`, `mcp_elicitations`,
  `request_permissions`, `skill_approval`). `untrusted` в актуальной документации
  помечен deprecated (→ `trust_level = "untrusted"` в project config).
- **Execpolicy rules** (Starlark, `~/.codex/rules/*.rules`):
  `prefix_rule(pattern=[...], decision="allow|prompt|forbidden", justification=...,
  match=[...], not_match=[...])`. При нескольких совпадениях — **самое строгое
  (forbidden > prompt > allow)**. `justification` показывается в промпте/отказе.
  `match`/`not_match` — встроенные self-tests правила, проверяемые при загрузке.
- Admin-слой (`requirements.toml`) может задавать только `prompt`/`forbidden`, **никогда
  allow** — администратор только ужесточает.
- «Approve for session» из TUI дописывает конкретное правило в `default.rules`.
- **MCP:** инструмент с аннотацией destructive требует одобрения, если read-аннотация не
  перевешивает; side-effecting app/MCP вызовы спрашивают даже вне shell/файлов.

### 1.3 Gemini CLI — policy engine

- TOML-правила (`~/.gemini/policies/*.toml`): `toolName`, `commandPrefix`/regex по
  аргументам, `modes = [...]`, `decision = allow | deny | ask_user`, `priority`.
- **Числовой приоритет** внутри **уровней** Default(1) < Extension(2) < Workspace(3) <
  User(4) < Admin(5): итог `tier + priority/1000`, побеждает наибольший. Workspace с
  `priority=999` проигрывает Admin с `priority=10`.
- Поучительный баг (#29051): при неразобранной shell-команде в YOLO `ask_user` молча
  становился `allow` — **ошибка разбора не должна ослаблять решение**.

### 1.4 OpenAI Agents SDK

- `needs_approval: bool | (ctx, args, call_id) -> bool` на инструменте. Run
  останавливается с `interruptions` (`ToolApprovalItem`), `RunState` сериализуется,
  приложение делает `approve`/`reject` и возобновляет **тот же** run. Есть sticky
  `always_approve`/`always_reject` в состоянии run-а; reject превращается в tool-результат
  для модели.
- SDK даёт только паузу: уведомления, таймауты и аудит — забота приложения.

### 1.5 MCP tool annotations и Playwright MCP

- MCP-аннотации `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` —
  **подсказки**; спецификация предупреждает не доверять им для недоверенных серверов.
  AutoBrowser уже читает `readOnlyHint` (`tool_is_read_only`, `src/harness/tools.py:52`).
- Playwright MCP имеет свой слой ограничений: `--allowed-origins` / `--blocked-origins`
  (blocked побеждает), `--caps` (опциональные группы инструментов), изоляция профиля,
  ограничение файлового доступа корнями workspace. Его документация прямо говорит:
  origin-списки — **удобство, а не security boundary** (не действуют на редиректы,
  обходятся намеренно).

### 1.6 Сводка

| Аспект | Claude Code | Codex | Gemini CLI | Agents SDK |
|---|---|---|---|---|
| Решения | allow/ask/deny | allow/prompt/forbidden | allow/ask_user/deny | approve/reject |
| Конфликт правил | deny > ask > allow | самое строгое | max priority по уровням | — |
| Матчинг | tool + specifier/param | prefix токенов команды | tool + args + mode | функция на инструменте |
| Режимы | 6 (`dontAsk`, `bypass`, …) | sandbox × approval | modes в правиле | — |
| Hook vs правило | hook не снимает deny/ask | — | — | guardrails отдельно |
| Кэш одобрения | per tool type (сессия/репо/домен) | правило в `.rules` | правило | sticky в RunState |
| Отказ человека | прерывает ход, ждёт пользователя | — | — | tool-результат модели |
| Sandbox | только shell | shell (OS-level) | — | — |

Общие инварианты всех систем: **deny-first**, **ошибка ≠ allow**, **правила вне
модели**, **hook не повышает права**, **sandbox — отдельный слой**.

---

## 2. Текущее состояние AutoBrowser (аудит кода)

Поток одного tool-хода — `TurnController._tool_turn`
(`src/agent_loop/execution/loop.py`):

```text
action.proposed
  → classify_tool_request(state, raw request)      # policy.py, до нормализации
       blocked      → tool-message модели, consecutive_failures+1, ход продолжается
  → ToolBroker.prepare(request)                    # normalizers + resolve tool/server
  → pre_tool_use hooks                             # deny → blocked; ask → needs_human; updated_input
  → needs_human:
       permission_request hooks (allow/deny)       # deny → terminal "blocked"
       → self._human_input(request, reason)        # False → terminal "blocked"
  → tool.started → ToolBroker.invoke → post_tool_use hooks → observe
```

Находки:

1. **Security и progress смешаны в одном `PolicyDecision`.** `classify_tool_request`
   (`policy.py:51`) делает две разные вещи: «нужен человек» по маркерам и «бессмысленный
   повтор» по журналу действий (`identical_outcome_count`). Второе — качество, не
   авторизация, но разделяет с первым тип и событие `policy.decided`.
2. **Маркеры проверяются в имени инструмента** (`BLOCKED_TOOL_MARKERS = ("payment",
   "purchase", "delete_account", "credential")`). Ни одно имя Playwright MCP
   (`browser_click`, `browser_type`, …) их не содержит — для браузера ветка
   `needs_human` из built-in политики фактически мёртвая. Риск браузерного действия —
   в **аргументах и цели** («Купить», поле пароля, домен), а не в имени.
3. **Built-in политика смотрит на запрос до нормализации**, hooks — после
   (`prepare` идёт между ними). Правило должно оцениваться по тому, что реально
   исполнится, т.е. по `PreparedToolCall.request` (и повторно — после `updated_input`).
4. **Интерактивный HITL не подключён.** `SessionRuntime` создаёт
   `native_task_runner(resources)` без `human_input` (`src/harness/session.py:697`),
   поэтому действует `_deny_human_input` (`loop.py:111`): любой `needs_human` без
   `permission_request`-hook-а → терминальный `blocked`. Фактически текущий режим — это
   Claude-овский `dontAsk`.
5. **Нет декларативных правил и режимов.** Разрешения настраиваются только через hooks:
   `url_policy` (`src/browser/hooks.py`, только `browser_navigate`), `approve_tools`
   (`permission_request`, allowlist инструментов/серверов), command hooks из
   `scripts/hooks/`. Это правильные guardrails, но нет единого места «что разрешено»,
   нет id правила в аудите, нет deny-first между источниками.
6. **Нет текущего URL в состоянии.** `BrowserState` хранит `snapshot` и last-action, но
   не URL. Доменная политика для `browser_click`/`browser_type` (не только navigate)
   требует извлечь `Page URL` из текста снапшота — это browser-specific разбор, ему место
   в `src/browser/`, не в engine.
7. **Что уже есть и переиспользуется:** `PreparedToolCall.server` (матчинг по серверу),
   `HookMatch` (fullmatch-regex по имени + сервер), события `policy.decided`,
   `approval.requested`, `hook.decided`, fail-closed семантика hooks для `pre_tool_use`,
   правило «reason не цитирует аргументы» (redaction ключевая, не по значению).

---

## 3. Что во входном research устарело или не подходит

| Тезис research | Статус для AutoBrowser |
|---|---|
| `PolicyEngine`, `Executor`, `human_input_node`, `BrowserProvider`, `FakeBrowserProvider`, `MemoryManager`, graph v1 | Удалены. Сейчас: `classify_tool_request` (`policy.py`), `ToolBroker` (`tools.py`), `HumanInputCallback` + `permission_request` hook, `ToolCallNormalizer`, `tests/mcp_fixtures/fake_server.py`, `src/harness/memory.py`. Фазы 7–8 («интегрировать в AgentLoopEngine», «убрать graph routing») уже выполнены. |
| Канонические имена `browser.click` → `browser_click` | Противоречит решению репозитория: имена сравниваются **как их выставляет MCP-мост**, без канонического маппинга (`policy.py`, docstring). Правила матчим по `server` + exposed `tool`, как Claude Code `mcp__server__tool`. |
| Stale-ref check и redundant-snapshot block в PreTool/permission | Сознательно удалены из engine (коммит `62b8199`): engine server-neutral, ref-ы валидирует MCP-сервер, снапшоты — на усмотрение модели. В PermissionEngine не возвращать. |
| Исходы `modify` / `retry` | `modify` уже есть как `updated_input` у `pre_tool_use`. `retry` — не решение авторизации. Достаточно allow/ask/deny. |
| Durable `ApprovalController` / `WAITING_FOR_APPROVAL` / expiry | Для синхронного REPL избыточно: одобрение запрашивается и исполняется в том же ходе. Нужно только при асинхронном/удалённом одобрении (см. §6.4). |
| Fingerprint с `policy_version`, `target_ref` | При синхронном одобрении действие не может измениться между approve и execute. Fingerprint нужен для **кэша** одобрений, и ключом там не может быть `ref` (эфемерен) — см. §6.4. |
| Codex modes `untrusted / on-failure / on-request / never` | Актуально: `on-request`, `never`, `granular`; `untrusted` deprecated. |
| 16 scopes (`browser.read`, `browser.external_post`, …) с классификатором | Скоупы, требующие понимания цели клика, — эвристика по тексту снапшота; это guardrail, а не граница. Стартовать с правил по `server/tool/args/domain` + MCP-аннотаций; семантику цели — отдельной фазой (§6.3). |
| `ToolSpec` с `risk_level`, `sandbox_profile` для каждого инструмента | Инструменты приходят динамически из MCP Manager. Вместо ручного реестра — аннотации сервера + правила конфигурации. |

Что из research **верно и берём**: deny > ask > allow; ошибка оценки → deny;
PermissionEngine ≠ sandbox; hook не снимает deny/ask; решение — структура с причиной и
id правила; провайдер не вызывается при deny; `browser_evaluate` — отдельный класс риска;
кэш одобрений по scope/ресурсу, а не «allow tool forever».

---

## 4. Модель угроз браузерного агента (кратко)

Главный источник опасных действий — **prompt injection со страницы** (страница
«просит» перейти, ввести, отправить) и ошибки модели на коммерческих страницах.
Опасные классы:

| Класс | Чем выражается в Playwright MCP | Чем ловить |
|---|---|---|
| Уход на чужой домен / exfiltration через URL | `browser_navigate(url)`, `browser_tabs` new + navigate, клик по ссылке, `browser_evaluate` (`location=`) | правило по домену для navigate; `browser_evaluate` → ask; origin-списки Playwright как второй слой |
| Произвольный JS | `browser_evaluate`, `browser_run_code` (если сервер его выставляет) | ask по умолчанию — обходит все остальные правила |
| Внешний side effect (купить, отправить, удалить, опубликовать) | `browser_click` по кнопке, `browser_press_key Enter` в форме, `browser_fill_form` | эвристика по цели (§6.3), ask |
| Ввод секретов | `browser_type`/`browser_fill_form` в password-поле, текст похож на карту/токен | command hook `scripts/hooks` (Luhn), правило по args; лучше — не держать секреты в профиле |
| Файлы | `browser_file_upload`, загрузки | ask; корни файлового доступа Playwright |
| Залогиненный профиль | persistent Chrome profile с сессиями | изоляция профиля — единственная настоящая граница |

Вывод: настоящие границы для браузера — **профиль/изоляция и сетевые ограничения
сервера**; PermissionEngine — обязательный, но логический слой (как Bash-правила
Claude Code: «покрывают типичную форму вызова, не являются security boundary»).

---

## 5. Предлагаемая модель

### 5.1 Место в цикле

```text
action.proposed
  → progress guard (identical outcome)        # остаётся в policy.py, не authorization
  → ToolBroker.prepare                         # нормализованный запрос + server
  → pre_tool_use hooks                         # deny / ask / updated_input (re-prepare)
  → PermissionEngine.evaluate(prepared, ctx)   # NEW: rules + mode + annotations + grants
       deny → tool-message модели (non-terminal), permission.decided
       ask  → permission_request hooks → human (+ grant) → allow | terminal blocked
       allow
  → tool.started → invoke → post_tool_use → observe
```

Почему **после** hooks: так правила видят финальные аргументы (после `updated_input`),
и hook-`allow` структурно не может снять deny/ask правила (инвариант Claude Code).
Hook-`ask` объединяется с решением движка как `max(decision)` по строгости.

### 5.2 Контракты (`src/contracts.py`, нейтральные)

```python
PermissionDecision = Literal["allow", "ask", "deny"]

@dataclass(frozen=True)
class PermissionVerdict:
    decision: PermissionDecision
    reason: str                     # видно модели; не цитирует аргументы
    rule_id: str = ""               # "" для решения режима/аннотации
    source: Literal["rule", "builtin", "mode", "annotation", "grant", "error"] = "mode"
```

Имя `PermissionRequest` занято событием hook-а `permission_request` — не переиспользовать.

### 5.3 Правило и настройки

Новый раздел `permissions` в `src/config.py` (десятый; требует обновить
`tests/test_config.py`, `.env.example`, `config.example.yaml`, CLAUDE.md «nine sections»):

```yaml
permissions:
  mode: default            # default | read_only | dont_ask | bypass
  rules:
    - id: no-evaluate
      decision: ask
      tool: "browser_evaluate|browser_run_code"
      reason: "Arbitrary page JavaScript needs approval."
    - id: shop-only
      decision: deny
      server: playwright
      tool: browser_navigate
      not_domains: [ozon.ru, wildberries.ru]
    - id: no-upload
      decision: deny
      tool: browser_file_upload
```

- Матчинг: `server` (exact), `tool` (`re.fullmatch`, как `HookMatch`), `args`
  (`{key: regex}` по строковому значению), `domains`/`not_domains` (ресурс из §5.5).
  Конфликт: **deny > ask > allow**, порядок и специфичность не важны. Без числовых
  приоритетов (Gemini) — меньше способов ошибиться.
- Слои: pydantic-settings **заменяет** списки, не мержит — личный `config.yaml` с
  `rules:` полностью перекроет env/дефолты. Поэтому неотключаемые запреты держать в коде
  (`builtin` source), а конфиг — только для пользовательских правил. Аналог Codex
  «admin только ужесточает».
- Невалидное правило (regex не компилируется, неизвестный ключ) — ошибка старта, как у
  hooks. Опционально — `match`/`not_match` примеры в правиле (идея Codex) как self-test.

### 5.4 Режимы и умолчания

| Режим | Аналог | Нет совпавшего правила | ask |
|---|---|---|---|
| `default` | Claude `default` | `readOnlyHint` → allow; `destructiveHint` → ask; иначе allow | к человеку |
| `read_only` | `plan` / Codex `read-only` | не-`readOnlyHint` → deny | к человеку |
| `dont_ask` | Claude `dontAsk` | как `default` | → deny (текущее фактическое поведение headless/batch/eval) |
| `bypass` | `bypassPermissions` | allow | → allow; **deny-правила и builtin всё равно действуют** |

`default` для браузера намеренно **разрешающий** для мутаций без аннотаций: спрашивать
на каждый клик — непригодно (Claude Code тоже не спрашивает на чтение, а «мутации»
браузера — основной режим работы). Ограничения задаются правилами. Аннотации учитываются
только для серверов из `mcp_servers` (их настраивает пользователь — доверенные).

`bypass` должен включаться только явно (CLI-флаг/конфиг), с предупреждением в выводе.

### 5.5 Ресурс (домен) — browser-side адаптер

Engine не знает про URL. Предлагается протокол рядом с `ToolCallNormalizer`:

```python
class PermissionResourceResolver(Protocol):
    def resources(self, request: ToolRequest, state: Mapping[str, Any]) -> Mapping[str, str]:
        """{"domain": "...", "target": "..."} для правил; {} если не применимо."""
```

Реализация в `src/browser/`: `domain` — из `args["url"]` для navigate, иначе из строки
`Page URL:` текущего снапшота. Если домен не удалось определить, а правило его требует —
считать **не совпавшим для allow и совпавшим для deny/ask** (fail-closed, урок Gemini
#29051).

### 5.6 Семантика цели клика (отдельная фаза)

Две независимые подсказки, обе — эвристика:

- `args["element"]` — Playwright MCP просит модель описать элемент «для получения
  разрешения». Дешёво, но пишет сама модель (ей можно солгать через injection).
- Строка снапшота с `[ref=eN]` — роль и accessible name из страницы (надёжнее модели, но
  контролируется страницей).

Правило `target: "(?i)купить|оформить|оплатить|удалить|отправить|buy|pay|delete|submit"`
→ ask. Матчить по **обоим** источникам (совпадение любого → ask). Это guardrail уровня
`url_policy`, документировать как не-границу.

---

## 6. Решения по поведению

### 6.1 Fail-closed

Исключение в `evaluate`, резолвере ресурса или разборе аргументов → `deny`
(`source="error"`), событие `permission.decided` с ошибкой. Никогда не `allow`.

### 6.2 Что видит модель при отказе

- **Правило/builtin deny** → как текущий `blocked`: tool-message с причиной, ход
  продолжается — модель может выбрать другой путь (Claude Code, Agents SDK reject).
- **Отказ человека** → оставить терминальный `blocked` (человек сказал «нет» задаче), но
  стоит добавить в промпт подтверждения вариант «отказать и продолжить» (как reject в
  Agents SDK) — это отдельный UX-вопрос (§8).

### 6.3 Аудит

Новое событие `permission.decided`: `tool`, `server`, `decision`, `source`, `rule_id`,
`mode`, `reason` — **без аргументов** (redaction в `events` ключевая; URL с токеном не
должен попасть в журнал — то же правило, что у `url_policy`). Дополнить
`approval.resolved` (`allow|deny`, `by: hook|human|grant`). `policy.decided` оставить
для progress guard.

### 6.4 Одобрение и кэш

- Одобрение синхронно и относится к уже подготовленному запросу — fingerprint для
  единичного одобрения не нужен.
- Варианты в CLI: «один раз» / «на сессию для `tool` на `domain`» / «отказать».
  Grant хранится в `SessionContext` (сессионный, как `HookEngine`), ключ —
  `(server, tool, domain)`; **никогда** не `ref` и не «весь инструмент навсегда».
- Grant не снимает deny и не действует на правила с `always_ask: true`
  (аналог `requiresUserInteraction`) — например, для `browser_evaluate`.
- Persistent grants (Claude «per repository», Codex дописывает `.rules`) — не сейчас.

---

## 7. План миграции

Каждая фаза поведенчески аддитивна и закрыта тестами.

1. **Разделить progress и authorization (без смены поведения).** Вынести identical-outcome
   блок в `progress.py`-уровень (`progress_guard`), маркеры — в будущий builtin-набор.
   Событие `policy.decided` остаётся для progress.
2. **Контракты + `PermissionEngine` (`src/harness/permissions.py`) + раздел `permissions`.**
   Чистая функция над правилами/режимом/аннотациями. `mode: dont_ask` по умолчанию в
   eval/batch и тестах — паритет с сегодняшним `_deny_human_input`. Unit-тесты на
   precedence и fail-closed. Тесты строят движок из явного `PermissionsSettings(...)`,
   не из `get_settings()` (как hooks).
3. **Встроить в `_tool_turn` после `pre_tool_use`.** Маркеры имени становятся builtin-
   правилом `ask`. Событие `permission.decided`. Обновить `docs/diagrams/agent-runtime-flow.md`.
4. **Подключить интерактивный HITL в REPL:** передать `human_input` из CLI в
   `native_task_runner` (`session.py:697`), промпт с once/session/deny и сессионные
   grants. `dont_ask` для `--task`/batch без TTY.
5. **`PermissionResourceResolver` в `src/browser/`** (`domain` из args/снапшота),
   доменные правила для всех `browser_*`. `url_policy` остаётся как hook (совместимость),
   в документации рекомендовать правила.
6. **Цель клика** (`target` из `element` + строки снапшота) — guardrail-правила для
   покупок/отправки/удаления; eval-сценарии на fake_server.
7. **Sandbox-слой отдельно:** рекомендации/дефолты для Playwright MCP
   (`--isolated` или отдельный профиль без сохранённых сессий, `--blocked-origins`),
   документированные как вторая линия, не замена правил.

---

## 8. Тестовая стратегия

Инварианты (property-style):

```text
decision == deny  ⇒ tool invoke count == 0
deny rule matches ⇒ decision != allow  (при любых allow/hook/mode/grant, включая bypass)
ask rule matches  ⇒ decision != allow без human/permission_request/grant
evaluate raises   ⇒ decision == deny
домен не определён и правило с domains ⇒ не allow
grant(server, tool, d1) не действует на d2 и на другой tool
updated_input от hook ⇒ правила переоценены на новых args
```

Сценарии (fake_server, без Chrome): navigate на запрещённый домен → deny + модель
продолжает; `browser_evaluate` в `dont_ask` → deny; клик «Купить» → ask → human deny →
`blocked`; read-only инструмент в `read_only` → allow, мутация → deny; `bypass` не снимает
builtin deny.

---

## 9. Открытые вопросы

1. Отказ человека: терминальный `blocked` (сейчас) или tool-результат «пользователь
   отказал» с продолжением? Вероятно, выбор в промпте подтверждения.
2. Нужен ли `auto`-режим (LLM-классификатор вместо человека, как Claude `auto`)? Дорого
   и недетерминированно; не раньше, чем появятся eval-ы на injection.
3. Один список `rules` или разделение `deny/ask/allow` списками (Claude Code)? Списки
   нагляднее, но pydantic-замена списков бьёт по каждому отдельно — проще рассуждать.
4. Где хранить grants между сессиями, если понадобится, — `.autobrowser/` (локально,
   git-ignored) по аналогии с Codex `default.rules`?
5. Делать ли `url_policy` тонкой обёрткой над правилами домена, чтобы не было двух
   источников доменной политики?

---

## Источники

- Claude Code — [Configure permissions](https://code.claude.com/docs/en/permissions),
  [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks)
- Codex — [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security),
  [Rules](https://developers.openai.com/codex/rules),
  [execpolicy README](https://github.com/openai/codex/blob/rust-v0.107.0/codex-rs/execpolicy/README.md)
- Gemini CLI — [Policy engine](https://geminicli.com/docs/reference/policy-engine/),
  [issue #29051](https://github.com/google-gemini/gemini-cli/issues/29051)
- OpenAI Agents SDK — [Human-in-the-loop](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md)
- Playwright MCP — [Configuration options](https://playwright.dev/mcp/configuration/options)
