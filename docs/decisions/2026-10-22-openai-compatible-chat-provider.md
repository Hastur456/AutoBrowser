# ADR-2026-10-22: OpenAI-совместимый провайдер чат-моделей

Status: Proposed
Date: 2026-10-22
Extends: [ADR-2026-09-03: Drop the LangChain/LangGraph/LangSmith Dependency Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md)
— в части провайдерских адаптеров

## Context

Движок работает через нейтральный контракт `ChatModel` (`src/llm.py`), но единственный
адаптер — `OllamaChatModel` (протокол Ollama `/api/chat`), и `src/cli/bootstrap.py` выбирает
его жёстко. Поэтому нельзя использовать OpenAI, агрегаторы (OhMyLama, OpenRouter, Together,
Groq), вендорские OpenAI-совместимые эндпоинты и локальные серверы вроде LM Studio и vLLM. Все
они говорят на одном протоколе — Chat Completions.

Сопутствующие пробелы:
- у вызова модели нет таймаута;
- `reasoning_effort` и `max_output_tokens` записаны в конфиг, но не отправляются;
- `temperature` отправляется всегда, хотя reasoning-модели её отклоняют;
- расход токенов выбрасывается;
- ошибки провайдера уходят в `goal.failed` сырыми исключениями клиента.

Ограничения проекта: в коде нет имён вендоров и логики по именам
([ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)); все
настройки живут в `src/config.py`
([ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md)); движок не
знает о провайдере. Термины — в [глоссарии](../glossary.md).

Подробный дизайн:
[docs/development/2026-10-22-openai-compat-provider-design.md](../development/2026-10-22-openai-compat-provider-design.md).

## Decision

1. **Один адаптер на протокол.** Новый `OpenAICompatChatModel` в
   `src/providers/openai_compat.py` реализует `ChatModel` поверх Chat Completions
   (`POST {host}/chat/completions`) для любого совместимого сервиса. Вендор определяется только
   конфигом (`host`, `api_key`, `model`, `extra_headers`, `extra_body`); вендорских имён, URL и
   пресетов в `src/` нет, профили сервисов — закомментированные примеры в
   `config.example.yaml`.
2. **Выбор провайдера.** `llm.provider: ollama | openai_compat` (по умолчанию `ollama`).
   `src/providers/registry.build_chat_model(model, temperature, *, provider=None)` выбирает
   адаптер и становится фабрикой по умолчанию в `bootstrap`. `--provider` привязывается к ней
   там же, поэтому `SessionContext` и классификатор подтверждений получают модели одного
   провайдера без изменений harness. Импортировать `src.providers` может только `src/cli/`.
3. **Транспорт — официальный SDK `openai`** (`AsyncOpenAI`, ленивый импорт, фиксированная
   версия): повторы с backoff, таймауты, типизированные ошибки. `host` для `openai_compat`
   обязателен. Ключ передаётся только из `llm.api_key`; без ключа заголовок `Authorization`
   не отправляется. Переменные `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`,
   `OPENAI_PROJECT_ID` не влияют на запрос.
4. **Отображение протокола.** Аргументы вызовов — JSON-строка, у вызова есть `id` и
   `type: "function"`. Ответ инструмента сопоставляется по `tool_call_id`; `id` провайдера
   сохраняется и генерируется, только если он пуст или повторяется. Пустой текст рядом с
   вызовами — `null`. Пустые `choices` — ошибка. Невалидные аргументы → `{}`, как в Ollama.
   Поля рассуждений и вендорские поля ответа игнорируются.
5. **Параметры.** `temperature: null` — «не отправлять». `max_output_tokens` отправляется под
   именем из `llm.max_tokens_param` (`max_completion_tokens` | `max_tokens`).
   `reasoning_effort` отправляется. `parallel_tool_calls` — только если задан и в запросе есть
   инструменты: движок исполняет один вызов за ход. `max_reasoning_tokens` остаётся
   декларативным, вендорские эквиваленты — через `extra_body`, где зарезервированные ключи
   запрещены.
6. **Новые поля `llm.*`.** Раздел остаётся плоским: `provider`, `timeout_seconds` (общий,
   включая Ollama, по умолчанию 60 с), `max_retries` (по умолчанию **0**: вызов модели
   укладывается в бюджет `loop.progress_timeout_seconds`), `max_tokens_param`,
   `parallel_tool_calls`, `extra_headers`, `extra_body`, `error_preview_chars`;
   `temperature` допускает `null`.
7. **Нейтральные ошибки и учёт токенов.** `src/llm.py` получает `ModelProviderError`
   (`provider`, `status_code`, `retryable`, короткий текст без секретов) и
   `ModelUsage`/`ModelResponse.usage` — в форме дизайна тестового пайплайна плюс
   `reasoning_tokens`. Оба адаптера заполняют их. `usage` попадает в `model.responded`.
   Повторы — только внутри SDK, движок не повторяет.
8. **Ollama.** Адаптер получает таймаут, `usage`, `ModelProviderError`,
   `max_output_tokens → num_predict` и необязательную `temperature`. Его протокол и
   поведение по умолчанию не меняются.
9. **`doctor`.** Строка `llm` показывает провайдер, `host`, модель, «ключ задан» и бюджет
   таймаута без сети. `doctor --llm` проверяет провайдер по сети (`GET {host}/models` или
   `/api/tags` у Ollama) через `probe_chat_provider` реестра — с тем же клиентом, таймаутом
   `ui.doctor_probe_timeout_seconds` и без повторов. Без флага `doctor` в сеть не ходит.
10. **Объём ветки.** Только `openai_compat`. Нативные адаптеры Anthropic и Gemini — будущая
    отдельная ветка с отдельным ADR; реестр и плоский раздел `llm.*` — их точки расширения.

## Consequences

Плюсы:
- любой OpenAI-совместимый сервис, включая OhMyLama, подключается одним конфигом;
- декларативные поля `llm.*` становятся честными;
- зависший вызов модели ограничен таймаутом, а не сторожем цели;
- ошибки провайдера читаемы в TUI и событиях;
- расход токенов виден в `model.responded` и готов для ветки тестового пайплайна.

Компромиссы и риски:
- новая зависимость `openai`, поведение которой по умолчанию (окружение, заголовки) нужно
  закрепить тестами исходящего запроса;
- у Ollama появляется таймаут 60 с: медленная локальная модель может не уложиться, тогда
  нужен `timeout_seconds: null`;
- умолчание `temperature: 0.0` даёт 400 у reasoning-моделей, пользователь ставит `null` сам —
  молчаливого обхода нет;
- агрегаторы, проксирующие Claude или Gemini, могут неточно передавать вызовы инструментов.
  Это проверяется живым прогоном до перевода ADR в Accepted;
- лишние параллельные вызовы теряются, пока движок исполняет один вызов за ход;
- без повторов по умолчанию кратковременные 429/503 завершают цель как `failed`; повторы
  включаются через `max_retries`, а `doctor` предупреждает, если они выводят вызов за бюджет
  сторожа;
- данные страниц уходят внешнему провайдеру — это отмечается в документации.

Последующая работа:
- суммирование `usage` и сравнение стоимости — ветка тестового пайплайна
  ([ADR-2026-10-19](2026-10-19-autonomous-test-pipeline.md));
- нативные адаптеры Anthropic и Gemini — запланированы, отдельной веткой и ADR;
- стриминг, Responses API, несколько именованных провайдеров — по необходимости.

## Alternatives Considered

- **Собственный клиент на `httpx`** (уже в зависимостях). Нет новой зависимости, но повторы,
  `Retry-After`, разбор ошибок и сериализацию пришлось бы писать и поддерживать самим. Не
  выбран: адаптер должен оставаться тонким.
- **LiteLLM или подобная мультипровайдерная библиотека.** Покрывает сотни вендоров, но
  возвращает тяжёлую зависимость и чужие абстракции, от которых проект отказался в
  ADR-2026-09-03. Нужен один протокол.
- **Адаптер на каждого вендора** (OpenAI, OpenRouter, OhMyLama…). Дублирует один протокол и
  вносит вендорские имена в код. Не выбран.
- **Использовать OpenAI-совместимый `/v1` Ollama вместо нативного адаптера.** Сохранили бы
  один адаптер, но потеряли бы нативные поля Ollama (`load_duration`, `num_ctx`) и сломали
  бы рабочие конфиги. Нативный адаптер остаётся умолчанием.
- **Responses API.** Агрегаторы его в основном не поддерживают. Отложен.
- **Вложенные подразделы `llm.ollama.*` / `llm.openai_compat.*`.** Почти все поля общие,
  имена переменных окружения стали бы длиннее. Выбран плоский раздел с описанием, какой
  провайдер читает поле.
- **Повторы по умолчанию (`max_retries: 1`) или больший `loop.progress_timeout_seconds`.**
  Повтор сглаживает 429/503, но при 60-секундном таймауте худший случай упирается в
  120 с сторожа цели, а увеличение бюджета сторожа замедлит обнаружение реальных зависаний.
  Выбрано: без повторов по умолчанию, с возможностью включить.
- **Сетевая проверка в `doctor` по умолчанию.** Каждый запуск `doctor` тратил бы запрос и
  отправлял ключ. Выбрано: по флагу `--llm`.
- **Читать `OPENAI_API_KEY` и `OPENAI_BASE_URL` из окружения.** Удобно, но ключ или
  организация другого проекта могли бы уйти стороннему агрегатору. Ключи — только в
  конфигурации AutoBrowser, как уже сделано для Ollama.

## Related

- [Проектный документ](../development/2026-10-22-openai-compat-provider-design.md)
- [Glossary](../glossary.md)
- [ADR-2026-09-03: Drop the LangChain/LangGraph/LangSmith Dependency Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md)
- [ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md)
- [ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)
- [ADR-2026-10-19: Автономный пайплайн тестирования](2026-10-19-autonomous-test-pipeline.md) — `ModelUsage`
- Код: `src/llm.py`, `src/providers/`, `src/config.py`, `src/cli/bootstrap.py`,
  `src/cli/parser.py`, `src/cli/subcommands.py`, `src/agent_loop/model.py`,
  `src/agent_loop/execution/loop.py`
