# Search Task Sequence

This diagram shows the intended high-level tool sequence for browser search
tasks. It documents the expected behavior after the prompt changes that limit
repeated Search-button loops.

```mermaid
sequenceDiagram
  participant User
  participant Agent
  participant Browser
  participant Observer

  User->>Agent: Ask for search results
  Agent->>Browser: browser_navigate
  Browser-->>Observer: Page loaded
  Agent->>Browser: browser_snapshot
  Browser-->>Observer: Visible controls and refs
  alt Editable search field is visible
    Agent->>Browser: browser_type query and submit
  else Only search affordance is visible
    Agent->>Browser: browser_click affordance once
    Agent->>Browser: browser_snapshot
    alt Editable search field appears
      Agent->>Browser: browser_type query and submit
    else No progress
      Agent->>Browser: browser_navigate direct search URL
    end
  end
  Agent->>Browser: browser_snapshot or snapshot-based extraction
  Browser-->>Observer: Visible result data
  Agent-->>User: Final answer with result titles and links
```

The agent should not repeat the same Search-button click after an unchanged
snapshot. Direct search URL navigation is the preferred fallback for sites with
a known query URL pattern.
