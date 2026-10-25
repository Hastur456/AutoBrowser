# ADR-2026-10-26: Скиллы агента по модели Agent Skills

Status: Proposed
Date: 2026-10-26
Replaces: `PromptResource` / `browser_agent_rules_resource()` (`src/agent_loop/skills.py`) — не
отдельный ADR

## Context

«Скилл» сейчас — один зашитый файл правил браузера
(`src/agent_loop/resources/browser-agent-rules.md`). Сессия с браузерным MCP-сервером
вставляет его в системный промпт целиком на каждом ходе (`SessionContext._prompt_resources()` →
`ContextAssembler(resources=...)`). Нет каталога, выбора моделью, подгрузки по запросу,
справочных файлов и пользовательских скиллов. Готовые скиллы экосистемы Agent Skills (формат
`SKILL.md` Anthropic) взять нельзя.

Ограничения проекта: движок не знает ни хранилищ, ни имён harness-инструментов (как с памятью,
`tests/test_memory_boundaries.py`); нет логики по именам инструментов, сайтов и словам задачи
([ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)); все лимиты —
в `src/config.py` ([ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md));
пока нет CI, baseline evals не должен меняться. Термины — в [глоссарии](../glossary.md).

Подробный дизайн:
[docs/development/2026-10-26-agent-skills-design.md](../development/2026-10-26-agent-skills-design.md).

## Decision

1. **Формат — `SKILL.md` Agent Skills.** Скилл — папка с `SKILL.md`: YAML-frontmatter
   (`name`, `description` обязательны; `license`, `compatibility`, `allowed-tools`,
   `metadata` — по спецификации) и markdown-тело; рядом — справочные файлы. `name` совпадает с
   папкой. Расширения AutoBrowser — только строковые ключи в `metadata`: `always`, `requires`,
   `title`. Готовые скиллы берутся без правки.
2. **Постепенная подгрузка.** В системном промпте — блок `Skills`: каталог `name: description`.
   Тело и файлы скилла модель загружает сама harness-инструментом `skill_view(name, path?,
   view_range?)` сервера `skills` с `readOnlyHint` (по образцу `memory_view`). Путь файла —
   строго внутри папки скилла, только UTF-8.
3. **Выбирает модель.** Код не сопоставляет скиллы с задачей, URL, страницей или
   инструментами. Единственный фильтр — возможности сессии: `metadata.requires: browser`
   прячет скилл в сессии без браузерного сервера. Возможность решает `SessionContext`, как и
   сейчас (`mcp.browser_server`).
4. **`always`-скиллы.** `metadata.always: "true"` вставляет тело в системный промпт блоком
   `title` — как нынешний `PromptResource`. Правила браузера становятся первым встроенным
   скиллом `browser-agent-rules` (`always`, `requires: browser`, `title: Browser Rules`) в
   `src/agent_loop/resources/skills/browser-agent-rules/SKILL.md`; тело байт-в-байт прежнее,
   промпт не меняется. Пользователь по-прежнему правит правила сам.
5. **Источники.** Встроенные (пакет) и пользовательские — только `skills.user_dir`
   (по умолчанию `<home>/skills/`). Рабочая папка и репозиторий не сканируются. Пользовательский
   скилл с именем встроенного заменяет его (`skills.shadowed`). Недействительный скилл
   пропускается с `skills.invalid` и не валит старт; недействительный встроенный — ошибка тестов.
6. **Без скриптов в v1.** Файлы скилла читаются как текст и никогда не исполняются. Запуск кода
   — позже, отдельным инструментом под правилами разрешений и отдельным ADR. `allowed-tools`
   ни на что не влияет: авторизация остаётся за `PermissionEngine`.
7. **Скиллы отдельно от памяти L4.** Скиллы пишет человек: им доверяют, агент их не меняет,
   `MemoryContentPolicy` к ним не применяется. Процедуры `memory/procedures/*.md` пишет агент,
   они проходят staged trust. Хранилища, инструменты и серверы раздельные; автоматического
   превращения процедуры в скилл нет.
8. **Слои.** `SkillStore`/`SkillCatalog` — `src/harness/skills.py`, `skill_view` —
   `src/harness/skill_tool.py`; их импортирует только `SessionContext` (и `evals.py`).
   `ContextAssembler` получает нейтральные `PromptSection(name, title, text, priority)` и не
   знает о скиллах; `PromptResource` и `src/agent_loop/skills.py` удаляются. Движок
   (`src/agent_loop/execution/`) не импортирует модули скиллов и не называет `skill_view`
   (новый `tests/test_skill_boundaries.py`). Скиллы читаются один раз при старте сессии —
   системный промпт стабилен.
9. **Настройки.** Новая секция `skills` в `src/config.py`: `enabled` (по умолчанию `false`),
   `user_dir`, `catalog_max_chars`, `description_max_chars`, `body_max_chars`,
   `file_max_chars`, `always_max_chars`, `max_skills`; с `.env.example`,
   `config.example.yaml`, `tests/test_config.py`. Выключенные скиллы дают ровно нынешний промпт
   (`always`-скиллы работают всегда); включение добавляет каталог, `skill_view` и
   пользовательские скиллы. Тесты строят хранилище из явных `SkillsSettings`, никогда из
   `get_settings()`.
10. **Наблюдаемость.** События `skills.loaded`, `skills.invalid`, `skills.shadowed`; снимок
    `skills` в `session.json`; строка `skills` в `doctor`. Загрузка тела видна как обычный вызов
    инструмента.

## Consequences

Плюсы:
- модель видит каталог и сама подгружает только нужное; промпт не растёт с числом скиллов;
- пользователь добавляет инструкции папкой в домашнем каталоге, без правки пакета;
- готовые скиллы Agent Skills работают без конвертации;
- правила браузера получают общий механизм и могут быть заменены пользовательской версией;
- движок остаётся нейтральным, тест границ это закрепляет.

Компромиссы и риски:
- модель может не загрузить подходящий скилл — качество зависит от `description`; правила
  браузера остаются `always`, пока evals не покажут, что подгрузки по запросу достаточно;
- расширения в `metadata` — строки (`always: "true"`) — цена совместимости;
- чужой скилл из интернета — доверенный текст без политики содержимого; защита — только
  домашняя папка, предупреждение в руководстве и список в `doctor`;
- read-only ответ `skill_view` попадает в `snapshots["skills"]`, как `memory_view` — в
  `snapshots["memory"]`; если это раздувает наблюдение, нужно общее решение для
  harness-серверов;
- изменения скиллов видны только после перезапуска сессии;
- двенадцатая секция настроек и ещё один harness-сервер.

Последующая работа:
- запуск скриптов скиллов под правилами разрешений — отдельный ADR;
- `/skills` в TUI, вызов скилла пользователем, горячая перезагрузка;
- решение, переводить ли правила браузера с `always` на подгрузку по запросу, — по данным
  evals и пайплайна ([ADR-2026-10-19](2026-10-19-autonomous-test-pipeline.md)).

## Alternatives Considered

- **Расширить `PromptResource` папкой ресурсов.** Без каталога и выбора моделью — та же проблема
  в большем масштабе. Не выбрано.
- **Подбор скиллов кодом по словам задачи, URL или инструментам.** Противоречит направлению
  «модель решает, без хардкодов» и name-free ADR. Не выбрано.
- **Одно хранилище с процедурами L4.** Смешивает доверенный текст человека и непроверенный
  текст агента. Не выбрано.
- **Скиллы как отдельный MCP-сервер.** Лишний процесс и протокол внутри своего же процесса;
  harness-инструмент проходит тот же конвейер (разрешения, хуки, события). Возможен позже как
  экспорт.
- **Расширения на верхнем уровне frontmatter (`always: true`).** Удобнее, но валидаторы
  экосистемы отклоняют такие файлы. Не выбрано.
- **Загруженные тела в отдельном блоке контекста с состоянием в `LoopState`.** Нужна логика в
  движке; ответ инструмента в истории даёт то же. Не выбрано.
- **Сканировать рабочую папку.** Скилл чужого репозитория стал бы доверенным текстом. Не выбрано.
- **Скрипты в v1 через `PermissionEngine`.** Требует песочницы, протокола вывода и правил по
  умолчанию, которых проект сознательно не поставляет. Отложено.

## Related

- [Проектный документ](../development/2026-10-26-agent-skills-design.md)
- [Glossary](../glossary.md)
- [ADR-2026-10-02: Layered Agent Memory](2026-10-02-layered-agent-memory.md) — образец слоёв и
  harness-инструментов
- [ADR-2026-10-19: Name-Free Tool Handling](2026-10-19-name-free-tool-handling.md)
- [ADR-2026-09-16: Typed Settings Module](2026-09-16-typed-settings-module.md)
- [ADR-2026-10-01: Permission Engine](2026-10-01-permission-engine.md)
- [ADR-2026-10-19: AutoBrowser Command and Home Directory](2026-10-19-autobrowser-command-and-home-directory.md)
- Код: `src/agent_loop/skills.py` (удаляется), `src/agent_loop/context.py`,
  `src/harness/session.py`, `src/agent_loop/evals.py`, `src/config.py`,
  `src/agent_loop/resources/`
