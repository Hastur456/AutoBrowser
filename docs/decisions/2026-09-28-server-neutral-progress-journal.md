# ADR-2026-09-28: Server-Neutral Progress Journal and Honest Completion

Status: Accepted
Date: 2026-09-28

## Context

After the [MCP Manager migration](2026-09-28-universal-mcp-manager.md) the loop lost its
progress signal: "ineffective action" detection compared browser snapshot fingerprints and
relied on browser-specific result parsing that had moved out of the tool path. A traced run
looped `browser_evaluate → [] → browser_snapshot → browser_evaluate …` until the model
declared `done` without an answer. Memory compaction also dropped the short results the model
needed to notice the repeat. Details:
[agent-loop progress recovery](../development/2026-09-26-agent-loop-progress-recovery.md).

## Decision

- **Action journal** (`src/agent_loop/execution/progress.py`, a pure leaf): every executed
  call becomes an `ActionRecord` keyed by `args_key` (tool name + canonical JSON args) and
  `outcome_key` (status + normalized result text), with an `occurrence` count per task. No
  tool, server or result format is special-cased. The journal lives on `LoopState`.
- **Repeat signal, not a script.** The observation notes a detected repeat, and
  `ContextAssembler` renders an `Action History` block
  (`observation.action_history_limit`, `observation.action_history_preview_chars`).
  Policy blocks a further identical call once it has returned the same outcome
  `loop.max_ineffective_actions` times, forcing a replan.
- **Read-only tools come from MCP annotations.** `tool_is_read_only` (`src/harness/tools.py`)
  uses `readOnlyHint` rather than a list of tool names to judge snapshot freshness.
- **Memory keeps evidence.** Older tool outputs are compacted only when longer than
  `memory.compact_tool_output_min_chars` and superseded by a newer result of the same tool.
- **Honest completion.** `CompletionController` maps `{"decision":"blocked"}` and
  `stop(failed)` to `blocked`, `stop(cancelled)` to `cancelled`; only JSON `done` or a plain
  text answer gives `done`.
- Prompts no longer prescribe a fixed snapshot/evaluate cycle.

## Consequences

- Loops end as `blocked` within a few turns instead of hitting `turn_cap` or a false `done`.
- `loop.max_ineffective_actions` changed meaning (identical call + identical outcome, not
  "unchanged snapshot"); three settings were added to `src/config.py`, `.env.example` and
  `tests/test_config.py`. `tests/evals/baselines/agent_loop_v1.json` was refreshed.
- Legitimate polling of an unchanged page is limited by the same budget.

## Alternatives Considered

- Per-tool counters on `LoopState` (e.g. `evaluate_empty_count`): hardcodes tools into state.
- Restore snapshot-fingerprint detection: browser-specific and blind to non-browser servers.

## Related

- [Glossary](../glossary.md)
- [ADR-2026-08-31: Native Agent Loop Engine](2026-08-31-native-agent-loop-engine.md)
- [Agent Runtime Flow](../diagrams/agent-runtime-flow.md)
- Code: `src/agent_loop/execution/progress.py`, `guards.py`, `policy.py`, `observation.py`,
  `src/harness/memory.py`
