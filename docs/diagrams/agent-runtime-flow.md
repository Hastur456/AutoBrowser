# Agent Runtime Flow

This diagram shows the verified LangGraph node flow assembled by
`build_agent_graph`.

```mermaid
flowchart TD
  Start([START]) --> Plan[plan]
  Plan --> Agent[agent]
  Agent -->|tool_call| Policy[policy]
  Agent -->|replan| Plan
  Agent -->|done| End([END])
  Policy -->|approved| Executor[executor]
  Policy -->|needs_human| Human[human_input]
  Policy -->|blocked| Agent
  Human -->|approved| Executor
  Human -->|rejected or revised| Agent
  Executor --> Observe[observe]
  Observe --> Agent
```

The graph keeps planning, reasoning, policy, execution, and observation as
separate responsibilities. Runtime infrastructure is injected by the harness
when the graph is compiled.
