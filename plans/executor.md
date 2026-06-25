# Executor — подграф исполнения

## Назначение

Вызывает MCP-инструменты из `tool_calls` последнего сообщения. Обрабатывает ошибки с экспоненциальным backoff и классификацией на `fatal` / `retryable`.

## Граф

```
START → mcp → (retry_router) → backoff → mcp → ...
                    ↓
                  abort → END
```

## Узлы

### `mcp_invoke_node` ([src/subgraphs/executor/nodes.py:13](../src/subgraphs/executor/nodes.py#L13))

- Читает `tool_calls` из последнего сообщения
- Строит `tool_registry = {t.name: t for t in tools}`
- Для каждого вызова:
  - Инструмент не найден → `fatal_error = True`
  - `TimeoutError` / `ConnectionError` / `RuntimeError` → `retryable`
  - Успех → `ToolMessage` с результатом
- Возвращает обновлённые счётчики: `error_count`, `retry_attempts`, `total_tool_calls`, `last_error_type`

### `backoff_node` ([src/subgraphs/executor/nodes.py:118](../src/subgraphs/executor/nodes.py#L118))

- Задержка: `min(2^attempt + jitter(0..0.5), 10)` секунд
- Не изменяет состояние, только ждёт

## Маршрутизатор (`retry_router`) ([src/subgraphs/executor/routers.py:4](../src/subgraphs/executor/routers.py#L4))

| `last_error_type` | `retry_attempts` | Маршрут |
|---|---|---|
| `"fatal"` | любое | `abort` → END |
| `"retryable"` | `< max_retries` | `backoff` |
| `"retryable"` | `>= max_retries` | `abort` → END |
| `None` | любое | `abort` → END |

## Состояние (`ExecutorState`)

| Поле | Тип | Описание |
|---|---|---|
| `messages` | `list[BaseMessage]` | История + ToolMessage-ответы |
| `error_count` | `int` | Суммарное число ошибок |
| `retry_attempts` | `int` | Число попыток текущей серии |
| `total_tool_calls` | `int` | Всего вызовов инструментов |
| `last_error_type` | `str \| None` | `"fatal"`, `"retryable"` или `None` |
| `last_action` | `ToolMessage \| None` | Последний ответ инструмента |

## Известные проблемы / TODO

- `max_retries=3` захардкожен по умолчанию, не пробрасывается из `main.py`

## Связанные планы

- [browser-agent.md](browser-agent.md) — верхний уровень
- [planner.md](planner.md) — поставщик `plan_steps`
