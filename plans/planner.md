# Planner — подграф планирования

## Назначение

Принимает пользовательскую задачу, генерирует свободный текстовый план, затем парсит его в типизированные шаги `PlanStep`.

## Граф

```
START → task_decomposition → get_list_of_tools → END
```

## Узлы

### `task_decomposition_node` ([src/subgraphs/planner/nodes.py:11](../src/subgraphs/planner/nodes.py#L11))

- Вход: `state["messages"]` + описание инструментов
- Промпт: `task_decomposition_prompt` (системный) + история сообщений
- Выход: `current_plan` (str) — свободный текст с шагами

### `get_list_of_tools_node` ([src/subgraphs/planner/nodes.py:34](../src/subgraphs/planner/nodes.py#L34))

- Вход: `state["current_plan"]`
- LLM со структурированным выводом: `llm.with_structured_output(PlanSteps, method="json_schema")`
- Retry: `stop_after_attempt=3`
- Выход: `plan_steps: PlanSteps`

## Состояние (`PlannerState`)

| Поле | Тип | Описание |
|---|---|---|
| `messages` | `list[BaseMessage]` | История диалога |
| `current_plan` | `str` | Свободный текст плана |
| `plan_steps` | `PlanSteps` | Распарсенные шаги |

## Типы шагов (`PlanStep`)

Поля: `step_id`, `description`, `action_type`, `estimated_tool`, `is_sensitive`

## Классы

- `PlannerWorkflow` — компилирует и запускает граф
- Методы `task_decomposition` / `get_list_of_tools` — обёртки над узлами с инжекцией `llm` и `tools`

## Известные проблемы / TODO

- `render_text_description(tools)` вызывается в обоих узлах отдельно — можно вынести в состояние
- Промпты (`task_decomposition_prompt`, `get_list_of_tools_prompt`) не версионируются
- `get_list_of_tools_node` передаёт `current_plan` как второй `SystemMessage` — семантически должен быть `HumanMessage` или `AIMessage`

## Связанные планы

- [browser-agent.md](browser-agent.md) — верхний уровень
- [executor.md](executor.md) — потребитель `plan_steps`
