# Графы агента AutoBrowser

## Текущий граф (реализован)

```mermaid
flowchart TD

    START([START])

    START --> Plan[plan]

    Plan --> Execute[execute\nLLM.bind_tools → tool_calls]

    Execute --> MCP[mcp\nmcp_invoke_node]

    MCP --> Observe[observe\nbrowser_snapshot]

    Observe --> Vision[vision\nLLM perception]

    Vision --> Reflect[reflect\nLLM decision]

    Reflect -->|continue| Execute

    Reflect -->|replan| Plan

    Reflect -->|retry| Backoff[backoff\nexponential]

    Backoff --> MCP

    Reflect -->|human| Human[human_input]

    Human --> Execute

    Reflect -->|done / fatal / other| END([END])
```

## Известные проблемы

- `reflect_node` временно использует `ainvoke` без structured output — рефлексия всегда заканчивается в END
- Продвижение шагов (`plan_steps[0]` → удалить после выполнения) не реализовано
