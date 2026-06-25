# Задача: Новый граф агента с Observe + Reflection

**Цель**: Расширить `AgentWorkflow` — добавить узлы `observe` и `reflect` между исполнением и принятием решения о следующем шаге. Reflection заменяет текущий `_should_retry` и становится единственной точкой маршрутизации.

**Приоритет**: high  
**Статус**: done

---

## Целевой граф

```
START → plan → execute → mcp → observe → reflect → (router)
                 ↑                           |
                 |── replan ─────────────────┘
                 |── continue ──→ execute
                 |── retry ────→ backoff → mcp
                 |── done ─────→ END
                 └── fatal ────→ END
```

---

## Что меняется относительно текущего кода

| Было | Будет |
|---|---|
| `_should_retry` в `AgentWorkflow` — роутер только по `last_error_type` | `reflect_node` — LLM-узел, читает DOM/tool output и решает маршрут |
| `execute` вызывает `ExecutorWorkflow` как чёрный ящик | `mcp` и `observe` — отдельные узлы верхнего графа |
| Нет наблюдения за состоянием браузера | `observe_node` снимает снапшот (`browser_snapshot`) после каждого tool-вызова |
| Replan невозможен — planner вызывается один раз | `reflect` может вернуть управление в `plan` |

---

## Шаги реализации

- [x] **1. Расширить `AgentState`**
  - Добавить поля: `observation: str | None`, `reflection: str | None`, `replan_count: int`
  - Файл: [src/agent/state.py](../src/agent/state.py)

- [x] **2. Написать `observe_node`**
  - Вызывает `browser_snapshot` через MCP
  - Сохраняет результат в `state["observation"]`
  - Файл: `src/agent/nodes.py` (новый)

- [x] **3. Написать `reflect_node`**
  - Вход: `messages`, `observation`, `plan_steps`, `last_error_type`, `retry_attempts`
  - LLM с structured output: `ReflectDecision(action: Literal["continue", "replan", "retry", "done", "fatal"])`
  - Сохраняет решение в `state["reflection"]`
  - Файл: `src/agent/nodes.py`

- [x] **4. Написать `reflect_router`**
  - Читает `state["reflection"]`
  - Возвращает: `"execute"` / `"plan"` / `"backoff"` / `END`
  - Файл: `src/agent/routers.py` (новый)

- [x] **5. Переписать `AgentWorkflow._build_graph`**
  - Добавить узлы: `observe`, `reflect`, `backoff`
  - Убрать `_should_retry`
  - Рёбра:
    ```
    START → plan → execute → mcp → observe → reflect
    reflect --conditional--> reflect_router
    reflect_router: continue → execute
                    replan   → plan
                    retry    → backoff → mcp
                    done     → END
                    fatal    → END
    ```
  - Файл: [src/agent/agent.py](../src/agent/agent.py)

- [x] **6. Промпт для `reflect_node`**
  - Системный промпт: контекст задачи + снапшот + история ошибок → решение
  - Файл: `src/agent/prompts.py` (новый)

- [x] **7. Обновить `main.py`**
  - Убрать прямой вызов узлов планировщика
  - Передавать `AgentWorkflow` начальное состояние с `HumanMessage`
  - Добавлен CLI: `--task`, `--loop`, интерактивный ввод

- [x] **8. Написать тесты**
  - `tests/test_reflect_router.py` — юнит-тест маршрутизатора
  - `tests/test_observe_node.py` — мок `browser_snapshot`

---

## Новые файлы

```
src/agent/nodes.py       — observe_node, reflect_node
src/agent/routers.py     — reflect_router
src/agent/prompts.py     — reflect_prompt
```

## Изменяемые файлы

```
src/agent/state.py       — новые поля AgentState
src/agent/agent.py       — новый граф _build_graph
main.py                  — использует AgentWorkflow напрямую
```

---

## Ограничения / риски

- `observe_node` добавляет один `browser_snapshot` после каждого tool-вызова — увеличивает латентность
- `replan_count` нужно ограничить (например, `max_replans=2`) чтобы избежать бесконечного цикла plan → reflect → replan
- `reflect_node` требует LLM-вызов на каждой итерации — модель должна поддерживать structured output с `json_schema`

---

## Связанные планы

- [browser-agent.md](browser-agent.md)
- [planner.md](planner.md)
- [executor.md](executor.md)
