# ADR-2026-11-08: Траектория прогона в метаданных корневого наблюдения

Status: Accepted
Date: 2026-11-08

Дополняет: [ADR-2026-10-31: Langfuse как платформа датасетов, экспериментов и
оценок](2026-10-31-langfuse-platform-integration.md) (п. 7, LLM-судья) и
[ADR-2026-10-29: OTLP-экспорт трасс](2026-10-29-otlp-trace-export.md).

## Context

- LLM-судья настроен на корневом наблюдении item-трейса (фильтр Is Root Observation) и должен
  оценивать путь агента, а не только ответ.
- В Langfuse v4 evaluator наблюдения видит только поля самого наблюдения: Input, Output,
  Metadata, Tool Calls (и expected output / item metadata в экспериментах). Соседние и дочерние
  наблюдения он не загружает. Переменной вида `{{trace}}` на всё дерево нет.
- Вызовы инструментов лежат в дочерних спанах `execute_tool {tool}` под `invoke_agent
  autobrowser`. Поле Tool Calls корня пустое, потому что корень — не генерация.
- Langfuse рекомендует для оценки всего агента собирать нужный контекст на корневом
  наблюдении.
- Метаданные по OTLP можно передать двумя способами:
  - по ключу: `langfuse.observation.metadata.<key>`. JSON-значение такого ключа сервер
    раскрывает только на спанах своих SDK (Python ≥ 5, JS ≥ 6) или при заголовке
    `x-langfuse-sdk-version`. От прямого экспортёра вложенный объект придёт строкой, и JSONPath
    по нему не пройдёт;
  - одним атрибутом `langfuse.observation.metadata` с JSON-объектом всех метаданных. Его сервер
    разбирает на любом спане, и именно так метаданные шлёт Python SDK v5.

## Decision

1. **Нейтральный построитель.** `src/harness/otel_spans.task_trajectory(events,
   capture_content=…)` превращает события одной задачи в JSON-список шагов:
   - `task`;
   - `plan` — первый план и каждый реплан;
   - `tool_call` — от `action.proposed` до `tool.finished`, с заметными решениями гейтов в
     `gates`. Статус берётся у инструмента; `blocked`, если гейт ответил `deny`/`blocked`;
     `not_executed`, если вызов не стартовал по другой причине;
   - `gate` — заметное решение вне вызова (хуки `goal_start`, `stop`, `post_tool_use*`) на своём
     месте в последовательности: оно не теряется и не приписывается следующему вызову;
   - `model_error`;
   - `outcome` — `incomplete`, если терминального события нет.

   Режимы содержимого те же, что у спанов: `none` — имена, id, статусы и счётчики; `answers`
   добавляет текст задачи и ответ; `redacted` — аргументы, результаты, причины, шаги плана и
   сообщения ошибок. Значения берутся только из `EventRecord.to_json_dict()`, то есть уже
   отредактированные и усечённые. `trajectory_summary` нумерует шаги и добавляет `stop_reason`
   и `final_status` последней задачи. Имён вендоров и инструментов в коде нет.
2. **Эксперименты.** Корень `scenario {id}` получает атрибут `langfuse.observation.metadata` с
   объектом `{full_trajectory, stop_reason, final_status, run_status, task_statuses}` по всем
   задачам сценария. `final_status` — статус последней цели, `run_status` — статус прогона
   сценария: словари у них разные, поэтому и ключи разные. Сценарий без трассы получает
   пустую траекторию и `final_status: null`. Атрибут
   включён всегда: отдельной настройки нет, содержимое регулирует `capture_content`. В судье
   переменная привязывается к Metadata с JSONPath `$.full_trajectory`.
3. **Живой экспорт.** Настройка `telemetry.trajectory` (по умолчанию `false`) кладёт тот же
   объект на спан задачи под нейтральным ключом `autobrowser.trajectory`. Для Langfuse
   пользователь добавляет алиас `autobrowser.trajectory → langfuse.observation.metadata`, так
   что в `src/` вендор по-прежнему не назван.
4. **Отдельного лимита размера нет.** Строки уже усечены по `events.max_string_chars`. Слишком
   большой трейс делится на запросы по `eval_platform.max_request_bytes`, а корень при
   необходимости уходит отдельным запросом.

## Consequences

- Судья на корне видит весь путь: какие инструменты вызывались, с каким исходом, что
  заблокировали гейты, чем закончилась каждая задача. С `capture_content: redacted` он видит
  ещё аргументы и результаты.
- Корень item-трейса растёт на размер траектории. Units Langfuse считаются по наблюдениям,
  поэтому стоимость не меняется.
- В `redacted` содержимое страниц уходит и в метаданные корня, а не только в спаны
  инструментов. Это то же решение о приватности, что и раньше.
- Если у корня появятся другие метаданные, их нужно класть в тот же объект, а не отдельными
  ключами: так один источник правды и предсказуемый разбор.
- Поле Tool Calls корня остаётся пустым. Langfuse выводит его из сообщений генераций, а корень
  генерацией не является; траектория в метаданных его заменяет.

## Alternatives Considered

- **Ключ `langfuse.observation.metadata.full_trajectory`** (первый черновик). Отвергнут:
  вложенный JSON от прямого экспортёра остаётся строкой. Кроме того, черновик читал атрибуты
  `langfuse.observation.*` у дочерних спанов, которых там нет (у них ключи `gen_ai.*`), и
  получал одни имена.
- **Траектория во `input`/`output` корня.** Отвергнута: `input` — вход item датасета, `output`
  — ответ. Их ждут судья по `expected_output` и сравнение прогонов.
- **Синтетическая генерация с `tool_calls` в формате ChatML, чтобы заполнить Tool Calls.**
  Отвергнута: формат извлечения не документирован как контракт, добавились бы лишнее
  наблюдение и units.
- **Устаревшие trace-level evaluators.** Отвергнуты: в Cloud перестают работать после
  2026-11-16.

## Related

- Код: `src/harness/otel_spans.py` (`task_trajectory`, `trajectory_summary`,
  `TRAJECTORY_KEY`), `scripts/langfuse_bridge/experiments.py` (`OBSERVATION_METADATA`,
  `_root_metadata`), `src/harness/otel_sink.py`, `src/config.py` (`TelemetrySettings.trajectory`).
- Гайды: `docs/development/eval-platform.md` (Trajectory Judge),
  `docs/development/telemetry.md` (Judging the trajectory).
- Тесты: `tests/test_otel_trajectory.py`, `tests/test_langfuse_experiments.py`,
  `tests/test_config.py`.
- Langfuse (проверено 2026-11-08):
  https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge,
  https://langfuse.com/integrations/native/opentelemetry,
  https://langfuse.com/integrations/native/opentelemetry/migration-to-v4,
  https://github.com/langfuse/langfuse/pull/18528,
  https://github.com/langfuse/langfuse/pull/18911,
  https://github.com/langfuse/langfuse-python/pull/1944.
