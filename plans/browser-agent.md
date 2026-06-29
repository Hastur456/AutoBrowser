# Browser Agent — общая архитектура

## Назначение

LangGraph-агент для автоматизации браузера. Соединяет планировщик задач с исполнителем MCP-инструментов через CDP-подключение к Chrome.

## Граф верхнего уровня

```
START → plan → execute → mcp → observe → vision → reflect → (router)
                  ↑                                              |
                  └── continue ──────────────────────────────────┘
                  └── replan      → plan
                  └── retry       → backoff → mcp
                  └── human       → human_input → execute
                  └── done/fatal  → END
```

## Компоненты

| Компонент | Файл | Роль |
|---|---|---|
| `AgentWorkflow` | [src/agent/agent.py](../src/agent/agent.py) | Верхний граф: plan → execute → mcp → observe → vision → reflect |
| `AgentState` | [src/agent/state.py](../src/agent/state.py) | Общее состояние агента |
| `observe_node` | [src/agent/nodes.py](../src/agent/nodes.py) | Снимает снапшот браузера после каждого tool-вызова |
| `vision_node` | [src/agent/nodes.py](../src/agent/nodes.py) | LLM-интерпретация снапшота → `perception` |
| `reflect_node` | [src/agent/nodes.py](../src/agent/nodes.py) | LLM-решение о следующем шаге (continue/replan/retry/done/fatal/human) |
| `reflect_router` | [src/agent/routers.py](../src/agent/routers.py) | Читает `reflection`, возвращает имя следующего узла |
| `PlannerWorkflow` | [src/subgraphs/planner/workflow.py](../src/subgraphs/planner/workflow.py) | Подграф планирования |
| `mcp_invoke_node` | [src/subgraphs/executor/nodes.py](../src/subgraphs/executor/nodes.py) | Исполняет `tool_calls` из последнего сообщения |
| `backoff_node` | [src/subgraphs/executor/nodes.py](../src/subgraphs/executor/nodes.py) | Экспоненциальный backoff перед повторным вызовом |
| `setup_mcp` | [src/mcp/mcp_setup.py](../src/mcp/mcp_setup.py) | Инициализация MCP-клиента |
| `main.py` | [main.py](../main.py) | Точка входа: запуск Chrome + граф |

## Точка входа (`main.py`)

1. Запускает Chrome с флагами CDP (`--remote-debugging-port`, `--user-data-dir`)
2. Polling-ждёт открытия порта (`is_port_open`)
3. `setup_mcp()` → получает список LangChain-инструментов от `@playwright/mcp`
4. Фильтрует до `ALLOWED_TOOLS` (8 инструментов)
5. Инициализирует `ChatOllama(model="gpt-oss:20b-cloud")`
6. Передаёт в `AgentWorkflow` или напрямую в узлы планировщика

## Разрешённые инструменты

```
browser_navigate, browser_snapshot, browser_click,
browser_type, browser_fill_form, browser_wait_for,
browser_tabs, browser_navigate_back
```

## Состояние агента (`AgentState`)

Наследует `ExecutorState`, добавляет:
- `plan_steps: PlanSteps | None` — типизированные шаги из планировщика
- `observation: str | None` — снапшот браузера (из `observe_node`)
- `perception: str | None` — интерпретация снапшота LLM (из `vision_node`)
- `reflection: str | None` — решение о маршруте (из `reflect_node`)
- `replan_count: int` — счётчик переплановок

## Известные проблемы / TODO

- `reflect_node` временно использует `ainvoke` без structured output — `reflection` содержит сырой текст LLM, роутер всегда уходит в END
- Нет продвижения `plan_steps` — после выполнения шага `steps[0]` не удаляется из списка
- Нет тестов для `AgentWorkflow`
- LLM захардкожен в `main.py`, не передаётся через конфиг

## Связанные планы

- [planner.md](planner.md) — детали подграфа планирования
- [executor.md](executor.md) — детали подграфа исполнения
