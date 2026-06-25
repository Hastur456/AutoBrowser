# Browser Agent — общая архитектура

## Назначение

LangGraph-агент для автоматизации браузера. Соединяет планировщик задач с исполнителем MCP-инструментов через CDP-подключение к Chrome.

## Граф верхнего уровня

```
START → plan → execute → END
                ↑           ↓
                └── retry ──┘ (retryable error, attempts < max_retries)
```

## Компоненты

| Компонент | Файл | Роль |
|---|---|---|
| `AgentWorkflow` | [src/agent/agent.py](../src/agent/agent.py) | Верхний граф, компонует planner + executor |
| `AgentState` | [src/agent/state.py](../src/agent/state.py) | Общее состояние агента |
| `PlannerWorkflow` | [src/subgraphs/planner/workflow.py](../src/subgraphs/planner/workflow.py) | Подграф планирования |
| `ExecutorWorkflow` | [src/subgraphs/executor/workflow.py](../src/subgraphs/executor/workflow.py) | Подграф исполнения |
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
- `plan_steps` — типизированные шаги из планировщика

## Известные проблемы / TODO

- `main.py` вызывает узлы планировщика напрямую, минуя `AgentWorkflow` — временный обходной путь
- Нет тестов для `AgentWorkflow` и `ExecutorWorkflow`
- LLM захардкожен в `main.py`, не передаётся через конфиг

## Связанные планы

- [planner.md](planner.md) — детали подграфа планирования
- [executor.md](executor.md) — детали подграфа исполнения
