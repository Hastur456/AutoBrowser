# ADR-2026-10-19: Langfuse не вводим; нейтральный OTLP-экспорт как отложенная опция

Status: Proposed
Date: 2026-10-19

## Context

Пользователь просит автономный пайплайн регрессий и бенчмарков и интеграцию с Langfuse,
«если она нужна». Если не нужна — обосновать.

**Что уже есть в репозитории:**
- `events.jsonl` — полная типизированная трасса задачи. `redact_json_safe` редактирует
  ключи с `token/secret/password/credential/api_key/authorization` и усекает строки.
- `agent_trace.jsonl` — компактная проекция.
- `scripts/replay_trace.py`, `src/agent_loop/replay.py` — `summarize_trace`,
  `print_action_sequence`.
- `src/agent_loop/metrics.py`, `export.py`, `scripts/export_sessions.py` — строки задач.
- Сценарные evals с baseline, `run_batch.py` с `run_index.jsonl`.
- Будущая TUI-лента и `autobrowser replay`.

[Дизайн пайплайна](../development/2026-10-19-autonomous-test-pipeline-design.md) добавляет:
- `ModelUsage` в `ModelResponse` (токены и длительности от Ollama) и поле `usage` в
  `model.responded`;
- тайминги и стоимость из пар событий;
- baseline v2 с правилами сравнения;
- кассеты, A/B-бенчмарк и локальную историю `history.jsonl`.

**История.** [ADR 2026-09-03](2026-09-03-drop-langchain-stack-provider-neutral-model.md)
удалил LangSmith и зафиксировал: «no cloud tracing … diagnostics stay local».

**Langfuse на 2026-10-19 (проверено по официальной документации):**
- Возможности:
  - трассы, сессии, generations с токенами и стоимостью;
  - датасеты и эксперименты (`run_experiment` с оценщиками, сравнение в UI);
  - GitHub Action `langfuse/experiment-action`;
  - LLM-as-judge, разметка, управление промптами.
- SDK и протокол:
  - Python SDK v5, OTel-based с v3; требует Python 3.11+ и Cloud или self-hosted
    v4.59.0+;
  - OTLP HTTP-эндпоинт `/api/public/otel` с OSS 3.22.0, без gRPC.
- Нестабильность API:
  - мажорные версии SDK v3 → v4 (март 2026) → v5;
  - Cloud снимает legacy-API ingestion и чтения 2026-11-16;
  - self-hosted v3 получает патчи до 2027-01-31.
- Self-host (v3+): web, worker, PostgreSQL, ClickHouse, Redis, S3/MinIO; 4–8 ГБ RAM.
- Лицензия и владелец: ядро MIT; 2026-01-16 Langfuse куплена ClickHouse, лицензия
  сохранена.

**Свойства самого пайплайна:**
- Merge-gate герметичен: без сети и сервисов, на Windows и Linux. Вердикт обязан выноситься
  без внешнего сервиса.
- Даже в схеме Langfuse-экспериментов порог и провал CI пишет собственный скрипт
  (`RegressionError` по порогу).
- Основная машина пользователя — Windows; self-host означает Docker Desktop и шесть
  контейнеров.
- Трассы содержат снимки страниц, в том числе возможно залогиненных магазинов.

## Decision

1. **Langfuse не вводится** ни как зависимость, ни как сервис, ни как часть пайплайна.
   - Регрессии, бенчмарки и стоимость считаются локально из `events.jsonl`, `ModelUsage`,
     baseline и `history.jsonl`.
   - Вердикт выносит `scripts/ci.py`.
2. **Langfuse-специфичный `EventSink` на SDK не делается.**
3. **Нейтральный OTLP-экспорт фиксируется как отложенная опция** и реализуется только по
   триггеру (п. 4) отдельным шагом с собственным ADR.
   - Компонент: `OtlpEventSink` в `src/harness/otel_sink.py`, подключается в
     `_build_session_event_sink`. Движок не меняется.
   - Секция `telemetry`:
     - `otlp_enabled=false`;
     - `otlp_endpoint`;
     - `otlp_headers` (`SecretStr`);
     - `otlp_timeout_seconds`;
     - `capture_content="none" | "trace" | "full"`, по умолчанию `none`;
     - `service_name`.
   - Экспортируется только редактированная проекция `EventRecord.to_json_dict()`.
   - Spans строятся по парам событий:
     - `goal.*` — корень;
     - `model.requested/responded` — `gen_ai.operation.name=chat` с `gen_ai.usage.*`;
     - `tool.started/finished` — `execute_tool`;
     - `session.id` — атрибут.
   - `opentelemetry-api/sdk` 1.39.1 уже закреплены в `pyproject.toml`. Экспортёр
     `opentelemetry-exporter-otlp-proto-http` — в необязательной группе зависимостей.
   - Бэкенды равноправны: Langfuse (OTLP HTTP), Arize Phoenix, Jaeger, Grafana Tempo.
4. **Триггеры пересмотра:**
   - живые прогоны разбирает больше одного человека;
   - нужна ручная разметка трасс или массовый LLM-judge;
   - объём живых прогонов перестаёт помещаться в `summary.md`, `diff.md` и `replay`.

## Consequences

Плюсы:
- Нет новой зависимости, сервиса, секретов и утечки содержимого страниц. Сохраняется
  принцип локальной диагностики ADR 2026-09-03.
- Пайплайн не привязан к циклу мажорных версий SDK и к датам отключения API Cloud.
- Если UI трасс понадобится, OTLP-путь подключит Langfuse и любой другой бэкенд без
  изменения движка и без вендорного SDK.

Компромиссы:
- Нет готового веб-UI сравнения экспериментов, ручной разметки и графиков. Их заменяют
  `summary.md`/`diff.md`, `history.jsonl` и `ci.py history`, а в будущем — TUI-лента и
  `autobrowser replay`.
- LLM-as-judge при необходимости реализуется локально (`pipeline.judge="model"`), а не
  берётся готовым.
- Известный риск отложенной опции: Langfuse не полностью читает GenAI semconv v1.37+
  (сообщения в span events). Экспортёр должен писать атрибуты в форме, которую бэкенд
  понимает, и это проверяется при реализации.

## Alternatives Considered

- **(б) Langfuse-специфичный `EventSink` на SDK за флагом.** Отвергнуто:
  - вендорный SDK меняет мажорные версии чаще раза в год;
  - Cloud снимает legacy-API 2026-11-16;
  - OTLP даёт Langfuse тот же результат без SDK.
- **Langfuse-эксперименты и `experiment-action` как механизм регрессии в CI.** Отвергнуто:
  - нужен доступный сервис, а gate герметичен;
  - порог и код провала всё равно пишутся своим скриптом;
  - содержимое страниц ушло бы в облако или потребовался бы self-host из шести сервисов.
- **Self-hosted Langfuse на машине пользователя.** Отвергнуто: Docker Desktop и 4–8 ГБ RAM
  ради функций, которые не участвуют в вердикте.
- **(в) OpenTelemetry сразу, в этом же изменении.** Отложено: пока нет потребителя трасс,
  экспортёр — мёртвая поверхность и лишний код для тестов.

## Related

- [2026-10-19 Автономный пайплайн тестирования](2026-10-19-autonomous-test-pipeline.md).
- Дизайн, §4.9: [docs/development/2026-10-19-autonomous-test-pipeline-design.md](../development/2026-10-19-autonomous-test-pipeline-design.md).
- [2026-09-03 Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md).
- Код: `src/agent_loop/events.py` (`EventSink`, `redact_json_safe`), `src/harness/session.py`
  (`_build_session_event_sink`), `src/llm.py`.
- Langfuse: https://langfuse.com/docs/compatibility,
  https://langfuse.com/resources/engineering/opentelemetry-languages,
  https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd,
  https://langfuse.com/self-hosting/deployment/docker-compose,
  https://www.infoworld.com/article/4118621/clickhouse-buys-langfuse-as-data-platforms-race-to-own-the-ai-feedback-loop.html,
  https://github.com/langfuse/langfuse/issues/12657.
