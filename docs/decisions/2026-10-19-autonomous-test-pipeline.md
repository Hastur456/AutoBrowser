# ADR-2026-10-19: Автономный пайплайн тестирования — регрессии, кассеты, бенчмарки

Status: Proposed
Date: 2026-10-19

## Context

Пайплайна, который запускается без человека и сам выносит вердикт, нет:
- нет каталога `.github/` и единой точки входа;
- полный `uv run pytest` идёт около 5 минут, и его запускают вручную.

Что есть:
- **Сценарные evals.** `src/agent_loop/evals.py` (`_FakeBrowserTools`, `FakeChatModel`,
  `run_scenario`), 7 YAML-сценариев, baseline `tests/evals/baselines/agent_loop_v1.json`.
  Шесть метрик, сравнение строгим равенством в `tests/test_agent_loop_evals.py` и
  `scripts/run_evals.py`.
- **Golden-набор.** `tests/golden/tasks.jsonl` на живых сайтах, запуск через
  `scripts/run_batch.py`.
- **Телеметрия.** `events.jsonl`, `agent_trace.jsonl`, `replay.py`, `metrics.py`, `export.py`.

Чего не хватает:
- `run_batch.py` всегда возвращает 0, `expected` никто не проверяет.
- `run_evals.py` падает трейсбеком вместо отчёта.
- Baseline не видит, *какие* действия выполнены, и не учитывает размер запроса.
- `FakeChatModel` циклически повторяет ответы (`evals.py:270`) и маскирует лишние вызовы
  модели.
- `run_scenario` сводит `cancelled` к `goal.completed` (`evals.py:436`).
- `ModelResponse` не несёт usage: адаптер Ollama отбрасывает `prompt_eval_count`,
  `eval_count` и длительности. «Дороже» измерить нечем.
- Между рукописными сценариями и живым вебом нет уровня, который воспроизводил бы поведение
  реальной модели офлайн.
- `pywin32==311` без маркера платформы делает `uv sync` на Linux невозможным.
- Маркеров pytest нет.
- Истории результатов нет, поэтому «стало медленнее» не с чем сравнить.
- Параллельно проектируется терминальный UI
  ([ADR](2026-10-19-session-controller-and-local-web-ui.md)) с собственной тестовой
  стратегией: снимки, pyte, ConPTY. Её нужно встроить в тот же пайплайн.

Подробный дизайн: [docs/development/2026-10-19-autonomous-test-pipeline-design.md](../development/2026-10-19-autonomous-test-pipeline-design.md).

## Decision

### Уровни и что гейтит merge

- **Merge-gate.** Герметичен: без сети, модели и браузера; на `windows-latest` и
  `ubuntu-latest`. В него входят:
  - T0 static (`ruff`, по умолчанию только отчёт);
  - T1 unit/contract;
  - T2a рукописные evals против baseline v2;
  - T2b воспроизведение кассет;
  - T3 UI (снимки, pyte, ConPTY на Windows);
  - T4 офлайн-бенч.

  Детерминированные прокси (`request_chars`, `bytes_per_frame`) гейтят всегда.
  Статистическая часть T4 гейтит только при `pipeline.bench_gate=true`.
- **Nightly.** Всё из gate плюс:
  - T5 — реальная модель и реальный Playwright MCP на локальном фикстурном сайте
    `tests/pipeline/sites/`;
  - T6 — golden-набор на живых сайтах;
  - T7 — живые бенчмарки против истории.

  Живые уровни повторяются `live_repeats` раз и не блокируют merge. Деградация открывает или
  обновляет issue.

### Единая точка входа

- `uv run scripts/ci.py run --profile gate|nightly | --stages …` — тонкий оркестратор над
  pytest и in-process стадиями. Сопутствующие команды: `baseline show|update`,
  `cassette record|list|prune`, `report`, `history`.
- Подкоманда `autobrowser test` не вводится: `tests/` не входят в пакет, а подкоманды CLI —
  для пользователя.
- Артефакты:
  - `report.json` (`schema_version`);
  - `summary.md` — первая строка: регрессия да/нет · скорость · стоимость;
  - `diff.md`, `junit/*.xml`, `bench.json`;
  - трассы только упавших сценариев.
- Коды возврата (приоритет сверху вниз):
  - `2` — ошибка пайплайна;
  - `1` — функциональная регрессия;
  - `3` — гейтящая деградация производительности или стоимости;
  - `4` — недоступна стадия из `--require`;
  - `0` — всё в порядке.

### Регрессия

- **Baseline v2** (`tests/evals/baselines/agent_loop_v2.json`, `schema_version: 2`). К шести
  метрикам v1 добавляются:
  - `final_answer`, `error_count`, `approval_request_count`, `observation_count`;
  - `action_signature` — sha256 последовательности `(tool, ключи аргументов)`;
  - `request_chars` — сообщения плюс сериализованные `ToolDef`.

  Отдельный блок `scenarios_with_memory` хранит baseline для `--memory-seed`.
- **Правила.** Словарь «метрика → вид сравнения» (`exact`, `upper_bound`, `ratio`, `report`)
  фиксирован в `src/agent_loop/comparison.py`. Допуски задаёт `PipelineSettings`.
  - Детерминированные стадии: счётчики точно, любое отличие — `changed` с указанием
    направления.
  - `request_chars` — относительный допуск.
  - Живые стадии: доля успешных повторов и верхние границы с `live_count_slack`.
  - Отсутствующая или лишняя строка baseline — провал.
- **Детерминизм:**
  - `FakeChatModel(strict=True)`: исчерпание ответов — ошибка;
  - статус через `goal_status_from_completion()`;
  - `PYTHONHASHSEED=0`;
  - без временных меток и id в сравнении;
  - стадии получают очищенное окружение с закоммиченным пустым профилем
    `tests/pipeline/hermetic.yaml`, а отчёт фиксирует отличные от дефолта настройки с
    редактированием;
  - для живых прогонов — новый `llm.seed` при `temperature=0.0`.
- **Обновление baseline, кассет и UI-снимков — только явная команда**
  `ci.py baseline update`. CI их не пишет. PR, меняющий baseline, показывает дифф метрик в
  сводке.
- **Грейдеры golden-набора** (`src/agent_loop/graders.py`) проверяют только текст ответа
  генерическим словарём: `contains`, `not_contains`, `regex`, `min_items`/`max_items`/`items`,
  `numbers_at_most`/`numbers_at_least`. Неизвестный ключ — `ungraded`.
  - LLM-оценщик — opt-in `pipeline.judge="model"`.
  - `run_batch.py --grade` возвращает 1 при провале.

### Кассеты

- **Запись.** `SessionConfig.cassette_path` включает обёртки в `SessionContext.initialize`:
  - `RecordingChatModel` вокруг модели;
  - `RecordingToolSource` вокруг источника MCP-инструментов.

  Обе живут в `src/harness/cassette_recorder.py`. Движок о записи не знает.
- **Воспроизведение.** `ReplayChatModel` и `CassetteToolSource`/`ReplayTool` из
  `src/agent_loop/cassettes.py`.
  - Ответы идут строго по порядку; инструменты сопоставляются по `(name, canonical_args)`.
  - `ReplayTool` сохраняет `server` и `annotations`, чтобы снимки по `readOnlyHint`
    работали как с `MCPTool`.
- **Дайджесты.** Расхождение `structure_digest` (роли сообщений, набор предложенных
  инструментов) — `cassette_drift`, провал с подсказкой перезаписи. Расхождение только
  содержимого (правка промпта) — информационное `prompt_changed`.
- **Секреты.** Редактируются согласованно в аргументах инструментов и в `tool_calls`
  записанных ответов.
- **Приватность.** Коммитятся только кассеты фикстурного сайта (T5). Кассеты реального веба
  остаются в git-ignored `storage.ci_dir`.

### Бенчмарки

- **Usage.** Аддитивный нейтральный `ModelUsage` в `src/llm.py`, поле
  `ModelResponse.usage`. Адаптер Ollama заполняет токены и длительности. Движок только
  кладёт `usage` в payload `model.responded`; новых `EventType` нет.
- **Живые тайминги** выводятся из существующих пар событий
  (`src/agent_loop/benchmarks.py`):
  - латентность задачи;
  - время до плана и до первого инструмента;
  - латентность модели по фазам с отделённой холодной загрузкой;
  - латентность инструментов.

  Стоимость = токены × `llm.input_price_per_mtok`/`output_price_per_mtok`; при нулевой цене
  вердикт «unknown».
- **PR.** Парный A/B: base и head (`git worktree` от merge-base) на одной машине, прогрев,
  чередование, медианы, бутстрап-интервал отношения с фиксированным seed. «Быстрее» или
  «медленнее» — только если весь интервал вышел за `bench_threshold`.
- **Nightly.** Скользящая история `history.jsonl`: локально в `storage.ci_dir`, в CI — в
  orphan-ветке `ci-history`. Деградация требует одновременно:
  - превышения медианы окна на `bench_threshold`;
  - робастного z-score по MAD выше `history_mad_z`;
  - `history_consecutive` ночей подряд.

  p95 считается только при числе выборок ≥ `bench_p95_min_samples`.

### Настройки

- Новая секция `pipeline` (`PipelineSettings`): стадии профилей, таймауты, повторы,
  допуски, параметры A/B и истории, `bench_gate`, `lint_gate`, `judge`.
- Новые поля в существующих секциях: `storage.ci_subdir` (computed `ci_dir`), `llm.seed`,
  `llm.input_price_per_mtok`, `llm.output_price_per_mtok`.
- Всё документируется в `.env.example` и `config.example.yaml` и проверяется
  `tests/test_config.py`.

### CI

- `.github/workflows/gate.yml`: `pull_request` и `push main`, матрица Windows/Linux,
  `astral-sh/setup-uv`, `uv sync --locked`, `ci.py run --profile gate --base …`, сводка в
  `GITHUB_STEP_SUMMARY`, артефакты.
- `.github/workflows/nightly.yml`: cron + `workflow_dispatch`, рекомендован self-hosted
  Windows-раннер, секреты модели и пути Chrome через `AUTOBROWSER_*`, `--notify issue`.
- Локальный эквивалент — `schtasks`.

### Предварительные условия

- `pywin32 ; sys_platform == 'win32'`.
- Маркеры pytest `live`/`bench`/`conpty`/`ui` с `--strict-markers` и
  `addopts = -m "not live and not bench"`.
- Dev-группа зависимостей для UI-тестов.
- `.gitattributes` с `eol=lf` для снимков и кассет.

## Consequences

Плюсы:
- Одна команда локально и в CI отвечает «регрессия есть/нет» и «быстрее/медленнее/дороже»
  кодом возврата и строкой сводки.
- Merge-gate герметичен и одинаков на Windows и Linux.
- Кассеты дают детерминированную проверку harness, наблюдения и контекста на реальном
  поведении модели — уровень, которого не было.
- Найденные дефекты eval-харнесса исправляются аддитивно: циклический `FakeChatModel`,
  `cancelled` → `completed`, отсутствие диффа, `run_batch` с кодом 0.
- Стоимость измеряется (токены, `request_chars`) без сервиса наблюдаемости.
- Решение по Langfuse не требуется для вердикта (см. связанный ADR).

Компромиссы и риски:
- Новый код оркестратора и аналитики требует собственных тестов (`tests/test_ci_pipeline.py`
  и др.).
- Кассеты устаревают при изменении формы разговора, и их нужно перезаписывать. Правка
  промпта без изменения формы их не ломает.
- A/B удлиняет gate (worktree + `uv sync`). Стадия идёт только при изменениях кода,
  статистика по умолчанию не гейтит до калибровки.
- Секция `pipeline` в `src/config.py` не читается рантаймом агента. Это осознанно: так
  требует инвариант о тюнаблах, и дрейф шаблонов проверяется бесплатно.
- Живые уровни зависят от машины с Chrome, Node и ключом модели. Рекомендован self-hosted
  раннер; живой веб (T6) только отчитывается.
- Корневой `.env` читается из cwd до внедрения `AUTOBROWSER_HOME`; preflight
  предупреждает.

Дальнейшая работа:
- `docs/development/testing-pipeline.md`.
- `docs/diagrams/test-pipeline.md`.
- Команды в `AGENTS.md`/`CLAUDE.md`.
- Удаление `agent_loop_v1.json` после принятия v2.

## Alternatives Considered

- **Только pytest-маркеры и YAML GitHub Actions.** Отвергнуто: вердикт (A/B, история,
  «дороже») оказался бы размазан по YAML и плагинам и не воспроизводился бы локально одной
  командой.
- **nox/tox/make.** Отвергнуто: новая зависимость без выигрыша. Вердикт всё равно писать
  самим, `make` неудобен на Windows.
- **Реальная модель в merge-gate (`temperature=0`, `seed`).** Отвергнуто: сеть и секреты в
  PR, облачные модели не бит-детерминированы, медленно. Этот уровень идёт ночью.
- **Абсолютные пороги производительности.** Отвергнуто: шум общих раннеров ±20–30 %.
- **Сравнение PR только с историей main.** Отвергнуто для PR по той же причине. Для
  живых ночных метрик принято с MAD и гистерезисом.
- **Langfuse-эксперименты как механизм регрессии.** Отвергнуто: порог и провал CI всё равно
  пишет наш скрипт, а в герметичном gate сервис недоступен. См.
  [ADR о Langfuse](2026-10-19-langfuse-observability.md).
- **Строгое сравнение кассет по полному тексту запроса.** Отвергнуто: каждая правка промпта
  пользователем ломала бы все кассеты.
- **Подкоманда `autobrowser test`.** Отвергнуто: тесты не входят в пакет, а пространство
  подкоманд принадлежит пользователю.

## Related

- Дизайн: [docs/development/2026-10-19-autonomous-test-pipeline-design.md](../development/2026-10-19-autonomous-test-pipeline-design.md).
- [2026-10-19 Langfuse не вводим; нейтральный OTLP-экспорт как отложенная опция](2026-10-19-langfuse-observability.md).
- [2026-10-19 Терминальный UI над SessionController](2026-10-19-session-controller-and-local-web-ui.md):
  стадия `ui` и бенч рендера.
- [2026-10-19 Команда `autobrowser` и домашний каталог](2026-10-19-autobrowser-command-and-home-directory.md):
  `AUTOBROWSER_HOME` снимает чтение `.env` из cwd.
- [2026-10-19 Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md): кассеты и
  грейдеры не вводят логики по именам.
- [2026-09-16 Typed Settings Module](2026-09-16-typed-settings-module.md): секция `pipeline`.
- [2026-09-03 Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md):
  `ModelResponse`, `FakeChatModel`, локальная диагностика.
- Код: `src/agent_loop/evals.py`, `src/agent_loop/metrics.py`, `src/agent_loop/replay.py`,
  `src/agent_loop/batch.py`, `src/agent_loop/events.py`, `src/llm.py`,
  `src/providers/ollama.py`, `src/harness/session.py`, `scripts/run_evals.py`,
  `scripts/run_batch.py`, `tests/test_agent_loop_evals.py`, `tests/evals/`, `tests/golden/`.
