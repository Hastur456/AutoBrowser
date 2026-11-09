# ADR-2026-11-09: Каскад над моделью сессии, каналы чата и пул worker'ов

Status: Accepted
Date: 2026-11-09

Дополняет: [ADR-2026-11-02: Режим стенда и лестница стоимости](2026-11-02-serve-mode-and-cost-ladder.md)
(решения о `ModelTiers`, секции `cascade`, канале Telegram и V2). Не заменяет его.

## Context

- Фаза 4 [дизайна стенда](../development/2026-11-02-serve-mode-design.md) (§14, шаги 18–20)
  добавляет каскад моделей, канал Telegram и пул worker'ов. Системный дизайн Фазы 4
  ([документ](../development/2026-11-08-serve-mode-phase4-design.md)) сверил её с кодом
  (U28–U63) и поставил вопросы P4–P11. Пользователь принял их по дефолтам 2026-11-09.
- §9.2 дизайна читается так, будто `cascade.tiers[0]` заменяет модель сессии. Но модель сессии
  уже участвует в `--model`, `/model`, в классификаторе подтверждений и в метаданных OTLP.
- У стенда нет места для вендорного кода. При этом `src/serve` не должен знать вендора, а
  вендорные адреса не могут жить в `src/config.py`.
- `ApprovalBroker` привязан к одной задаче, а `Dispatcher` Фазы 3 рассчитан на один прогон за
  раз.

## Decision

1. **Уровень 0 — модель сессии.** `cascade.tiers` — до трёх уровней **над** ней (P4). `/model`
   и `--model` меняют только уровень 0. Уровни строит `src/cli/models.py` через
   `registry.build_chat_model` и передаёт сессии как `CascadeFactory`. Engine получает их готовыми
   в `EngineResources.models`.
2. **Эскалация — чистая функция в guards**, одна точка вызова: начало каждой итерации цикла
   engine. Поводы: пустой ответ, блок progress guard, серия неудач, повторные планирования —
   всё «с последней смены уровня». Уровень только растёт. Новый уровень получает полное окно
   `loop.max_consecutive_failures` (U64). Без каскада события и вызовы байт-в-байт прежние.
3. **Каналы чата — пакет `src/channels/`.** Он импортирует только нейтральные порты
   `src.serve.{commands,notify,clock}`, `src.config` и `src.contracts`. Импортирует его только
   `src/cli/`. Адрес вендора — константа адаптера. Лексика команд — стенда, а не вендора.
   `/run` разрешён только для задания с триггером типа канала.
4. **Пул.** Каждый worker — своя сессия, брокер, профиль `w<i>` и CDP-порт. Закреплённый
   worker резервируется за своими заданиями (P5). Два прогона одного задания или одной записи
   не идут одновременно. Работающий прогон держит свой бюджет вне остатка дня. Правило Q13:
   при `workers > 1` нужен `memory.persistent_enabled=false`. Плейсхолдер `{profile_dir}` даёт
   внешнему браузеру профиль worker'а без логики по именам серверов.

## Consequences

- Каскад работает и в интерактивном режиме. Денежный лимит требует цены для каждого уровня:
  стенд без неё не стартует, интерактивная сессия предупреждает.
- Новый вендор чата — новый модуль в `src/channels/` над тем же портом, без правок `src/serve`.
- Один asyncio-цикл и синхронные записи файлов исключают гонки ledger и состояния. Ограждение
  тестом запрещает потоки в `src/serve` и `src/channels`.
- Резервы бюджета дня могут держать прогоны в очереди при больших бюджетах заданий.
- Блокировка файлов памяти L4 для пула отложена.

## Alternatives Considered

- `tiers[0]` вместо модели сессии: ломает смысл `/model` и `--model` при включённом каскаде.
- Адаптер Telegram внутри `src/serve`: вендор попал бы в слой, который по ADR о стенде нейтрален.
- Общий `ApprovalBroker` на пул: тикеты приписывались бы не тем задачам.
- Закрепление как «предпочтение»: профиль с логинами достался бы свободной задаче из чата.

## Related

- [Phase 4 system design](../development/2026-11-08-serve-mode-phase4-design.md),
  [phase 4 plan](../development/2026-11-08-serve-mode-phase4-plan.md),
  [serve mode guide](../development/serve-mode.md), [diagrams](../diagrams/serve-mode.md).
- Код: `src/cli/models.py`, `src/agent_loop/execution/guards.py` (`tier_escalation`),
  `src/serve/commands.py`, `src/channels/telegram.py`, `src/serve/pool.py`,
  `src/serve/dispatcher.py`.
- Тесты: `tests/test_model_tiers.py`, `tests/test_tier_escalation.py`,
  `tests/channels/test_channel_telegram.py`, `tests/serve/test_serve_worker_pool.py`,
  `tests/test_serve_boundaries.py`; evals `tests/evals/cascade_scenarios`, baseline
  `cascade_v1`.
