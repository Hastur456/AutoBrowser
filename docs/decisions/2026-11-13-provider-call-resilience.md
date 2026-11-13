# ADR: Устойчивый вызов провайдера модели

Status: Proposed
Date: 2026-11-13

## Context

Задачи завершаются `goal.failed` из-за одного неудачного вызова модели:
`openai_compat: timed out after 60 s` и `openai_compat: connection failed (APIConnectionError <-
ConnectError)`. MCP и браузер при этом исправны.

ADR-2026-10-22 ввёл нейтральную `ModelProviderError` (`provider`, `status_code`, `retryable`),
один таймаут `llm.timeout_seconds` на все фазы HTTP и повторы только внутри SDK, выключенные по
умолчанию (`max_retries: 0`), чтобы вызов укладывался в сторож цели
`loop.progress_timeout_seconds`. Отсюда четыре проблемы:

- недоступный хост ждётся 60 с;
- read-таймаут без стриминга равен всему времени генерации;
- кратковременный сбой не повторяется;
- включённые повторы SDK заново выполняют неоднозначные запросы (read timeout, 5xx), что может
  удвоить генерацию, и невидимы для сторожа цели.

Кроме того, по логам нельзя отличить DNS от отказа в соединении и read от connect.

Подробный разбор, таблицы и тесты — в
[дизайне](../development/2026-11-13-provider-error-handling-design.md).

## Decision

1. **Классификация у источника.** `ModelProviderError` получает аддитивные keyword-only поля:
   `kind`, `phase`, `cause`, `retry_after_seconds`, `request_id`, `endpoint` (без userinfo и
   query), `model`, `attempts`, `elapsed_ms`.
   - Безопасность повтора — производное свойство `retry_safety`: `safe` | `ambiguous` | `never`.
   - `retryable` сохраняется и равен `retry_safety != "never"`.
   - Причина выводится из типов и `errno` цепочки исключений, не из текста.
   - Оба адаптера классифицируют по общим хелперам `src/providers/_wire.py`.
2. **Фазовые таймауты.** Новые поля `llm.connect_timeout_seconds` (10 с),
   `write_timeout_seconds` (30 с) и `pool_timeout_seconds` (10 с). `None` в любом из них
   означает «как `timeout_seconds`». Сам `timeout_seconds` (60 с) становится read-таймаутом.
   Поля собираются в `httpx.Timeout` для обоих адаптеров.
3. **Один слой повторов вне движка.** `ResilientChatModel` в `src/providers/resilience.py`
   оборачивает каждую модель в `registry.build_chat_model`; повторы SDK выключаются.
   - По умолчанию до 3 попыток на вызов (`llm.retry_max_attempts`).
   - Повторяется только `safe`: connect, таймауты connect/write/pool, 429, 503/529/408/409.
   - `ambiguous` (read timeout, обрыв ответа, 500/502/504) повторяется только при
     `llm.retry_ambiguous`.
   - Пауза — full jitter с потолком, `Retry-After` уважается до `llm.retry_after_max_seconds`.
   - Все попытки ограничены сроком `llm.call_deadline_ratio × loop.progress_timeout_seconds`
     (< 1). Поэтому ошибка провайдера всегда опережает сторож цели.
   - `CancelledError` не повторяется никогда.
4. **Диагностика в существующих событиях.**
   - `goal.failed.error` несёт безопасные поля ошибки (`redact_json_safe`).
   - `model.responded` получает аддитивный ключ `call` (попытки, `elapsed_ms`, `request_id`).
   - `TaskUsage` получает счётчик `provider_attempts`, `TaskOutcome` — поле
     `provider_error_kind`.
   - Новых типов событий нет. Повтор пишется в лог WARNING без ключей, заголовков и промпта.
5. **Совместимость.**
   - `llm.max_retries > 0` читается как `retry_max_attempts = max_retries + 1` с
     `retry_ambiguous = true` и `FutureWarning`.
   - Заданные одновременно `max_retries` и `retry_max_attempts` — ошибка конфигурации.
   - Терминальный путь не меняется: исчерпанный вызов → `goal.failed` → `provider_error`
     (INFRA).

## Consequences

Плюсы:
- кратковременные сбои сети и 429/503 больше не валят задачу и не тратят токенов;
- недоступный хост виден за `connect_timeout`, а не за 60 с;
- сторож цели больше не маскирует ошибку провайдера;
- по `goal.failed` видно вид, фазу, причину, статус, request id и попытки;
- движок, `GoalRunner`, MCP и permissions не меняются.

Компромиссы и риски:
- десять новых полей `llm.*`;
- `read timeout` остаётся неоднозначным, пока нет стриминга;
- цена неоднозначной попытки неизвестна, поэтому `retry_ambiguous` выключен по умолчанию;
- `cause` на экзотических транспортах будет `unknown`.

Последующая работа:
- переключение тира каскада при исчерпанном вызове (`cascade.escalate_on_provider_error`);
- circuit breaker на сессию;
- SSE-стриминг — отдельными решениями.

## Alternatives Considered

- **Поднять `timeout_seconds` до 180 с.** Недоступный хост ждётся 3 минуты, сторож цели
  срабатывает раньше. Не выбран.
- **Включить `max_retries` SDK по умолчанию.** Повторяет и неоднозначные запросы, невидим для
  событий и сторожа, срока на все попытки нет. Не выбран.
- **Повторы в движке (`ModelDriver`/`_model_step`).** Нарушает границу: движок владеет
  reasoning/routing/execution/observation, а не инфраструктурой. ADR-2026-10-22 прямо говорит,
  что движок не повторяет. Не выбран.
- **Обёртка в `src/harness/` (`SessionContext`).** Не покрыла бы тиры каскада и модель
  классификатора, которые собирает фабрика CLI. Не выбран (Q4 дизайна).
- **Классификация по тексту ошибки.** Текст может цитировать заголовки и тело, а формулировки
  вендорские. Не выбран.

## Related

- [Дизайн: обработка ошибок провайдера модели](../development/2026-11-13-provider-error-handling-design.md)
- [ADR-2026-10-22 OpenAI-совместимый провайдер](2026-10-22-openai-compatible-chat-provider.md) — дополняется (повторы, таймауты, ошибки)
- [ADR-2026-11-09 Каскад, каналы, пул воркеров](2026-11-09-cascade-channels-and-worker-pool.md)
- Код: `src/llm.py`, `src/providers/_wire.py`, `src/providers/openai_compat.py`,
  `src/providers/ollama.py`, `src/providers/registry.py`, `src/agent_loop/events.py`,
  `src/agent_loop/failures.py`, `src/agent_loop/goals.py`
