# ADR-2026-09-26: `config.yaml` at the Repo Root Auto-Loads by Default

Status: Accepted
Date: 2026-09-26
Supersedes: [ADR-2026-09-16: Opt-In YAML Settings File](2026-09-16-opt-in-yaml-settings-file.md)
 (its "explicit activation only" clause)

## Context

[ADR-2026-09-16](2026-09-16-opt-in-yaml-settings-file.md) added the YAML settings file as a
fourth source, active only when `AUTOBROWSER_CONFIG_FILE` names it, with no
working-directory scan. In practice that means a developer's personal tuning (model choice,
Chrome profile path) has nowhere convenient to live: `.env` is shared with the API key and
`AUTOBROWSER_*` exports don't survive a new shell, so the working pattern became a git-ignored
`config.yaml` at the repo root plus remembering to export `AUTOBROWSER_CONFIG_FILE=config.yaml`
every session -- a step nobody actually did, so the file silently had no effect.

`src/config.py` had in fact already grown a `DEFAULT_CONFIG_FILE_YAML = ROOT_PATH /
"config.yaml"` constant for exactly this, but `_resolve_config_path()` wired it backwards: an
unset `AUTOBROWSER_CONFIG_FILE` made the *default* file mandatory (raising `FileNotFoundError`
on a fresh checkout with no `config.yaml`), while a *set* `AUTOBROWSER_CONFIG_FILE` was ignored
in favor of the default path. Neither half matched ADR-2026-09-16's decision or its own
docstring, and was fixed as a bug independently of this ADR -- but fixing it forced the real
question: should `config.yaml` at the repo root auto-load, or should activation stay strictly
opt-in?

## Decision

`config.yaml` at the repo root (`DEFAULT_CONFIG_FILE_YAML`) is now a **default profile**,
auto-loaded when present, distinct from an explicitly named file:

- **`AUTOBROWSER_CONFIG_FILE` still wins when set.** Its file is mandatory: missing means
  `FileNotFoundError`, exactly as ADR-2026-09-16 decided.
- **Unset falls back to `config.yaml` at the repo root, if it exists.** This is a fixed path
  (`Path(__file__).resolve().parent.parent / "config.yaml"`), not a working-directory scan --
  a `config.yaml` anywhere else (a different cwd, a subdirectory) is still never picked up.
  Absence is silent: no error, just no file source, same as before.
- Everything else from ADR-2026-09-16 stands unchanged: precedence (init kwargs > YAML >
  `AUTOBROWSER_*` env > `.env` > secrets), partial-profile deep merging, unknown-section
  rejection, named-file-in-errors.

`config.yaml` and `config.local.yaml` are both git-ignored (see `.gitignore`), so this is safe
as a personal, per-machine default -- it never lands in a commit by accident.

## Consequences

Benefits:

- A developer's local tuning (model, Chrome path, temperature) just works by dropping a
  `config.yaml` next to the code, no env var to remember.
- `AUTOBROWSER_CONFIG_FILE` remains available for a second profile, CI overrides, or naming
  a file outside the repo.

Costs:

- The default is silent by design: a `config.yaml` at the repo root that a developer forgot
  about *will* change what the process does, unlike every other file location. `config.py`'s
  module docstring calls this out explicitly, as does `config.example.yaml`'s header comment.
- `tests/test_config.py`'s `settings` fixture must redirect
  `src.config.DEFAULT_CONFIG_FILE_YAML` to a nonexistent path per test; otherwise a
  contributor's own `config.yaml` would make the suite machine-dependent. Tests that want to
  exercise the default-file path do so by monkeypatching that same constant to a real file.
