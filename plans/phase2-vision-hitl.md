# Фаза 2: Vision/Perception + Human-in-the-loop

**Цель**: Дополнить граф агента двумя недостающими узлами из целевой схемы.  
**Статус**: done  
**Приоритет**: high

---

## Текущий граф (фаза 1 — done)

```
START → plan → execute → mcp → observe → reflect → (router)
                 ↑                                      |
                 └── continue / replan / retry / done ──┘
```

## Целевой граф (фаза 2)

```
START → plan → execute → mcp → observe → vision → reflect → (router)
                 ↑                                               |
                 └── continue ───────────────────────────────────┘
                 └── replan  → plan
                 └── retry   → backoff → mcp
                 └── done    → END
                 └── fatal   → END
                 └── human   → human_input → execute
```

---

## Что добавляется

| Узел | Роль | Вход | Выход |
|---|---|---|---|
| `vision_node` | Анализирует снапшот (текст + скриншот) LLM-вызовом с vision | `observation` (DOM-текст) | `perception: str` — краткое описание текущего состояния страницы |
| `human_input_node` | Приостанавливает граф, запрашивает подтверждение у пользователя | `plan_steps[current]`, `perception` | `messages` — ответ пользователя как `HumanMessage` |

`reflect_node` получает `perception` вместо сырого `observation` — это сокращает контекст и повышает точность решения.

---

## Шаги реализации

- [ ] **1. Расширить `AgentState`**
  - Добавить поле: `perception: str | None`
  - Файл: `src/agent/state.py`

- [ ] **2. Написать `vision_node`**
  - Вход: `state["observation"]` (DOM-текст из `browser_snapshot`)
  - Вызов: `llm.ainvoke([SystemMessage(VISION_PROMPT), HumanMessage(observation)])`
  - Выход: `{"perception": <краткое описание состояния страницы>}`
  - Файл: `src/agent/nodes.py`
  - Промпт (`VISION_PROMPT`) добавить в `src/agent/prompts.py`:
    > "Ты — Vision модуль. Получаешь DOM-снапшот страницы. Опиши в 2-3 предложениях: что сейчас на странице, какие интерактивные элементы видны, выполнена ли целевая задача."

- [ ] **3. Написать `human_input_node`**
  - Печатает текущий шаг и `perception` в консоль
  - Читает ввод пользователя (`input()` через `asyncio.get_event_loop().run_in_executor`)
  - Возвращает `{"messages": [HumanMessage(content=user_input)]}`
  - Файл: `src/agent/nodes.py`

- [ ] **4. Обновить `reflect_node`**
  - Заменить `observation` на `perception` в контексте LLM-сообщения
  - Добавить `"human"` в `Literal` у `ReflectDecision.action`
  - Файл: `src/agent/nodes.py`

- [ ] **5. Обновить `reflect_router`**
  - Добавить ветку: `action == "human"` → `"human_input"`
  - Файл: `src/agent/routers.py`

- [ ] **6. Обновить `AgentWorkflow._build_graph`**
  - Добавить узлы: `vision`, `human_input`
  - Изменить рёбра:
    ```
    observe → vision          (было: observe → reflect)
    vision  → reflect
    human_input → execute
    ```
  - Добавить `"human_input"` в `add_conditional_edges` словарь
  - Файл: `src/agent/agent.py`

- [ ] **7. Обновить `AgentWorkflow.__init__` и `__doc__`**
  - Обновить docstring с новым графом
  - Добавить метод `human_input(state) → await human_input_node(state)`

---

## Изменяемые файлы

```
src/agent/state.py    — новое поле perception
src/agent/nodes.py    — vision_node, human_input_node; обновить reflect_node
src/agent/routers.py  — ветка "human"
src/agent/prompts.py  — VISION_PROMPT
src/agent/agent.py    — новые узлы и рёбра
```

---

## Ограничения / решения

- **`human_input_node` блокирует event loop** — использовать `loop.run_in_executor(None, input, prompt)` чтобы не блокировать asyncio.
- **`is_sensitive` уже есть в `PlanStep`** — `reflect_node` должен проверять текущий шаг и принудительно возвращать `"human"` если `is_sensitive=True`, не дожидаясь LLM-решения.
- **Vision увеличивает латентность** — промпт минимальный (2-3 предложения), structured output не нужен, достаточно plain text.
- **`replan_count` ограничен `MAX_REPLANS=2`** — аналогично ввести `MAX_HUMAN_INPUTS` (по умолчанию 5) в `routers.py`.

---

## Связанные файлы

- [current.md](current.md) — фаза 1 (done)
- [browser-agent.md](browser-agent.md)
- [executor.md](executor.md)
