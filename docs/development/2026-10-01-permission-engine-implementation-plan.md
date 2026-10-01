# PermissionEngine — план реализации

Дата: 2026-10-01 · Ветка: `feat/permission-engine` · Статус: **Proposed**

Основа: [PermissionEngine Research](../research/2026-10-01-permission-engine-research.md)
(далее — «research»). Продолжает «Phase 5: ToolBroker And PermissionEngine» из
[Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md)
и опирается на [ADR lifecycle hooks](../decisions/2026-09-30-lifecycle-hooks-engine.md).

## 1. Цель

Заменить `src/agent_loop/execution/policy.py` (`classify_tool_request` + `policy_updates`)
детерминированным `PermissionEngine`:

```text
(подготовленный запрос, ресурсы, правила, режим, grants) → allow | ask | deny
                                                          + reason + rule_id + source
```

После завершения плана `policy.py` удалён, а его обязанности разнесены так, чтобы
**авторизация** (PermissionEngine) и **качество/прогресс** (identical-outcome guard) больше
не делили один тип решения.

Не цели: sandbox (это Playwright MCP / профиль браузера), `auto`-режим с
LLM-классификатором, persistent grants между сессиями, асинхронное/удалённое одобрение.

## 2. Что происходит с содержимым `policy.py`

| Сейчас (`policy.py`) | Куда | Коммит |
|---|---|---|
| `BROWSER_TOOL_PREFIX`, `SNAPSHOT_TOOL`, `TABS_TOOL`, `is_browser_tool` | `src/browser/names.py` (там уже живут `is_browser_tool_name` / `is_browser_snapshot_name`; дубли схлопнуть). Импорты в `guards.py`, `observation.py` | 1 |
| identical-outcome блок в `classify_tool_request` | `progress.py`: чистая `ineffective_repeat_reason(history, name, args, limit) -> str \| None`; вызов в `_tool_turn` как **progress guard** | 1 |
| `policy_updates(state, "blocked", reason)` | `guards.py`: `tool_block_updates(state, reason, *, event)` — общий путь для progress-блока, hook-deny и permission-deny | 1 |
| `policy_updates(state, "needs_human", …)` | исчезает: ask-ветка строится из `PermissionVerdict` | 3 |
| «нет tool request» → `blocked` | остаётся в progress guard (это валидация, не авторизация) | 1 |
| `BLOCKED_TOOL_MARKERS` (по имени → `needs_human`) | builtin-правило `ask` в `src/harness/permissions.py` (`tool: "(?i).*(payment\|purchase\|delete_account\|credential).*"`) — паритет | 3 |
| реэкспорты в `execution/__init__.py` | удалить `BLOCKED_TOOL_MARKERS`, `classify_tool_request`; добавить ничего (движок живёт в `src/harness/`) | 3 |

`LoopState.policy_decision` / `policy_event` и контракт `PolicyDecision`
(`approved | needs_human | blocked`) **сохраняются** как итог «шлюза» хода: на них завязаны
сброс task-local полей (`session.py`, `guards.py`, `observation.py`), `cli/output.py` и
`state.py`. Переименование — отдельная косметика вне этого плана. `policy_event`
дополняется полями `source` и `rule_id`.

## 3. Целевая форма tool-turn-а

```text
action.proposed
progress guard (raw request)           нет запроса / identical outcome ≥ limit
  blocked ─────────────────────────────► tool_block_updates → policy.decided → return
ToolBroker.prepare(request, state)     normalizers + resolve tool/server
  unknown tool → permission и hooks пропускаются, error result → observe (как сейчас)
pre_tool_use hooks                     deny → blocked (как сейчас); updated_input → re-prepare
PermissionEngine.evaluate(check)       check строится из ФИНАЛЬНОГО prepared (+ hook ask)
  emit permission.decided
  deny  ───────────────────────────────► tool_block_updates → return  (НЕ терминально)
  ask   → approval.requested
          permission_request hooks     deny → terminal blocked; allow → дальше
          human_input                  once | session(+grant) | deny → terminal blocked
          emit approval.resolved {decision, by: hook|human|grant}
  allow
tool.started → invoke → post_tool_use → observe
```

Почему так:

- **Progress guard до `prepare`** — он не авторизация, ему достаточно сырого запроса, и
  дешёвый отказ не платит за normalizers. Событие `policy.decided` остаётся за ним
  (метрики `_is_policy_block`, replay не ломаются).
- **Permission после hooks** — правила видят то, что реально исполнится (после
  `updated_input`), а hook-`allow` структурно не может снять deny/ask правила.
- **Hook-`ask`** не ветвит loop отдельно: передаётся в `evaluate` как `hook_ask_reason`
  и проходит ту же логику режимов (в `dont_ask` станет deny, в `bypass` — allow).
- **Permission-deny не терминален** (research §6.2): модель получает tool-message с
  причиной и может выбрать другой путь. Отказ человека/`permission_request` остаётся
  терминальным `blocked` (паритет; см. §10, вопрос 1).

## 4. Контракты (`src/contracts.py`, нейтральные)

```python
PermissionDecision = Literal["allow", "ask", "deny"]
PermissionMode = Literal["default", "read_only", "dont_ask", "bypass"]
PermissionSource = Literal["rule", "builtin", "hook", "mode", "annotation", "grant", "error"]
ApprovalAnswer = Literal["once", "session", "deny"]

@dataclass(frozen=True)
class PermissionCheck:
    """Что оценивается: финальный нормализованный вызов. Только скаляры/dict — как HookEvent."""
    tool: str
    server: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False          # tool_is_read_only(prepared.tool)
    destructive: bool = False        # явный destructiveHint is True (не spec-default)
    hook_ask_reason: str = ""        # pre_tool_use ask → эскалация

@dataclass(frozen=True)
class PermissionVerdict:
    decision: PermissionDecision
    reason: str                      # видно модели; НЕ цитирует аргументы
    source: PermissionSource = "mode"
    rule_id: str = ""
    always_ask: bool = False         # grant не может покрыть (для промпта CLI)
    grant_key: tuple[str, str, str] | None = None   # (server, tool, domain) для «на сессию»

class PermissionResourceResolver(Protocol):
    def resources(self, check: PermissionCheck, state: Mapping[str, Any]) -> Mapping[str, str]:
        """{"domain": ..., "target": ...}; {} если неприменимо. Исключение → deny."""
```

Имя `PermissionRequest` не использовать — занято hook-событием `permission_request`.

## 5. Настройки (`src/config.py`, раздел `permissions` — десятый)

```python
class PermissionRule(_Section):
    id: str                                   # обязателен, уникален
    decision: PermissionDecision
    tool: str = ".*"                          # re.fullmatch, как HookMatch
    server: str = ""                          # exact; "" = любой
    args: dict[str, str] = {}                 # {key: regex} по str(value), re.search
    domains: list[str] = []                   # суффиксное совпадение: ozon.ru ⊇ www.ozon.ru
    not_domains: list[str] = []
    target: str = ""                          # regex по "target" ресурса (фаза 6)
    always_ask: bool = False                  # только для decision=ask; grant не покрывает
    reason: str = ""                          # иначе генерируется "Denied by rule <id>"
    # валидаторы: regex компилируются; id уникальны (на уровне PermissionsSettings);
    # always_ask только при ask; domains и not_domains не одновременно

class PermissionsSettings(_Section):
    mode: PermissionMode = "default"
    rules: list[PermissionRule] = []
```

- Один список `rules` (research §9.3): pydantic-settings **заменяет** списки целиком,
  поэтому неотключаемые правила — в коде (`builtin`), конфиг — только пользовательские.
- Env: `AUTOBROWSER_PERMISSIONS__MODE`; `rules` — через YAML (JSON в env допустим, но не
  документируем как основной путь).
- Обновить: `tests/test_config.py`, `.env.example`, `config.example.yaml` (закомментированный
  пример из research §5.3), CLAUDE.md («nine sections» → ten).

## 6. `PermissionEngine` (`src/harness/permissions.py`)

Session-scoped, как `HookEngine`: строится в `SessionContext.initialize`
(`PermissionEngine.from_settings(settings.permissions, builtin=..., resolver=...)`) **до**
запуска Chrome/MCP — битое правило валит старт. Держит сессионные grants. В loop попадает
через `EngineResources.permissions`.

```python
class PermissionEngine:
    @classmethod
    def from_settings(cls, settings, *, builtin=BUILTIN_RULES, extra_builtin=(), resolver=None): ...
    def evaluate(self, check: PermissionCheck, state: Mapping[str, Any]) -> PermissionVerdict: ...
    def grant(self, key: tuple[str, str, str]) -> None: ...
    @property
    def mode(self) -> PermissionMode: ...
```

Дефолт `EngineResources.permissions` — `PermissionEngine(mode="default", builtin=BUILTIN_RULES)`
без пользовательских правил: тесты/evals без явного движка ведут себя как сегодня (маркеры →
ask → инжектированный `human_input`, иначе `_deny_human_input`).

### Алгоритм `evaluate` (порядок и специфичность правил не важны)

```text
try:
  resources = resolver.resources(check, state) if resolver else {}
  matched   = [r for r in builtin + config_rules if r.matches(check, resources)]

  1. any deny  in matched → deny  (source rule|builtin, rule_id)       # bypass/grant/hook бессильны
  2. mode == read_only and not check.read_only and no allow rule → deny (source mode)
  3. asks = ask-правила из matched (+ check.hook_ask_reason как source hook)
     if asks: return resolve_ask(asks)
  4. any allow in matched → allow (rule)
  5. дефолт режима:
       bypass                         → allow (mode)
       default | dont_ask:
         check.destructive           → resolve_ask([annotation])
         иначе                        → allow (mode / annotation при read_only)
       read_only (read_only tool)     → allow (annotation)
except Exception → deny (source error, reason без текста исключения с аргументами)

resolve_ask(asks):
  always = any(a.always_ask) or any(a.source == hook)
  grant  = (server, tool, resources.get("domain", "")) in grants
  if grant and not always    → allow (grant)
  if mode == bypass and not always → allow (mode)
  if mode == dont_ask        → deny (mode)           # не терминально — модель продолжает
  else                       → ask (первое ask-правило: reason, rule_id, always_ask, grant_key)
```

Fail-closed по ресурсам (урок Gemini #29051): если правило требует `domains`/`not_domains`/
`target`, а ресурс не определён — правило **не совпадает** для `allow` и **совпадает** для
`deny`/`ask`.

`tool_is_destructive(tool)` добавить рядом с `tool_is_read_only` в `src/harness/tools.py`:
только явное `destructiveHint is True` (MCP-default `true` при отсутствии аннотации не
учитываем — иначе ask на каждый вызов неаннотированного сервера).

## 7. Browser-side (`src/browser/permissions.py`)

Engine и `src/harness/permissions.py` server-neutral; всё про URL/Playwright — здесь.
`SessionContext` передаёт это в `from_settings` (как `BrowserToolNormalizer` через
`mcp_setup.py`).

- `BrowserResourceResolver`:
  - `domain`: `args["url"]` для `browser_navigate` (и `browser_tabs` с url, если есть);
    иначе — строка `Page URL:` из текущего снапшота (`state["browser"]["snapshot"]`).
    Нормализация: lowercase host, без порта/`www.`; IDN → punycode.
  - `target` (коммит 6): `args["element"]` + строка снапшота с `[ref=<args["ref"]>]`
    (роль + accessible name), склеенные через `\n` — правило матчит любой источник.
- `BROWSER_BUILTIN_RULES` (поведенческое изменение, выносится отдельным коммитом 4):
  - `browser-evaluate`: `ask`, `always_ask`, `tool: "browser_evaluate|browser_run_code"`.
  - `browser-file-upload`: `ask`, `tool: "browser_file_upload"`.
  - Матч по `tool` без `server`, т.к. browser-сервер выставляется unprefixed — проверить на
    реальном списке инструментов.

## 8. Интеграция в loop (`src/agent_loop/execution/loop.py`)

- `TurnController` получает `resources.permissions`; `_tool_turn` перестраивается по §3.
- `HookEvent.reason` для `pre_tool_use` больше не несёт built-in решения (оно теперь после
  hooks) — передаём `""`; для `permission_request` — `verdict.reason`. Отметить в
  `docs/development/lifecycle-hooks.md`.
- `HumanInputCallback` → `Callable[[ToolRequest, str, PermissionVerdict], Awaitable[ApprovalAnswer | bool]]`;
  `bool` принимается для совместимости (`True → once`, `False → deny`). Адаптер — одна
  функция в loop.py, старые тестовые колбэки `(request, reason)` оборачиваются через
  `inspect.signature` или обновляются (предпочтительно обновить — их мало).
- `session` → `permissions.grant(verdict.grant_key)` до `tool.started`. Если
  `grant_key is None` или `always_ask` — `session` трактуется как `once`.
- События (`src/agent_loop/events.py`): добавить `permission.decided`
  (`tool`, `server`, `decision`, `source`, `rule_id`, `mode`, `reason` — **без args**) и
  `approval.resolved` (`decision`, `by: hook|human|grant`, `scope: once|session`).
  Проекция/redaction в `events.py:280`, учёт в `metrics.py` (permission-deny считается
  блоком, как `hook.decided` deny), отображение в `replay.py` и `cli/output.py`.
- `approval.requested` сохраняет форму payload (`tool_request`, `reason`) + `rule_id`.

## 9. План коммитов

Каждый коммит зелёный (`python -m pytest`), поведение меняется только там, где сказано.

**0. ADR + инвентаризация аннотаций.** `docs/decisions/2026-10-01-permission-engine.md`
(Proposed). Снять реальные `annotations` всех инструментов Playwright MCP
(и `tests/mcp_fixtures/fake_server.py`) — от этого зависит, сработает ли
`destructiveHint → ask` и на каких именно инструментах. Результат — таблица в ADR. Если
fake_server не выставляет аннотаций — добавить их, чтобы тесты были репрезентативны.

**1. Разделить progress и authorization (без смены поведения).**
- Имена браузера → `src/browser/names.py`; `ineffective_repeat_reason` → `progress.py`;
  `tool_block_updates` → `guards.py`.
- `classify_tool_request` в `policy.py` оставляет только маркеры (временно).
- Тесты: существующие проходят без изменений; unit на `ineffective_repeat_reason`.

**2. Контракты + настройки + `PermissionEngine` (не подключён).**
- `src/contracts.py`, `src/config.py` (`PermissionRule`, `PermissionsSettings`),
  `src/harness/permissions.py`, `tool_is_destructive`.
- `tests/test_permissions.py`: precedence (deny > ask > allow при любом порядке),
  режимы × (rule/annotation/none), grants (ключ, always_ask, hook-ask), fail-closed
  (исключение резолвера/матчера → deny; неизвестный домен), валидация конфигурации
  (плохой regex, дубли id, `always_ask` у deny). Движок строится из явного
  `PermissionsSettings(...)`, **не** из `get_settings()`.
- `tests/test_config.py`, `.env.example`, `config.example.yaml`.

**3. Встроить в `_tool_turn`, удалить `policy.py`.**
- Порядок §3, `EngineResources.permissions`, `SessionContext.permissions`
  (`from_settings` до MCP), события §8, маркеры → `BUILTIN_RULES`.
- Удалить `src/agent_loop/execution/policy.py` и реэкспорты.
- Тесты: `tests/test_agent_loop_hooks.py` (порядок событий: `permission.decided` между
  `hook.decided` и `tool.started`; `policy.decided` только от progress guard), метрики,
  replay, `tests/test_harness_session.py`. Новые: deny-правило → invoke не вызван, модель
  получает tool-message, ход продолжается; hook-ask + `dont_ask` → deny; `updated_input`
  → правило переоценено на новых args.
- Обновить `docs/diagrams/agent-runtime-flow.md`, `harness-boundaries.md`.

**4. Browser resolver + browser builtin-правила.**
- `src/browser/permissions.py`, подключение в `SessionContext`.
- Поведенческое изменение: `browser_evaluate` начинает спрашивать (в headless — deny с
  продолжением). Прогнать `scripts/run_evals.py --baseline …` и сравнить.
- Тесты: домен из args / из `Page URL:` снапшота; `not_domains` для `browser_click` на
  чужом домене; снапшот без URL + правило с `domains` → не allow.

**5. Интерактивный HITL в REPL.**
- `SessionRuntime` принимает `human_input` и передаёт в `native_task_runner`
  (`session.py:697`). CLI (`src/cli/agent_cli.py`) реализует промпт
  `[y] once / [s] session for <tool> on <domain> / [n] deny` через
  `asyncio.to_thread(input)`; `s` не показывается при `always_ask`.
- `--permission-mode` в `src/cli/parser.py` (перекрывает `permissions.mode`);
  `bypass` печатает предупреждение. Без TTY (`--task` в pipe, `run_batch.py`) — принудительно
  `dont_ask`; `run_evals.py` — `dont_ask` явно.
- Тесты: CLI-колбэк на подменённом stdin; grant на сессию не действует на другой домен /
  инструмент; следующая задача той же сессии видит grant.

**6. Цель клика (guardrail).**
- `target` в резолвере + пример правила в `config.example.yaml`
  (`(?i)купить|оформить|оплатить|удалить|отправить|buy|pay|delete|submit` → ask).
  Не builtin — эвристика с ложными срабатываниями.
- Eval-сценарий на fake_server: клик «Купить» → ask → deny → `blocked`.

**7. Документация и sandbox-рекомендации.**
- `docs/development/permissions.md` (как писать правила, режимы, отладка по
  `permission.decided`), раздел в CLAUDE.md, глоссарий, ADR → Accepted.
- Рекомендации для Playwright MCP (`--isolated`/отдельный профиль, `--blocked-origins`) как
  второй слой, не замена правил.
- `url_policy` hook остаётся; в доке рекомендовать доменные правила.

## 10. Тестовые инварианты (сквозные)

```text
decision == deny                       ⇒ invoke count == 0
deny-правило совпало                   ⇒ decision != allow (любые allow/hook/mode/grant, вкл. bypass)
ask-правило совпало                    ⇒ allow только через human | permission_request | grant | bypass
always_ask / hook-ask                  ⇒ grant и bypass не дают allow
evaluate бросил                        ⇒ deny, source == "error"
домен не определён + правило с domains ⇒ не allow
grant(server, tool, d1)                ⇒ не действует на d2 и на другой tool
updated_input от hook                  ⇒ правила оценены на новых args
permission.decided / approval.*        ⇒ payload без args
```

## 11. Решения, принятые в плане (закрывают research §9)

1. **Отказ человека** — терминальный `blocked` (паритет). Вариант «отказать и продолжить»
   можно добавить в промпт CLI позже как четвёртый ответ — **нужно подтверждение**.
2. **`auto`-режим** — нет.
3. **Один список `rules`**, builtin-правила в коде.
4. **Persistent grants** — нет; grants живут в `PermissionEngine` до конца сессии.
5. **`url_policy`** — не переписываем на правила в этом плане; два источника доменной
   политики допустимы до отдельного решения.
6. **Permission-deny и `dont_ask`-deny не терминальны** — это меняет поведение для
   маркер-инструментов (сейчас: терминальный `blocked`), но такие имена у Playwright MCP не
   встречаются. **Нужно подтверждение.**
7. **`PolicyDecision`/`policy_*` поля state не переименовываются.**

## 12. Риски

- Порядок событий меняется (permission после hooks) — тесты на точную
  последовательность событий в `test_agent_loop_hooks.py` придётся обновить осознанно.
- `destructiveHint` у Playwright MCP может оказаться у неожиданных инструментов
  (например, `browser_close`) → лишние ask; решается в коммите 0 по фактическим данным.
- `Page URL:` в снапшоте — формат Playwright MCP; при смене формата резолвер вернёт `{}`,
  и fail-closed сделает доменные deny/ask-правила срабатывающими на всё. Покрыть тестом на
  реальном образце снапшота.
