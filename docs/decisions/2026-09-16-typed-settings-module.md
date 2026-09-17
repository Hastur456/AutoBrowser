# ADR-2026-09-16: Typed Settings Module

Status: Accepted
Date: 2026-09-16

## Context

Tunables were scattered across the layers they happened to be written in:

- `src/llm.py` — `DEFAULT_OLLAMA_MODEL`.
- `src/contracts.py` — `MAX_REPLANS`, `MAX_CONSECUTIVE_FAILURES`,
  `MAX_SNAPSHOT_RECOVERIES`, `MAX_STEPS_WITHOUT_PLAN_ADVANCE`,
  `MAX_UNCHANGED_SNAPSHOTS`.
- `src/agent_loop/execution/loop.py` — `DEFAULT_TURN_CAP = 50`.
- `src/agent_loop/goals.py` — three phase timeouts.
- `src/agent_loop/events.py`, `src/harness/memory.py`,
  `src/browser/observation.py` — truncation budgets, partly as named
  constants and partly as inline literals (`500`, `400`, `300`, `1200`) for
  the same concept.
- `src/cli/parser.py` — argparse defaults, several read straight from the
  environment.
- Hardcoded `.autobrowser/...` path literals in `src/harness/session.py`.

Environment handling had grown a second, undocumented path. `main.py` called
`load_dotenv()`, which copied `.env` into `os.environ`; the third-party
`ollama` client then picked up `OLLAMA_API_KEY` and `OLLAMA_HOST` from there on
its own. Meanwhile the flat names `PORT`, `CHROME_PATH`, and `USER_DATA_DIR`
were read directly out of the environment by the CLI parser. Nothing tied a
setting to a type, a bound, or a single place — and the same value could
arrive by two different routes.

## Decision

Add `src/config.py` as the single source of truth: a pydantic v2 /
pydantic-settings `Settings` root composed of eight frozen section sub-models
(`llm`, `browser`, `loop`, `observation`, `memory`, `events`, `storage`,
`flags`).

- **Naming.** `AUTOBROWSER_<SECTION>__<FIELD>`, so `browser.cdp_port` is
  `AUTOBROWSER_BROWSER__CDP_PORT`. Environment outranks `.env`; every field
  carries a default.
- **Neutrality.** Like `src/contracts.py`, the module is a leaf: it imports
  nothing from `src/agent_loop/`, `src/harness/`, or `src/browser/`, so every
  layer may depend on it.
- **Call-time reads.** Consumers call `get_settings()` at the point of use
  rather than binding module-level constants at import.
- **One `.env` path.** `load_dotenv()` is removed from `main.py`;
  pydantic-settings reads `.env` itself. `settings.llm.api_key` is now passed
  explicitly to the provider as an `Authorization: Bearer` header instead of
  being left to the client's own environment lookup. The explicit header wins
  over `OLLAMA_API_KEY` in `ollama/_client.py`, verified experimentally.
- **Empty means unset.** `env_ignore_empty=True`, so `AUTOBROWSER_X=` leaves
  the default in place instead of failing validation.
- **No compatibility layer.** The flat names `PORT`, `CHROME_PATH`,
  `USER_DATA_DIR`, `OLLAMA_MODEL`, `OLLAMA_HOST`, `OLLAMA_API_KEY`, and
  `AUTOBROWSER_AGENT_LOOP` are no longer recognised.

## Consequences

Benefits:

- Every tunable is typed, bounded, and documented in one file; a typo in a
  section name fails loudly (`extra="forbid"` on sections).
- The `.env` file is read exactly once, through one mechanism.
- Credentials flow through the same path as every other setting rather than
  through an ambient environment side effect.
- `.env.example` documents all 28 fields and is held in sync with the model by
  tests that fail if a field is missing, duplicated, or documented with a
  default the code no longer has.

Tradeoffs and risks:

- Renaming the environment variables is a **breaking change** for any existing
  `.env`, shell export, or external tooling. The repository `.env` was migrated
  in place.
- `AUTOBROWSER_LLM__HOST` left unset means `host=None` reaches the client,
  which then still falls back to its own `OLLAMA_HOST` lookup. This is the one
  remaining path by which the process environment can influence behaviour
  outside `src/config.py`.
- The modules below the config layer must keep reading settings at call time.
  Reintroducing a module-level `SOME_LIMIT = get_settings().loop.x` would make
  `reload_settings()` meaningless.
- Sections must stay free of dependencies on loop, harness, or browser types
  to preserve the leaf boundary.

## Alternatives Considered

- **Keep the flat names working** through a custom `LegacyEnvSource` below the
  env and `.env` sources. Implemented first, then rejected: it preserved two
  parallel naming schemes indefinitely and kept a third-party client's variable
  names inside our configuration surface.
- **`validation_alias` / `AliasChoices` on nested fields**, the documented
  pydantic approach for alternate names. Rejected after a minimal repro showed
  aliases on fields inside nested sub-models fail to resolve even when given
  the exact canonical name.
- **Module-level constants** reassigned from settings on import. Rejected: it
  restores the original problem of values frozen at import time, and makes
  `reload_settings()` a lie.
- **A separate config file format** (TOML/YAML, as floated in early research).
  Rejected: the environment and a `.env` file were already the working
  mechanism, and pydantic-settings handles both without a new parser.

## Related

- `src/config.py` — the module itself; its docstring is the field reference.
- [2026-09-16 Opt-In YAML Settings File](2026-09-16-opt-in-yaml-settings-file.md)
  — adds a YAML file source that outranks `.env` and the environment as a partial
  profile, plus the reasoning-budget fields; its "no config file format"
  alternative above is the decision that ADR revisits.
- `.env.example` — generated reference for every setting and its default.
- `tests/test_config.py` — default parity, resolution, bounds, and
  `.env.example` drift checks.
- [2026-09-03 Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md)
  — removed the `langsmith.py` module whose environment variables were the
  last piece of dead configuration.
- [2026-08-31 Native Agent Loop Engine](2026-08-31-native-agent-loop-engine.md)
  — the engine-native runtime whose constants moved into `settings.loop`.
