"""Tests for the central settings module (:mod:`src.config`).

Covers four things:

1. **Parity** — the configured defaults still equal the module constants they
   replaced, so extracting them was behaviour-preserving.
2. **Resolution** — canonical ``AUTOBROWSER_<SECTION>__<FIELD>`` names, the
   legacy flat names, and their precedence.
3. **Validation** — bounds, the phase-timeout clamp, and unknown-key rejection.
4. **The YAML file source** — opt-in activation, its priority against the
   environment and ``.env``, deep per-field merging, and its failure modes.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.config import (
    CONFIG_FILE_ENV_VAR,
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    Settings,
    get_settings,
)
from src.providers.ollama import OllamaChatModel


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    """Keep env-driven overrides from leaking between tests."""

    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Return a factory building ``Settings`` from a controlled environment.

    ``chdir`` into an empty directory hides the repository ``.env``, and any
    ambient ``AUTOBROWSER_*`` variable is cleared, so each call starts from the
    hard-coded defaults unless the test sets something.
    """

    monkeypatch.chdir(tmp_path)
    for name in [key for key in os.environ if key.startswith("AUTOBROWSER_")]:
        monkeypatch.delenv(name, raising=False)

    def build(**overrides: Any) -> Settings:
        return Settings(**overrides)

    return build


# --------------------------------------------------------------------------
# Parity with the constants this module replaced
# --------------------------------------------------------------------------


def test_defaults_match_the_constants_they_replaced(settings: Any) -> None:
    """Parity guard: these defaults were module constants before the extraction.

    The ``was:`` comments name the constant each assertion pins, so a future
    change to a default is a deliberate act rather than a silent drift.
    """

    config = settings()

    # was: src/llm.py DEFAULT_OLLAMA_MODEL
    assert config.llm.model == "gpt-oss:20b-cloud"

    # was: src/agent_loop/execution/loop.py DEFAULT_TURN_CAP
    assert config.loop.turn_cap == 50

    # was: src/contracts.py control-loop thresholds
    assert config.loop.max_replans == 3
    assert config.loop.max_consecutive_failures == 3
    assert config.loop.max_snapshot_recoveries == 1
    assert config.loop.max_steps_without_plan_advance == 8
    assert config.loop.max_unchanged_snapshots == 3
    assert config.loop.max_ineffective_actions == 3

    # was: src/agent_loop/goals.py phase timeouts
    assert config.loop.task_timeout_seconds == 300.0
    assert config.loop.progress_timeout_seconds == 120.0
    assert config.loop.latest_state_timeout_seconds == 15.0

    # was: src/browser/observation.py
    assert config.observation.max_content_preview_chars == 1200
    assert config.observation.max_refs_in_observation == 25

    # was: src/harness/memory.py
    assert config.memory.max_tool_message_refs == 25

    # was: src/agent_loop/events.py
    assert config.events.max_string_chars == 20_000
    assert config.events.agent_trace_max_text_chars == 500

    # was: src/cli/parser.py
    assert config.browser.cdp_port == 9222
    assert config.browser.cdp_timeout_seconds == 30.0
    assert config.browser.chrome_path == Path(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    assert config.browser.user_data_dir == Path(r"C:\temp\chrome_debug_profile")

    # was: the hardcoded ".autobrowser" storage literals
    assert config.storage.sessions_dir == Path(".autobrowser") / "sessions"
    assert config.storage.batches_dir == Path(".autobrowser") / "batches"


def test_storage_derives_per_session_paths(settings: Any) -> None:
    storage = settings().storage

    assert storage.session_dir("session-1") == Path(".autobrowser/sessions/session-1")
    assert storage.workspace_dir("session-1") == Path(
        ".autobrowser/sessions/session-1/workspace"
    )


# --------------------------------------------------------------------------
# Environment resolution
# --------------------------------------------------------------------------


def test_canonical_nested_env_names_resolve(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOBROWSER_LOOP__TURN_CAP", "17")
    monkeypatch.setenv("AUTOBROWSER_LLM__TEMPERATURE", "0.7")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__CDP_PORT", "9333")

    config = settings()

    assert config.loop.turn_cap == 17
    assert config.llm.temperature == 0.7
    assert config.browser.cdp_port == 9333


def test_nested_env_names_resolve_every_browser_and_llm_field(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOBROWSER_BROWSER__CDP_PORT", "9444")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__CHROME_PATH", "C:/other/chrome.exe")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__USER_DATA_DIR", "C:/other/profile")
    monkeypatch.setenv("AUTOBROWSER_LLM__HOST", "http://example.invalid:11434")

    config = settings()

    assert config.browser.cdp_port == 9444
    assert config.browser.chrome_path == Path("C:/other/chrome.exe")
    assert config.browser.user_data_dir == Path("C:/other/profile")
    assert config.llm.host == "http://example.invalid:11434"


def test_section_less_names_are_not_accepted(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flat name must not silently configure a nested field."""

    monkeypatch.setenv("AUTOBROWSER_CDP_PORT", "9999")
    monkeypatch.setenv("AUTOBROWSER_LLM_MODEL", "flat-model")
    monkeypatch.setenv("AUTOBROWSER_AGENT_LOOP", "true")

    config = settings()

    assert config.browser.cdp_port == 9222
    assert config.llm.model == "gpt-oss:20b-cloud"
    assert config.flags.agent_loop is False


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_agent_loop_flag_truthy_values(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("AUTOBROWSER_FLAGS__AGENT_LOOP", raw)

    assert settings().flags.agent_loop is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_agent_loop_flag_falsy_values(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """An empty value means "not set", so the field keeps its default."""

    monkeypatch.setenv("AUTOBROWSER_FLAGS__AGENT_LOOP", raw)

    assert settings().flags.agent_loop is False


def test_a_custom_env_file_is_read(settings: Any, tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "AUTOBROWSER_BROWSER__CDP_PORT=9444\nAUTOBROWSER_LLM__MODEL=from-file\n",
        encoding="utf-8",
    )

    config = settings(_env_file=str(env_file))

    assert config.browser.cdp_port == 9444
    assert config.llm.model == "from-file"


def test_the_discovered_env_file_is_read(settings: Any, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AUTOBROWSER_BROWSER__USER_DATA_DIR=C:/from-file\n",
        encoding="utf-8",
    )

    assert settings().browser.user_data_dir == Path("C:/from-file")


def test_a_discovered_env_file_can_be_disabled(settings: Any, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AUTOBROWSER_BROWSER__USER_DATA_DIR=C:/from-file\n",
        encoding="utf-8",
    )

    config = settings(_env_file=None)

    assert config.browser.user_data_dir == Path(r"C:\temp\chrome_debug_profile")


def test_empty_env_values_fall_back_to_the_default(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AUTOBROWSER_X=`` means "not set"; it must not crash on a bool field."""

    monkeypatch.setenv("AUTOBROWSER_FLAGS__AGENT_LOOP", "")
    monkeypatch.setenv("AUTOBROWSER_LLM__API_KEY", "")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__CDP_PORT", "")

    config = settings()

    assert config.flags.agent_loop is False
    assert config.llm.api_key is None
    assert config.browser.cdp_port == 9222


def test_secret_values_are_not_plain_strings(settings: Any) -> None:
    config = settings(llm={"api_key": "super-secret"})

    assert config.llm.api_key is not None
    assert "super-secret" not in repr(config.llm.api_key)
    assert config.llm.api_key.get_secret_value() == "super-secret"


def test_configured_api_key_reaches_the_provider_client(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is wired explicitly, not left to the client's own env lookup."""

    monkeypatch.setenv("AUTOBROWSER_LLM__API_KEY", "super-secret")
    get_settings.cache_clear()

    model = OllamaChatModel()

    assert model._client._client.headers["authorization"] == "Bearer super-secret"


def test_without_a_key_the_client_keeps_its_own_discovery(settings: Any) -> None:
    """A local daemon must not receive a bogus ``Bearer`` header."""

    get_settings.cache_clear()

    model = OllamaChatModel()

    assert "authorization" not in model._client._client.headers


# --------------------------------------------------------------------------
# The YAML file source
# --------------------------------------------------------------------------


def _write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    """Write a YAML settings file, dedented, and return its path."""

    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _point_at(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
) -> Path:
    """Activate the file source for ``path`` and return it."""

    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))
    return path


def test_a_config_file_named_by_the_env_var_is_read(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _point_at(
        monkeypatch,
        _write_config(tmp_path, "loop:\n  turn_cap: 17\nbrowser:\n  cdp_port: 9333\n"),
    )

    config = settings()

    assert config.loop.turn_cap == 17
    assert config.browser.cdp_port == 9333


def test_a_config_file_can_supply_the_api_key_as_a_secret(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A nested ``SecretStr`` from a file source stays a secret."""

    _point_at(monkeypatch, _write_config(tmp_path, "llm:\n  api_key: sk-from-file\n"))

    config = settings()

    assert config.llm.api_key is not None
    assert "sk-from-file" not in repr(config.llm.api_key)
    assert config.llm.api_key.get_secret_value() == "sk-from-file"


def test_the_config_file_outranks_the_environment(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A named file is a profile, not a fallback: the fields it names win."""

    _point_at(monkeypatch, _write_config(tmp_path, "llm:\n  model: from-file\n"))
    monkeypatch.setenv("AUTOBROWSER_LLM__MODEL", "from-env")

    assert settings().llm.model == "from-file"


def test_a_partial_config_file_leaves_the_other_fields_to_the_sources_below(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Naming one field must not freeze its neighbours.

    The file decides ``llm.model`` and nothing else, so the rest of the ``llm``
    section still comes from the environment and every untouched section from
    ``.env`` or the defaults.
    """

    _point_at(monkeypatch, _write_config(tmp_path, "llm:\n  model: from-file\n"))
    monkeypatch.setenv("AUTOBROWSER_LLM__TEMPERATURE", "0.5")
    monkeypatch.setenv("AUTOBROWSER_LOOP__TURN_CAP", "11")
    (tmp_path / ".env").write_text(
        "AUTOBROWSER_LLM__REASONING_EFFORT=high\n",
        encoding="utf-8",
    )

    config = settings()

    assert config.llm.model == "from-file"
    assert config.llm.temperature == 0.5
    assert config.llm.reasoning_effort == "high"
    assert config.loop.turn_cap == 11
    assert config.browser.cdp_port == 9222


def test_the_config_file_outranks_dotenv(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The file sits above both env sources, so it beats a pinned value.

    The repository ``.env`` is the realistic case: it pins ``llm.model`` and
    ``browser.cdp_port``, and a profile is expected to be able to override them.
    """

    (tmp_path / ".env").write_text(
        "AUTOBROWSER_LOOP__TURN_CAP=11\nAUTOBROWSER_LOOP__MAX_REPLANS=9\n",
        encoding="utf-8",
    )
    _point_at(monkeypatch, _write_config(tmp_path, "loop:\n  turn_cap: 7\n"))

    config = settings()

    assert config.loop.turn_cap == 7
    assert config.loop.max_replans == 9


def test_init_kwargs_outrank_the_config_file_and_the_environment(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _point_at(monkeypatch, _write_config(tmp_path, "loop:\n  turn_cap: 7\n"))
    monkeypatch.setenv("AUTOBROWSER_LOOP__TURN_CAP", "11")

    assert settings(loop={"turn_cap": 5}).loop.turn_cap == 5


def test_without_the_env_var_no_config_file_is_discovered(
    settings: Any,
    tmp_path: Path,
) -> None:
    """There is no working-directory scan, by design.

    A file the user forgot about must not be able to change what the process
    does, so ``config.yaml`` and ``config.local.yaml`` sitting right there are
    ignored until named explicitly.
    """

    (tmp_path / "config.yaml").write_text("loop:\n  turn_cap: 7\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text(
        "loop:\n  turn_cap: 9\n",
        encoding="utf-8",
    )

    assert settings().loop.turn_cap == 50


def test_a_missing_config_file_fails_loudly(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Silently falling back to the defaults would hide a typo in the path."""

    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(tmp_path / "absent.yaml"))

    with pytest.raises(FileNotFoundError, match="absent.yaml"):
        settings()


def test_a_malformed_config_file_names_the_file(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _point_at(
        monkeypatch,
        _write_config(tmp_path, "loop:\n  turn_cap: [7\n"),
    )

    with pytest.raises(ValueError) as excinfo:
        settings()

    assert str(path) in str(excinfo.value)


def test_a_config_file_that_is_not_a_mapping_is_rejected(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _point_at(monkeypatch, _write_config(tmp_path, "- loop\n- llm\n"))

    with pytest.raises(ValueError, match="mapping"):
        settings()


def test_an_unknown_section_in_the_config_file_is_rejected(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``extra="ignore"`` on the root would otherwise swallow a mistyped section."""

    _point_at(monkeypatch, _write_config(tmp_path, "loops:\n  turn_cap: 7\n"))

    with pytest.raises(ValueError) as excinfo:
        settings()

    assert "loops" in str(excinfo.value)


def test_an_unknown_field_in_the_config_file_is_rejected(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``extra="forbid"`` on the sections, inherited from the model."""

    _point_at(monkeypatch, _write_config(tmp_path, "loop:\n  turn_capp: 7\n"))

    with pytest.raises(ValueError):
        settings()


def test_config_file_values_are_still_bounded(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The file goes through the same model validation as everything else."""

    _point_at(monkeypatch, _write_config(tmp_path, "llm:\n  temperature: 5\n"))

    with pytest.raises(ValueError):
        settings()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("AUTOBROWSER_LOOP__TURN_CAP", "0"),
        ("AUTOBROWSER_LOOP__TURN_CAP", "1001"),
        ("AUTOBROWSER_LLM__TEMPERATURE", "5"),
        ("AUTOBROWSER_BROWSER__CDP_PORT", "0"),
        ("AUTOBROWSER_BROWSER__CDP_PORT", "99999"),
        ("AUTOBROWSER_OBSERVATION__MAX_REFS_IN_OBSERVATION", "0"),
    ],
)
def test_out_of_range_values_are_rejected(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ValueError):
        settings()


def test_phase_timeouts_are_clamped_to_the_task_budget(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    monkeypatch.setenv("AUTOBROWSER_LOOP__TASK_TIMEOUT_SECONDS", "5")

    config = settings()

    assert config.loop.task_timeout_seconds == 5.0
    assert config.loop.progress_timeout_seconds == 5.0
    assert config.loop.latest_state_timeout_seconds == 5.0


def test_unknown_keys_are_rejected(settings: Any) -> None:
    with pytest.raises(ValueError):
        settings(loop={"turn_capp": 5})


def test_settings_are_immutable(settings: Any) -> None:
    config = settings()

    with pytest.raises(ValueError):
        config.loop.turn_cap = 99


def test_get_settings_is_cached(settings: Any) -> None:
    assert get_settings() is get_settings()


# --------------------------------------------------------------------------
# The example files stay in sync with the model
# --------------------------------------------------------------------------

EXAMPLE_ENV = Path(__file__).resolve().parents[1] / ".env.example"
EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: Matches both a live ``AUTOBROWSER_X=v`` line and a commented ``# AUTOBROWSER_X=v``.
_EXAMPLE_LINE = re.compile(
    r"^(?:#\s*)?(?P<name>AUTOBROWSER_[A-Z0-9_]*(?:__[A-Z0-9_]*)?)\s*=(?P<value>.*)$"
)


def _canonical_env_names() -> set[str]:
    """Derive the expected env var name for every field of every section."""

    names: set[str] = set()
    for section, field in Settings.model_fields.items():
        for name in field.annotation.model_fields:
            names.add(
                f"{ENV_PREFIX}{section.upper()}{ENV_NESTED_DELIMITER}{name.upper()}"
            )
    return names


def _example_lines() -> list[tuple[str, str]]:
    """Return ``(name, value)`` for every setting documented in the template."""

    matches = []
    for line in EXAMPLE_ENV.read_text(encoding="utf-8").splitlines():
        match = _EXAMPLE_LINE.match(line)
        if match:
            matches.append((match.group("name"), match.group("value")))
    return matches


def test_env_example_documents_every_setting() -> None:
    documented = {name for name, _ in _example_lines()}

    assert documented == _canonical_env_names()


def test_env_example_documents_each_setting_once() -> None:
    names = [name for name, _ in _example_lines()]

    assert len(names) == len(set(names))


def test_env_example_values_are_the_actual_defaults(
    settings: Any,
    tmp_path: Path,
) -> None:
    """Uncommenting the whole template must reproduce the defaults exactly.

    This is what makes the file trustworthy as a reference: it cannot document
    a default that the code no longer has.
    """

    uncommented = "\n".join(
        f"{name}={value}" for name, value in _example_lines()
    )
    example_env = tmp_path / "example.env"
    example_env.write_text(uncommented + "\n", encoding="utf-8")

    assert settings(_env_file=str(example_env)) == settings()


def test_the_example_config_file_reproduces_the_defaults(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verbatim copy of ``config.example.yaml`` must be a no-op.

    Same contract as the ``.env.example`` check above: the template cannot
    document a value the code no longer has. Keys the file leaves commented out
    are the ones whose default is "unset", so they never reach the parsed
    document and are not compared.
    """

    defaults = settings()
    documented = yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))
    _point_at(monkeypatch, EXAMPLE_YAML)
    loaded = settings()

    assert documented, "config.example.yaml is empty"

    for section in documented:
        assert section in Settings.model_fields, f"unknown section: {section}"
        assert getattr(loaded, section) == getattr(defaults, section), (
            f"{section} in config.example.yaml no longer matches the code defaults"
        )
