# ADR-2026-09-30: Command Hooks

Status: Accepted
Date: 2026-09-30

## Context

The [Lifecycle Hooks Engine](2026-09-30-lifecycle-hooks-engine.md) only loads in-process
`async` Python handlers named by `package.module:attr`, and listed `command` hooks as a
follow-up. Claude Code and Codex hooks are external commands instead: the event arrives as
JSON on stdin and the verdict comes back through the exit code and stdout. That lets a
hook be written in any language, kept outside the package and changed without touching
the importable code.

## Decision

- `HookSpec` gets `type: python | command` (default `python`, so existing registries keep
  working) and a `command` string. A `python` spec needs `handler`; a `command` spec needs
  `command` and rejects `handler`/`options`.
- A command spec loads as `CommandHook` (`src/harness/command_hooks.py`), an ordinary
  async handler. Matching, ordering, aggregation, timeouts, `fail_closed` and
  `hook.decided` telemetry are the engine's and do not change.
- **Process**: run through the shell with the repository root as working directory;
  environment is the parent's plus `AUTOBROWSER_PROJECT_DIR` and `AUTOBROWSER_HOOK_EVENT`.
- **Input**: `json.dumps(asdict(HookEvent))`, ASCII-escaped (no console-encoding issues on
  Windows), then EOF. Field names are ours (`name`, `tool`, `server`, `args`, `result`,
  …), not Claude Code's `tool_name`/`tool_input`: browser tools differ anyway, so a Claude
  Code script is not portable as is.
- **Output**:
  - exit `0` — empty stdout is no opinion; a JSON object maps onto `HookResult`
    (`decision`, `reason`, `updated_input`, `updated_output`, `additional_context`,
    `user_message`; `"block"` is an alias of `"deny"`); unknown keys or wrong types are a
    failure; plain text is `additional_context` on `goal_start` and ignored elsewhere;
  - exit `2` — blocking, stderr is the reason: `deny` on decision events; on
    `post_tool_use*` stderr becomes `additional_context` (the tool already ran);
  - any other exit code — a handler failure, like a raising Python handler.
- **Timeout** kills the whole process tree (`taskkill /T` on Windows, the process group
  elsewhere).

## Consequences

- Unlike Claude Code, where other exit codes are non-blocking, a crashing command on
  `goal_start`/`pre_tool_use` denies — the fail-closed default of the engine ADR. A hook
  can opt out with `fail_closed: false`.
- Each run costs a process start (tens of milliseconds for a shell plus an interpreter);
  broad `match` filters on frequent tools multiply that.
- Commands inherit the environment, including `AUTOBROWSER_LLM__API_KEY`, and run with the
  user's rights — the same trust model as a Python handler named in a git-ignored config.
  The registry SHA-256 in `session.json` now covers `type` and `command`.
- The `error` of a failed run carries at most the last 300 characters of stderr; a hook
  must not print secrets there.

## Alternatives Considered

- **Claude Code's input/output schema verbatim** (`hookSpecificOutput`,
  `permissionDecision`, camelCase): two dialects for one contract, and scripts still would
  not port because tool names and arguments differ.
- **An argv list without a shell**: no pipes, redirects or env expansion, which hook
  authors expect from Claude Code and Codex.
- **Non-blocking failures like Claude Code**: would make a broken `url_policy`-style
  command silently allow navigation; the per-hook `fail_closed` switch covers the lenient
  case.

## Related

- [Lifecycle Hooks Engine](2026-09-30-lifecycle-hooks-engine.md)
- [Lifecycle hooks research](../research/2026-09-30-lifecycle-hooks-research.md)
