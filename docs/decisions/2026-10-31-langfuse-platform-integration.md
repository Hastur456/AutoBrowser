# ADR-2026-10-31: Langfuse как платформа датасетов, экспериментов и оценок поверх локальных результатов

Status: Proposed
Date: 2026-10-31

Заменяет в части: [ADR-2026-10-19: Langfuse не вводим; нейтральный OTLP-экспорт как отложенная
опция](2026-10-19-langfuse-observability.md) — пункт 1 решения («Langfuse не вводится ни как
зависимость, ни как сервис, ни как часть пайплайна») и запрет на datasets/scores API из плана
Part B (§3.3, Q-C). Пункты о SDK, герметичном gate и `experiment-action` сохраняются.

## Context

Пользователь хочет:
- версионировать наборы сценариев (Golden Set, будущий live-набор);
- получить observability-платформу для анализа трейсов, регрессионных прогонов и оценки
  агента.

Функции платформы (датасеты, эксперименты и сравнение прогонов, scores, LLM-as-judge, ручная
разметка) должны браться из Langfuse, а не писаться заново.

**Что уже есть** (Part A, ветка `feat/batch-analytics`):
- папка батча с `batch.json` (провенанс: commit, model, `tasks_sha256`, `settings_sha256`);
- `run_index.jsonl` v1 (`failure_kind`, `task_outcomes`, грейд с checks, метрики);
- полные трассы в `traces/`, `history.jsonl`;
- локальный вердикт `compare_batches.py`.

Part B (`feat/otlp-export`, план 2026-10-29) добавит нейтральный `OtlpEventSink`: один трейс на
задачу, `session.id` и метки батча на спанах, без содержимого по умолчанию.

**Почему решение 2026-10-19 пересматривается:**
- Выполнены его триггеры: нужны ручная разметка и LLM-судья, а объём живых прогонов растёт
  (18 сценариев × N прогонов × модели).
- Изменились факты (проверено 2026-10-31):
  - В Langfuse v4 эксперимент — это набор OTLP-трейсов с атрибутами `langfuse.experiment.*`,
    по одному трейсу на item. Python SDK v5 пишет эксперименты тем же путём, а
    `POST /dataset-run-items` в Cloud снимается 2026-11-16. Эксперимент можно собрать из уже
    выполненного прогона без SDK, той же OTLP-инфраструктурой, что Part B.
  - Элементы датасетов версионируются нативно: timestamps, с 2025-12-15; эксперименты на
    версии — с 2026-02-11.
  - Scores пишутся `POST /api/public/scores` списком; `id` вместе с `timestamp` дают upsert.
    Чтение — Scores API v3.
  - Есть управляемые observation-level LLM-судьи и очереди разметки; на Cloud Hobby — одна
    очередь.
  - Langfuse v4 не дедуплицирует повторно отправленные спаны: «export once».
  - Прямым OTLP-экспортёрам нужен заголовок `x-langfuse-ingestion-version: 4`.
  - Python SDK 5.1.0 (2026-10-30) требует `opentelemetry-* >= 1.45.0`, а проект закрепляет
    1.39.1. Мажоры SDK: v3 2025-06, v4 2026-03, v5 2026-10.

**Ограничения проекта:**
- вердикт регрессии и коды возврата CI герметичны и локальны;
- локальные файлы — источник истины;
- в `src/` нет имён вендоров и URL; движок не импортирует аналитику и интеграции;
- все тюнинги — в `src/config.py`; секреты — `SecretStr`;
- по умолчанию содержимое страниц не уходит с машины;
- тесты герметичны; Windows first.

Подробный дизайн: [Интеграция с Langfuse](../development/2026-10-31-langfuse-integration-design.md).

## Decision

1. **Гибрид: локальный вердикт и Langfuse как второй интерфейс.**
   - Регрессию по-прежнему решают `compare_batches.py` и будущий `scripts/ci.py`, без сети.
   - Langfuse получает копию результатов для просмотра, сравнения, судейства и разметки.
   - Локальный вердикт записывается в Langfuse как scores (`regression` на item, `verdict` и
     `regressions` на эксперименте) и там не пересчитывается.
   - Gate синхронизацию не вызывает.
2. **Без Langfuse SDK.**
   - Интеграция — узкий REST-клиент на уже закреплённом `httpx`: датасеты, элементы, scores,
     score configs, очереди разметки, чтение Observations v2, Scores v3 и Experiments.
   - Эксперименты пишутся через OTLP/HTTP в JSON-кодировке с атрибутами
     `langfuse.experiment.*`.
   - Новых зависимостей нет.
3. **Эксперимент создаётся постфактум из папки батча.**
   - Один батч — один эксперимент (`langfuse.experiment.id = batch_id`).
   - Один сценарий — один item и один item-трейс: корень `scenario {id}`, под ним спаны задач,
     вызовов модели и инструментов с исходным временем из `traces/*.events.jsonl`.
   - Живой OTLP-экспорт для сессий такого батча отключается, чтобы исполнение не пришло в
     Langfuse дважды.
4. **Идемпотентность и восстановление.**
   - Детерминированные id: элемент датасета — от имени датасета и `scenario_id`; трейс, спаны
     и scores — от эксперимента и цели.
   - Фиксированный `timestamp` scores (`finished_at` батча).
   - Леджер `<batch_dir>/platform_sync.json` хранит состояния `pending → sending →
     exported | unknown`; `unknown` проверяется через Observations v2 перед повтором.
   - Item-трейс, принятый сервером, никогда не отправляется повторно.
   - Смена проекта начинает новую попытку (`batch_id.N`) с новыми id.
   - Недоступность Langfuse не роняет батч и не меняет его код возврата; синхронизацию
     догоняет `langfuse_sync.py experiment push --pending`.
5. **Версия датасета.**
   - Каноническая версия — `tasks_sha256` содержимого файла, человеческая метка — git-коммит.
   - Нативная версия Langfuse (timestamp `validFrom`) — проекция; соответствие записывается в
     журнал `<storage.batches_dir>/platform_datasets.jsonl`.
   - `dataset push` отправляет только изменённые элементы (по `content_sha256`), удалённые
     сценарии архивирует и отказывается работать на грязном файле без `--allow-dirty`.
   - Семантическая версия не вводится.
   - `expected` переносится в `expected_output` как есть: `{"checks": expected, "reference":
     …}`; у сценария появляется необязательное поле `reference`.
6. **Оценки.**
   - На корень item: `passed`, `failure_kind`, `infra_failure`, `grade`, `check.<key>`,
     `regression` и счётчики, которых Langfuse не выводит сам.
   - На наблюдения задач: `stop_reason`, `task_failure_kind`.
   - На эксперимент: `pass_rate`, `agent_failures`, `infra_failures`, `verdict`, `regressions`.
   - Score configs создаются командой `setup` из `Literal` кода; при росте словаря старый
     config архивируется и создаётся новый.
   - Длительность, токены и стоимость Langfuse выводит из спанов сам; цены модели заводятся в
     Langfuse.
7. **LLM-судья и разметка — средствами Langfuse, только информационно.**
   - Ключи `expected` без детерминированного грейдера (`fields`) оценивает управляемый
     evaluator на корне item-трейса (закрывает Q7 родительского дизайна).
   - Провалившиеся сценарии по `triage_kinds` ставятся в очередь разметки.
   - `scores pull` выгружает оценки судьи и людей в `<batch_dir>/platform_scores.jsonl`.
   - Ни судья, ни разметка не входят в `failure_kind`, вердикт и gate.
8. **Границы кода.**
   - Всё, что знает Langfuse, живёт в `scripts/langfuse_bridge/` и `scripts/langfuse_sync.py`
     (не в wheel, как `scripts/pipeline/` родительского дизайна).
   - Нейтральные чистые модели — в `src/`: `src/agent_loop/platform_records.py` (элементы
     датасета, score-записи, словарь определений); `src/harness/otel_spans.py` (дерево спанов
     и OTLP/JSON, вносится в Part B).
   - Секция настроек называется нейтрально: `eval_platform` (`EvalPlatformSettings`, без
     дефолтного URL); вендорские значения — только в `config.example.yaml`.
   - `tests/test_engine_boundaries.py` проверяет: в `src/` нет слова `langfuse`; `src/`
     ничего не импортирует из `scripts/`; `platform_records` входит в `PIPELINE_MODULES`.
9. **Приватность.**
   - `eval_platform.capture_content = "none"` по умолчанию: уходят только данные репозитория
     (тексты задач, `expected`), идентификаторы, статусы, метрики и scores.
   - `answers` добавляет тексты задач и ответы агента, `redacted` — ещё вход и выход
     инструментов. Всё проходит `redact_json_safe`.
10. **Поправки к Part B** (вносятся в план до старта ветки 3):
    - чистый построитель спанов (PB-1);
    - нейтральные ключи содержимого `input.value`/`output.value` и `gen_ai.tool.call.*` (PB-2);
    - имя корня `invoke_agent autobrowser` (PB-3);
    - `telemetry.environment` → `deployment.environment.name` (PB-4);
    - конфиг-алиасы атрибутов `telemetry.attribute_aliases` для `langfuse.trace.metadata.*` и
      `langfuse.release` без вендоров в коде (PB-5);
    - `SessionConfig.otlp_export` (PB-6);
    - заголовок `x-langfuse-ingestion-version: 4` и self-host от v4.59.0 в документации (PB-7);
    - тест «ни один `span_id` не экспортируется дважды» (PB-8).
11. **Порядок веток.** Новая ветка `feat/langfuse-sync` идёт сразу после `feat/otlp-export`, до
    `feat/cassettes`. Оркестратор получает не гейтящую стадию `platform_sync` (только nightly),
    live-ветка — набор `live-local` как датасет, nightly — `experiment push --pending` и
    секреты платформы.
12. **Развёртывание.** Рекомендуется Langfuse Cloud Hobby в регионе EU; код от выбора не
    зависит. Self-host — по запросу, от v4.59.0. `langfuse_sync.py doctor` проверяет версию,
    ключи и проект.

## Consequences

Плюсы:
- Пользователь получает датасеты с версиями, сравнение прогонов, scores, судью и разметку
  платформы без собственной реализации этих функций.
- Вердикт и gate остаются герметичными и не зависят от сервиса, хранения 30 дней или
  недетерминированного судьи.
- Langfuse — восстанавливаемая проекция: любой батч можно синхронизировать позже, повторно
  или в другой проект. Выход из Langfuse не теряет данных, а разметку сохраняет `scores pull`.
- Нет вендорного SDK, нет подъёма закреплённого стека OpenTelemetry, нет новых зависимостей.
- Нейтральные модели (`platform_records`, `otel_spans`) годятся для второго бэкенда, например
  Phoenix с его datasets и experiments.

Компромиссы и риски:
- Свой REST-клиент примерно на 12 эндпоинтов нужно поддерживать при изменениях API Langfuse.
  Смягчение: один модуль, фейковый сервер, сверенный с OpenAPI, проверка версии сервера.
- Детерминированные id и леджер не исключают дубль полностью: если ответ потерян, а приём
  Langfuse отстал больше `verify_grace_seconds`, повтор может создать второй трейс.
  `status --verify` находит такие случаи.
- Cloud Hobby: около 1100 units на golden-прогон (≈45 прогонов в месяц), 30 req/min на REST,
  хранение 30 дней.
- Судья требует `capture_content ≥ answers` и LLM-подключения со structured outputs; стоит
  денег.
- Исключение «секций наблюдателей» (`pipeline`, `telemetry`, `eval_platform`) из отпечатка
  настроек один раз даёт предупреждение `settings differ` при сравнении через границу
  изменения.
- В `scripts/` появляется ещё один неупакованный пакет кода разработчика.

Последующая работа:
- поправки PB-1…PB-8 в плане Part B;
- гайд `docs/development/eval-platform.md`, диаграммы, глоссарий, команды в `AGENTS.md` и
  `CLAUDE.md`;
- ответы на открытые вопросы QL1–QL11 дизайна;
- живая проверка на отдельном проекте Cloud;
- перевод этого ADR в Accepted.

## Alternatives Considered

- **Langfuse Python SDK v5** (`run_experiment`, `create_score`, `get_dataset`). Отвергнут:
  - требует `opentelemetry-* >= 1.45.0` против закреплённых 1.39.1;
  - три мажора за 16 месяцев;
  - `run_experiment` сам исполняет задачу, и прикрепить уже выполненный прогон с исходным
    временем нельзя;
  - сеть понадобилась бы во время прогона.
- **`langfuse/experiment-action` в CI.** Отвергнут:
  - исполняет эксперимент внутри SDK (по умолчанию v4.6.0) на GitHub-раннере без Chrome;
  - вердикт всё равно пишется своим `RegressionError`;
  - nightly идёт на self-hosted Windows-раннере.
- **Живой OTLP-синк Part B пишет атрибуты эксперимента во время батча.** Отвергнут:
  - модель «трейс на задачу» не совпадает с «трейс на item»;
  - грейд и `failure_kind` ещё неизвестны;
  - потерянную отправку нельзя повторить из-за правила «export once».
- **Langfuse как источник вердикта.** Отвергнут: сеть в gate и nightly-вердикте, судья
  недетерминирован, хранение ограничено, lock-in на решение «есть ли регрессия».
- **Код интеграции в `src/integrations/langfuse/` и секция `langfuse` в настройках.**
  Отвергнут: нарушает правило «нет имён вендоров в `src/`» и попадает в wheel пользователя.
- **Отдельный пакет верхнего уровня `integrations/`.** Не выбран: новая конвенция репозитория
  без выигрыша перед `scripts/`, где уже живут инструменты пайплайна.
- **Семантическая версия наборов (`v1.2.0`).** Отвергнута: ручной подъём, расхождение с
  содержимым; пара `tasks_sha256` + коммит её покрывает.
- **Синхронизация scripted evals как экспериментов.** Отвергнута: они детерминированы и
  сравниваются точным baseline, а синтетические трейсы — шум и расход units.
- **Локальный LLM-судья (`pipeline.judge="model"`).** Отвергнут в пользу управляемого судьи
  платформы: это прямой запрос пользователя.
- **Self-host Langfuse по умолчанию.** Не выбран для старта: 6 контейнеров, рекомендовано от
  16 GiB / 4 ядер, compose не для production. Остаётся вариантом без изменений кода.

## Related

- Дизайн: [docs/development/2026-10-31-langfuse-integration-design.md](../development/2026-10-31-langfuse-integration-design.md).
- Заменяет в части: [2026-10-19 Langfuse не вводим; нейтральный OTLP-экспорт как отложенная опция](2026-10-19-langfuse-observability.md).
- [2026-10-19 Автономный пайплайн тестирования](2026-10-19-autonomous-test-pipeline.md) — gate и
  nightly, вердикт остаётся за `ci.py`.
- [2026-09-03 Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md) —
  «no cloud tracing … diagnostics stay local» уточняется: локальная диагностика остаётся
  источником истины, облачная платформа — opt-in проекция.
- [2026-09-16 Typed Settings Module](2026-09-16-typed-settings-module.md) — секция
  `eval_platform`.
- План Part A/B: [docs/development/2026-10-29-batch-analytics-and-otlp-export-plan.md](../development/2026-10-29-batch-analytics-and-otlp-export-plan.md);
  ADR Part B (`2026-10-29-otlp-trace-export`) появится в шаге B1.
- Код: `src/agent_loop/batch.py` (`run_batch`, `settings_provenance`, `tasks_sha256`),
  `src/agent_loop/failures.py`, `src/agent_loop/graders.py`, `src/agent_loop/batch_compare.py`,
  `src/contracts.py` (`StopReason`), `src/harness/session.py` (`SessionConfig.labels`,
  `_build_session_event_sink`), `scripts/run_batch.py`, `scripts/compare_batches.py`,
  `tests/test_engine_boundaries.py`.
- Langfuse (проверено 2026-10-31):
  https://langfuse.com/docs/compatibility,
  https://langfuse.com/faq/all/deprecated-api-migration,
  https://langfuse.com/integrations/native/opentelemetry,
  https://langfuse.com/integrations/native/opentelemetry/migration-to-v4,
  https://langfuse.com/integrations/native/opentelemetry/experiments,
  https://langfuse.com/docs/evaluation/experiments/datasets,
  https://langfuse.com/changelog/2025-12-15-dataset-versioning,
  https://langfuse.com/changelog/2026-02-11-versioned-dataset-experiments,
  https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk,
  https://langfuse.com/changelog/2026-06-10-scores-v3-api,
  https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge,
  https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues,
  https://langfuse.com/pricing,
  https://langfuse.com/self-hosting/deployment/docker-compose,
  https://cloud.langfuse.com/generated/api/openapi.yml,
  https://github.com/langfuse/experiment-action,
  https://pypi.org/project/langfuse/.
