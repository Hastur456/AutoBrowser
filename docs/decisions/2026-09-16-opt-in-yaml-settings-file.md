# ADR-2026-09-16: Opt-In YAML Settings File

Status: Accepted
Date: 2026-09-16

## Context

[The typed settings module](2026-09-16-typed-settings-module.md) put every tunable in
`src/config.py` behind `AUTOBROWSER_<SECTION>__<FIELD>` names, and explicitly rejected adding
a config file: "the environment and a `.env` file were already the working mechanism".

Two gaps have since shown up in that mechanism:

- Some values are awkward to spell as env vars. A whole `llm:` block, or the reasoning
  budget of the model, reads better as a nested document than as six
  `AUTOBROWSER_LLM__*` lines.
- Sharing a profile between machines means sharing a `.env`, which is also where the API
  key lives. The one file that must never be committed is the one file that carries the
  non-secret tuning.

## Decision

Add a YAML file as a **fourth, opt-in** settings source, slotted between `.env` and the
secret-file source.

- **Explicit activation only.** The file is read when and only when
  `AUTOBROWSER_CONFIG_FILE` names it. There is no working-directory scan, so `config.yaml`
  or `config.local.yaml` sitting in the repository changes nothing until it is named. A
  named-but-missing file raises `FileNotFoundError` rather than quietly falling back to
  the defaults.
- **Precedence** (highest first): `Settings(...)` kwargs → `AUTOBROWSER_*` env vars →
  `.env` → the YAML file → Docker/Kubernetes secret files → field defaults. pydantic-settings
  folds the sources with `deep_update`, so a higher source replaces one field and leaves the
  rest of the file's section standing. That is what lets an env var override `llm.model` for
  a single run without discarding the `llm.temperature` and `llm.reasoning_effort` the file
  set in the same block.
- **YAML only.** No JSON or TOML source. One human-edited format, one parser, one set of
  tests.
- **Typo protection.** `Settings` is `extra="ignore"` so that unrelated keys in `.env`
  cannot break startup; that leniency would turn a mistyped *section* name in the file
  (`loops:` for `loop:`) into silently ignored configuration. The file source therefore
  rejects unknown top-level sections itself, and the sections' `extra="forbid"` rejects
  unknown fields within a known section.
- **Errors name the file.** A malformed document is reported as a `ValueError` carrying the
  path, instead of the bare `yaml.YAMLError` the stock source raises.
- **New reasoning fields.** `llm.reasoning_effort` (`minimal`/`low`/`medium`/`high`),
  `llm.max_output_tokens`, and `llm.max_reasoning_tokens` join `LLMSettings` — the values a
  file source is most useful for. They are declarative for now; see the tradeoffs.

## Consequences

Benefits:

- Profiles can be committed (`config.yaml`) while secrets stay in `.env`, which keeps the
  "never commit this file" rule off the file that everyone shares.
- Large, related changes ("use the reasoning model for the batch") are one edit instead of
  five environment variables.
- Nothing changes for anyone who does not set `AUTOBROWSER_CONFIG_FILE`: the source is
  simply absent from the pipeline.

Tradeoffs and risks:

- **`.env` outranks the file.** The repository's own `.env` pins
  `AUTOBROWSER_LLM__MODEL` and `AUTOBROWSER_BROWSER__CDP_PORT`, so those two fields appear
  to ignore the file until the corresponding `.env` lines are commented out. This is the
  requested precedence and it is documented in `config.example.yaml`, but it is the first
  thing a reader will hit.
- **The reasoning fields change no request yet.** `src/providers/ollama.py` forwards only
  `temperature` (plus the `num_predict`/`num_ctx`/`top_p`/`seed` call params), so these are
  recorded configuration until that adapter is taught the parameters. The field docstrings
  and `.env.example` say so rather than implying they are wired.
- **The path is an env var, not a field.** It has to be resolved before the model that
  would describe it exists, so `AUTOBROWSER_CONFIG_FILE` is deliberately outside the typed
  surface and is not validated by pydantic.
- **PyYAML is now imported by `src/config.py`.** It was already a pinned dependency of the
  project and of `pydantic-settings[yaml]`, so this adds no new package.
- **No configuration profiles** (`development`/`production`). If they are ever needed, they
  belong in a mechanism that makes the choice explicit, not in filename auto-discovery.

## Alternatives Considered

- **Auto-discovery of `config.yaml`, with `config.local.yaml` preferred.** Implemented
  first, then rejected: the behaviour of the process would depend on the presence of a file
  the user may not know exists, which is exactly the reproducibility problem the explicit
  env var avoids. The `--config` flag was rejected alongside it — `build_parser()` in
  `main.py`, `scripts/run_batch.py` and `scripts/export_sessions.py` calls `get_settings()`
  while building argparse defaults, so a flag would need a pre-parse pass in every
  entrypoint to set the variable before that first read.
- **JSON or TOML instead of / alongside YAML.** JSON rejected: YAML covers the
  human-edited-document case, and a second format doubles the parser surface and the tests
  for no new capability. A `.json` path is not rejected by the loader either, but that is
  incidental and untested. TOML remains the reasonable third format if one is ever needed;
  `pydantic-settings` ships a source for it.
- **Per-run overrides via a merge helper** (`load_settings_with_override(path)` deep-merging
  a partial dict over the cached singleton), targeted at `run_batch.py` giving each scenario
  its own budget. Deferred: `Settings(**kwargs)` already covers per-call overrides, and the
  batch runner can set the env var per process instead. Worth revisiting when a run needs
  two different profiles at once.
- **Reusing the stock `YamlConfigSettingsSource` unchanged.** Its parse happens in
  `__init__` and raises an anonymous `yaml.YAMLError`, which is how this landed on a
  subclass hooking `_read_file` rather than `__call__`.

## Related

- [Typed Settings Module](2026-09-16-typed-settings-module.md) — the module this extends;
  its "no config file" alternative is the decision superseded here.
- `src/config.py` — `_resolve_config_path`, `_YamlFileSettingsSource`,
  `Settings.settings_customise_sources`.
- `config.example.yaml` — template and the precedence narrative.
- `.env.example` — every field with its default, held in sync by tests.
- `tests/test_config.py` — activation, precedence, deep merge, and the failure modes.
