# Step Viewer — пошаговый вывод действий агента

**Статус**: done  
**Приоритет**: medium

---

## Что добавлено

### `AgentWorkflow.stream()` (`src/agent/agent.py`)

Async generator поверх `graph.astream(stream_mode="updates")`.  
Yield: `(node_name: str, update: dict)` после каждого узла.

```python
async for node_name, update in agent.stream(state):
    ...
```

### `print_step(node_name, update)` (`main.py`)

Форматированный вывод для каждого узла:

| Узел | Что выводится |
|---|---|
| `plan` | Пронумерованные шаги с action_type и описанием |
| `execute` | Последние сообщения + tool_calls с аргументами |
| `mcp` | Результаты вызовов инструментов, тип ошибки |
| `observe` | Первые 200 символов DOM-снапшота |
| `vision` | Perception — интерпретация страницы LLM |
| `reflect` | Итоговое решение (continue / replan / retry / done / fatal / human) |
| `backoff` | Номер попытки повтора |
| `human_input` | Ввод пользователя |

### `run_task()` (`main.py`)

Переключён с `agent.run()` на `agent.stream()`.  
Аккумулирует финальное состояние через `final_update.update(update)`.

---

## Пример вывода

```
┌─ [PLAN] ──────────────────────────────────────────────────────
│  1. [navigate] Открыть habr.com
│  2. [click] Перейти в раздел "Информационная безопасность"
│  3. [click] Открыть первую статью
└──────────────────────────────────────────────────────────────

┌─ [EXECUTE] ───────────────────────────────────────────────────
│  AI: [tool_calls] browser_navigate({'url': 'https://habr.com'})
└──────────────────────────────────────────────────────────────

┌─ [MCP] ────────────────────────────────────────────────────────
│  Navigated to https://habr.com
└──────────────────────────────────────────────────────────────

┌─ [REFLECT] ───────────────────────────────────────────────────
│  decision → continue
└──────────────────────────────────────────────────────────────
```

---

## Изменённые файлы

```
src/agent/agent.py  — метод stream()
main.py             — print_step(), run_task() на стриминге
```
