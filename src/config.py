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

Pre-existing flat names (``PORT``, ``CHROME_PATH``, ``USER_DATA_DIR``,
``OLLAMA_HOST``, ``OLLAMA_API_KEY``, ``AUTOBROWSER_AGENT_LOOP``) still resolve
through :class:`LegacyEnvSource`, which sits *below* the environment and
``.env`` sources — a canonical name always outranks its legacy spelling, and
no existing `.env` file has to change.

Usage::

    from src.config import get_settings

    settings = get_settings()
    settings.loop.turn_cap            # 50
    settings.browser.cdp_port         # 9222
    settings.storage.sessions_dir     # .autobrowser/sessions
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, ClassVar, Mapping

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

#: Env var namespace for every setting.
ENV_PREFIX = "AUTOBROWSER_"

#: Separator between a section and a field in an env var name.
ENV_NESTED_DELIMITER = "__"


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


class LegacyEnvSource(PydanticBaseSettingsSource):
    """Fallback source for env var names that predate this config module.

    Canonical ``AUTOBROWSER_<SECTION>__<FIELD>`` names always win: this source
    sits below the environment and ``.env`` sources in the priority chain and
    only supplies values for legacy flat names that are still set, in
    ``os.environ`` or in the configured ``.env`` file.
    """

    #: Legacy env var name -> dotted field path inside :class:`Settings`.
    LEGACY_NAMES: ClassVar[Mapping[str, str]] = {
        "OLLAMA_MODEL": "llm.model",
        "OLLAMA_HOST": "llm.host",
        "OLLAMA_API_KEY": "llm.api_key",
        "CHROME_PATH": "browser.chrome_path",
        "USER_DATA_DIR": "browser.user_data_dir",
        "PORT": "browser.cdp_port",
        "AUTOBROWSER_AGENT_LOOP": "flags.agent_loop",
    }

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        """Unused: :meth:`__call__` resolves the whole legacy mapping at once."""

        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Return legacy values as a nested dict keyed by section name."""

        file_values = self._file_values()
        data: dict[str, Any] = {}
        for env_name, dotted_path in self.LEGACY_NAMES.items():
            raw = os.environ.get(env_name, file_values.get(env_name))
            if raw is None:
                continue
            value = str(raw).strip()
            if not value:
                continue
            section, _, field_name = dotted_path.partition(".")
            data.setdefault(section, {})[field_name] = value
        return data

    def _file_values(self) -> dict[str, str]:
        """Return the configured ``.env`` file contents as plain strings."""

        env_file = self.settings_cls.model_config.get("env_file")
        if isinstance(env_file, (list, tuple)):
            env_file = env_file[0] if env_file else None
        if not env_file:
            return {}
        path = Path(str(env_file))
        if not path.is_file():
            return {}
        return {
            str(key): str(value)
            for key, value in dotenv_values(path).items()
            if value is not None
        }


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
        """Insert the legacy-name fallback below the env and ``.env`` sources."""

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            LegacyEnvSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""

    return Settings()


def reload_settings() -> Settings:
    """Drop the cached settings and re-read the environment and ``.env``."""

    get_settings.cache_clear()
    return get_settings()


__all__ = [
    "BrowserSettings",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "EventSettings",
    "FlagsSettings",
    "LLMSettings",
    "LegacyEnvSource",
    "LoopSettings",
    "MemorySettings",
    "ObservationSettings",
    "Settings",
    "StorageSettings",
    "get_settings",
    "reload_settings",
]
