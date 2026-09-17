"""Typed, environment-driven application settings.

This module is the single source of truth for every tunable in AutoBrowser:
model defaults, loop caps and timeouts, observation/memory/event budgets,
browser launch parameters, and on-disk storage layout. Nothing here imports
from ``src/agent_loop/``, ``src/harness/`` or ``src/browser/`` — like
:mod:`src.contracts`, it is a neutral leaf that every layer may depend on.

Environment naming uses the ``AUTOBROWSER_`` prefix with a ``__`` nested
delimiter, so ``browser.cdp_port`` is ``AUTOBROWSER_BROWSER__CDP_PORT``::

    AUTOBROWSER_LLM__MODEL=gpt-oss:20b-cloud
    AUTOBROWSER_LOOP__TURN_CAP=25
    AUTOBROWSER_BROWSER__CDP_PORT=9333

Write Windows paths with forward slashes. ``python-dotenv`` decodes escape
sequences inside *double-quoted* values, so ``"C:\\temp"`` silently becomes a
tab character; forward slashes sidestep the hazard under every quoting style.

A YAML file can layer *over* the environment for tunables that are awkward to
spell as env vars -- a whole ``llm:`` block, ``reasoning_effort``, an
``api_key``. It is strictly opt-in: no file is discovered implicitly, so the
active configuration is never a function of which files happen to sit in the
working directory. Name it explicitly::

    AUTOBROWSER_CONFIG_FILE=config.local.yaml python main.py --task "..."

Precedence, highest first:

1. Keyword args passed to ``Settings(...)`` (tests, scripts, callers).
2. The YAML file named by ``AUTOBROWSER_CONFIG_FILE``.
3. ``AUTOBROWSER_*`` environment variables.
4. ``.env``.
5. Docker/Kubernetes secret files.

The file is a *partial profile*: it only has to name the fields it wants to
settle, and each of those outranks every source below it. Sources merge
*deeply*, field by field, so ``llm: {model: ...}`` in the file decides
``llm.model`` while the rest of the ``llm`` block still resolves through the
environment, ``.env`` and the defaults -- naming one field never freezes its
neighbours. Naming the file therefore wins over ``AUTOBROWSER_*`` for the fields
it sets: put a value there only if you mean it to stick. A name in the file that
matches no section, or no field of a matching section, is rejected at startup
rather than silently ignored. See ``config.example.yaml`` for the shape; YAML
needs PyYAML, already pinned in ``requirements.txt``.

Usage::

    from src.config import get_settings

    settings = get_settings()
    settings.loop.turn_cap            # 50
    settings.browser.cdp_port         # 9222
    settings.storage.sessions_dir     # .autobrowser/sessions
    settings.llm.reasoning_effort     # None unless .env or the YAML file sets it
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

#: Env var namespace for every setting.
ENV_PREFIX = "AUTOBROWSER_"

#: Separator between a section and a field in an env var name.
ENV_NESTED_DELIMITER = "__"

#: Env var naming an explicit YAML settings file. Never set means "no file
#: source"; set but missing means the process refuses to start (see
#: :func:`_resolve_config_path`).
CONFIG_FILE_ENV_VAR = "AUTOBROWSER_CONFIG_FILE"


class _Section(BaseModel):
    """Immutable base for a nested configuration section.

    Sections forbid unknown keys: a typo in a hand-written ``Settings(...)``
    call fails loudly instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class LLMSettings(_Section):
    """Provider-neutral chat model defaults.

    ``host``/``api_key`` are left as ``None`` by default so the underlying
    client keeps its own discovery (``OLLAMA_HOST``, ``~/.ollama``, a local
    daemon on the default port).

    The reasoning budget fields are recorded here for providers that separate
    reasoning tokens from output tokens. They are declarative for now:
    ``src/providers/ollama.py`` forwards only ``temperature`` (plus the
    ``num_predict``/``num_ctx``/``top_p``/``seed`` call params), so setting them
    changes no request until that adapter is taught the parameter.
    """

    model: Annotated[
        str,
        Field(
            min_length=1,
            description="Chat model name handed to the provider.",
        ),
    ] = "gpt-oss:20b-cloud"

    temperature: Annotated[
        float,
        Field(
            ge=0.0,
            le=2.0,
            description="Sampling temperature; 0.0 is deterministic.",
        ),
    ] = 0.0

    host: Annotated[
        str | None,
        Field(description="Provider base URL. ``None`` lets the client decide."),
    ] = None

    api_key: Annotated[
        SecretStr | None,
        Field(
            description="Provider API key. Never logged; use .get_secret_value().",
        ),
    ] = None

    reasoning_effort: Annotated[
        Literal["minimal", "low", "medium", "high"] | None,
        Field(
            description=(
                "How much reasoning to spend, for providers that expose a "
                "level rather than a token count. ``None`` leaves the choice "
                "to the provider."
            ),
        ),
    ] = None

    max_output_tokens: Annotated[
        int | None,
        Field(
            ge=1,
            description=(
                "Cap on generated output tokens, reasoning included for "
                "providers that do not budget it separately. ``None`` lets "
                "the provider decide."
            ),
        ),
    ] = None

    max_reasoning_tokens: Annotated[
        int | None,
        Field(
            ge=1,
            description=(
                "Cap on reasoning/thinking tokens specifically, for providers "
                "that budget them apart from output tokens."
            ),
        ),
    ] = None


class BrowserSettings(_Section):
    """Chrome/CDP launch parameters for the harness-managed browser."""

    chrome_path: Annotated[
        Path,
        Field(description="Path to the Chrome executable."),
    ] = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

    user_data_dir: Annotated[
        Path,
        Field(description="Chrome profile directory reused across sessions."),
    ] = Path(r"C:\temp\chrome_debug_profile")

    cdp_port: Annotated[
        int,
        Field(
            ge=1,
            le=65535,
            description="Chrome DevTools Protocol port.",
        ),
    ] = 9222

    cdp_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description="Seconds to wait for the CDP port to accept connections.",
        ),
    ] = 30.0

    @field_validator("chrome_path", "user_data_dir", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        """Expand ``%VAR%``/``$VAR`` and ``~`` in configured paths."""

        return Path(os.path.expandvars(str(value))).expanduser()


class LoopSettings(_Section):
    """Bounds and phase timeouts for the engine-native agent loop."""

    turn_cap: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description="Maximum number of TurnController turns per task.",
        ),
    ] = 50

    max_replans: Annotated[
        int,
        Field(
            ge=0,
            description="Replans allowed before the loop stops as blocked.",
        ),
    ] = 3

    max_consecutive_failures: Annotated[
        int,
        Field(
            ge=1,
            description="Failed tool calls in a row before the loop stops.",
        ),
    ] = 3

    max_snapshot_recoveries: Annotated[
        int,
        Field(
            ge=0,
            description="Recovery attempts after a lost/invalid element ref.",
        ),
    ] = 1

    max_steps_without_plan_advance: Annotated[
        int,
        Field(
            ge=1,
            description="Steps allowed without advancing the plan before replanning.",
        ),
    ] = 8

    max_unchanged_snapshots: Annotated[
        int,
        Field(
            ge=1,
            description="Identical consecutive snapshots before the loop stops.",
        ),
    ] = 3

    max_ineffective_actions: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Browser actions that changed nothing in a row before further "
                "tool calls are blocked and the agent must replan."
            ),
        ),
    ] = 3

    task_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description="Wall-clock budget for one goal/task.",
        ),
    ] = 300.0

    progress_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description="Stall budget when a goal stream stops reporting progress.",
        ),
    ] = 120.0

    latest_state_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description="Budget for unwrapping the carried-forward session state.",
        ),
    ] = 15.0

    @model_validator(mode="after")
    def _clamp_phase_timeouts(self) -> "LoopSettings":
        """Keep phase budgets from exceeding the total task budget."""

        for phase in ("progress_timeout_seconds", "latest_state_timeout_seconds"):
            if getattr(self, phase) > self.task_timeout_seconds:
                object.__setattr__(self, phase, self.task_timeout_seconds)
        return self


class ObservationSettings(_Section):
    """Budgets applied when compiling a raw tool result into an observation."""

    max_content_preview_chars: Annotated[
        int,
        Field(
            ge=1,
            description="Characters of page content kept in the preview.",
        ),
    ] = 1200

    max_refs_in_observation: Annotated[
        int,
        Field(
            ge=1,
            description="Element refs retained per compiled observation.",
        ),
    ] = 25


class MemorySettings(_Section):
    """Conversation-history shaping budgets (see ``src/harness/memory.py``)."""

    max_tool_message_refs: Annotated[
        int,
        Field(
            ge=1,
            description="Refs kept when a tool message is summarized into history.",
        ),
    ] = 25


class EventSettings(_Section):
    """Truncation limits for persisted events and the agent trace sidecar."""

    max_string_chars: Annotated[
        int,
        Field(
            ge=1,
            description="Longest string preserved in a persisted event payload.",
        ),
    ] = 20_000

    agent_trace_max_text_chars: Annotated[
        int,
        Field(
            ge=1,
            description="Longest text preserved in agent_trace.jsonl entries.",
        ),
    ] = 500


class StorageSettings(_Section):
    """On-disk layout under the (git-ignored) ``.autobrowser`` root."""

    root_dir: Annotated[
        Path,
        Field(description="Root directory for all runtime artifacts."),
    ] = Path(".autobrowser")

    sessions_subdir: Annotated[str, Field(min_length=1)] = "sessions"

    batches_subdir: Annotated[str, Field(min_length=1)] = "batches"

    workspace_subdir: Annotated[str, Field(min_length=1)] = "workspace"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sessions_dir(self) -> Path:
        """Directory holding one ``<session_id>`` folder per REPL session."""

        return self.root_dir / self.sessions_subdir

    @computed_field  # type: ignore[prop-decorator]
    @property
    def batches_dir(self) -> Path:
        """Directory holding ``run_batch.py`` run folders."""

        return self.root_dir / self.batches_subdir

    def session_dir(self, session_id: str) -> Path:
        """Return the artifact directory for one session."""

        return self.sessions_dir / session_id

    def workspace_dir(self, session_id: str) -> Path:
        """Return the filesystem workspace root for one session."""

        return self.session_dir(session_id) / self.workspace_subdir

    @field_validator("root_dir", mode="after")
    @classmethod
    def _expand_root(cls, value: Path) -> Path:
        """Expand ``%VAR%``/``$VAR`` and ``~`` in the configured root."""

        return Path(os.path.expandvars(str(value))).expanduser()


class FlagsSettings(_Section):
    """Boolean compatibility toggles that do not change routing."""

    agent_loop: Annotated[
        bool,
        Field(
            description=(
                "Inert. The engine-native loop is the only runtime; parsed "
                "for CLI/env compatibility only."
            ),
        ),
    ] = False


def _resolve_config_path() -> Path | None:
    """Return the explicitly configured YAML file, or ``None``.

    The path cannot be a settings field itself -- the file has to be located
    before the model that would describe it exists -- so it comes from
    :data:`CONFIG_FILE_ENV_VAR` and nothing else. No working-directory scan:
    a stray ``config.yaml`` must not silently change what the process does.
    """

    configured = os.environ.get(CONFIG_FILE_ENV_VAR)
    if not configured:
        return None

    path = Path(os.path.expandvars(configured)).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"{CONFIG_FILE_ENV_VAR} points at a file that does not exist: {path}"
        )
    return path


class _YamlFileSettingsSource(YamlConfigSettingsSource):
    """YAML settings source that names its file when the file is at fault.

    ``YamlConfigSettingsSource`` parses in ``__init__`` and reports a malformed
    document as a bare ``yaml.YAMLError`` with no hint of which file produced
    it, which is what makes a typo in a hand-written file hard to place. So the
    parse is intercepted at ``_read_file`` -- the method the base class calls to
    turn one path into a mapping -- rather than at ``__call__``.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        # The base class parses inside ``__init__``, before it has stored
        # ``settings_cls``, so both are captured here for ``_read_file``.
        self._settings_cls = settings_cls
        self.config_path = path
        super().__init__(settings_cls, yaml_file=path)

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        try:
            data = super()._read_file(file_path)
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            raise ValueError(f"Could not read {self.config_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"{self.config_path} must contain a mapping of settings sections "
                f"at the top level, got {type(data).__name__}."
            )

        # ``Settings`` itself is ``extra="ignore"``, deliberately, so that
        # unrelated keys in ``.env`` do not break startup. That leniency would
        # turn a mistyped section name in this file -- ``loops:`` for ``loop:``
        # -- into silently ignored configuration, so it is checked here.
        unknown = sorted(set(data) - set(self._settings_cls.model_fields))
        if unknown:
            raise ValueError(
                f"{self.config_path} has unknown section(s) {unknown}; "
                f"expected any of {sorted(self._settings_cls.model_fields)}."
            )
        return data


class Settings(BaseSettings):
    """Root settings object; build once per process via :func:`get_settings`.

    ``extra="ignore"`` keeps unrelated keys in ``.env`` (tracing, provider
    keys used by other tools) from failing validation.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    observation: ObservationSettings = Field(default_factory=ObservationSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    events: EventSettings = Field(default_factory=EventSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    flags: FlagsSettings = Field(default_factory=FlagsSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Slot the optional YAML file between init kwargs and the environment.

        Order is priority, highest first: init kwargs > YAML file >
        ``AUTOBROWSER_*`` env > ``.env`` > Docker/Kubernetes secrets. Field
        defaults are appended by ``BaseSettings`` itself and lose to all of them.

        The file is a partial profile that outranks the environment: the fields
        it names are settled by it, every field it omits still resolves through
        the sources below. pydantic-settings folds the sources with a deep
        update, so naming ``llm.model`` in the file leaves ``llm.temperature``
        to the environment rather than blanking it. The source is omitted
        entirely when :data:`CONFIG_FILE_ENV_VAR` is unset, which keeps the
        environment and ``.env`` behaving as they did before it existed.
        """

        sources: list[PydanticBaseSettingsSource] = [init_settings]

        config_path = _resolve_config_path()
        if config_path is not None:
            sources.append(_YamlFileSettingsSource(settings_cls, config_path))

        sources.append(env_settings)
        sources.append(dotenv_settings)
        sources.append(file_secret_settings)
        return tuple(sources)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""

    return Settings()


def reload_settings() -> Settings:
    """Drop the cached settings and re-read the environment, ``.env`` and the
    YAML file named by :data:`CONFIG_FILE_ENV_VAR`."""

    get_settings.cache_clear()
    return get_settings()


__all__ = [
    "BrowserSettings",
    "CONFIG_FILE_ENV_VAR",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "EventSettings",
    "FlagsSettings",
    "LLMSettings",
    "LoopSettings",
    "MemorySettings",
    "ObservationSettings",
    "Settings",
    "StorageSettings",
    "get_settings",
    "reload_settings",
]
