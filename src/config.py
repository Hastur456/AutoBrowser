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
``api_key``.

Files live in the AutoBrowser home directory (:func:`home_dir`): ``AUTOBROWSER_HOME``
when set, otherwise ``~/.autobrowser``. It holds ``config.yaml``, ``.env`` and, by
default, every runtime artifact (``storage.root_dir``): sessions, memory, logs and the
input history. Fixed places only, never a working-directory scan, so running the
``autobrowser`` command from another project's folder picks up nothing of it.

The configuration file is resolved as follows:

1. If `AUTOBROWSER_CONFIG_FILE` is set, that path is used. If the file does
   not exist, startup fails.
2. Otherwise `<home>/config.yaml` is used when it exists.
3. Otherwise `config.yaml` at the repo root (`DEFAULT_CONFIG_FILE_YAML`, the legacy
   checkout profile) is used when it exists.
4. If none exists, no file source is used at all.

``.env`` is read from the repo root (legacy) and then from ``<home>/.env``, which wins
for the keys both set; the working directory's ``.env`` is not read.

`config.yaml` and `config.local.yaml` are git-ignored, so `config.yaml` at the
repo root doubles as a personal default profile -- convenient for a local
override (e.g. `llm.model`, a Chrome profile path) without exporting env vars
every session, but it also means the file is set once per machine and easy to
forget about; check it before puzzling over a setting that will not budge.

Name a specific file explicitly to use something other than the default::

```
AUTOBROWSER_CONFIG_FILE=config.local.yaml python main.py --task "..."
```

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
import re
import warnings
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
from src.contracts import (
    RECORDING_NAME_PATTERN,
    ApprovalJudgeMode,
    HookEventName,
    LadderStage,
    PermissionDecision,
    PermissionMode,
)
from src.mcp.config import MCPServerConfig

#: Env var namespace for every setting.
ENV_PREFIX = "AUTOBROWSER_"

#: Separator between a section and a field in an env var name.
ENV_NESTED_DELIMITER = "__"

#: Env var naming an explicit YAML settings file. Set but missing means the process
#: refuses to start; unset falls back to :data:`DEFAULT_CONFIG_FILE_YAML` when that
#: exists (see :func:`_resolve_config_path`).
CONFIG_FILE_ENV_VAR = "AUTOBROWSER_CONFIG_FILE"
ROOT_PATH = Path(__file__).resolve().parent.parent
#: Optional local profile, auto-loaded when present and no env var names another file.
#: Fixed to the repo root regardless of the process's working directory; not a scan --
#: a stray file elsewhere is never picked up. Git-ignored, so it is safe to keep secrets
#: in it (see ``config.yaml``'s sibling ``config.example.yaml`` for the shape).
DEFAULT_CONFIG_FILE_YAML = ROOT_PATH / "config.yaml"
#: The checkout's ``.env``, read before ``<home>/.env`` (legacy; see :func:`env_files`).
LEGACY_ENV_FILE = ROOT_PATH / ".env"

#: Env var naming the AutoBrowser home directory (process level, like the config file).
HOME_ENV_VAR = "AUTOBROWSER_HOME"
#: Name of the home directory under the user's home when ``AUTOBROWSER_HOME`` is unset.
HOME_DIR_NAME = ".autobrowser"
#: File names inside the home directory.
HOME_CONFIG_FILE_NAME = "config.yaml"
HOME_ENV_FILE_NAME = ".env"
#: The old ``storage.root_dir`` default, relative to the working directory back then.
#: Copied into many profiles, so it now means the home directory itself.
LEGACY_STORAGE_ROOT = Path(".autobrowser")


def home_dir() -> Path:
    """The AutoBrowser home: ``AUTOBROWSER_HOME`` (``~`` and ``%VAR%`` expanded, made
    absolute) or ``~/.autobrowser``. Read at call time; nothing is created here."""

    configured = os.environ.get(HOME_ENV_VAR)
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().absolute()
    return Path.home() / HOME_DIR_NAME


def active_config_file() -> Path | None:
    """The YAML settings file in use, if any (raises ``FileNotFoundError`` when
    ``AUTOBROWSER_CONFIG_FILE`` names a missing file)."""

    return _resolve_config_path()


def env_files() -> tuple[Path, ...]:
    """``.env`` files in load order; a later file wins, missing ones are skipped."""

    return (LEGACY_ENV_FILE, home_dir() / HOME_ENV_FILE_NAME)


class _Section(BaseModel):
    """Immutable base for a nested configuration section.

    Sections forbid unknown keys: a typo in a hand-written ``Settings(...)``
    call fails loudly instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


#: Wire protocols a chat model adapter can speak (``src/providers/``). These name
#: protocols, not vendors: any service speaking Chat Completions is ``openai_compat``.
LLMProvider = Literal["ollama", "openai_compat"]
#: Who runs the browser: the session over CDP, or the MCP server itself (``browser.launch``).
BrowserLaunch = Literal["cdp", "external"]

#: Request-body keys ``llm.extra_body`` may not set: each has a typed field of its own
#: (or is owned by the adapter), so a vendor profile cannot silently override them.
RESERVED_EXTRA_BODY_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "stream",
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "parallel_tool_calls",
    }
)

#: Spellings of "unset" accepted from an env var for the nullable scalars below
#: (``null`` in YAML already parses to ``None``; an empty env value means "not set").
_NULL_STRINGS = frozenset({"null", "none"})


def _null_string_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _NULL_STRINGS:
        return None
    return value


class ModelPriceSpec(_Section):
    """What one million tokens of a model cost, in ``llm.currency``."""

    input_per_mtok: Annotated[float, Field(ge=0.0, description="Price of 1M input tokens.")]
    output_per_mtok: Annotated[float, Field(ge=0.0, description="Price of 1M output tokens.")]
    cached_input_per_mtok: Annotated[
        float | None,
        Field(ge=0.0, description="Price of 1M cached input tokens; None: the input price."),
    ] = None


class LLMSettings(_Section):
    """Provider-neutral chat model defaults.

    ``provider`` picks the wire protocol (see
    ``docs/decisions/2026-10-22-openai-compatible-chat-provider.md``); every other
    field says in its description which adapter reads it.

    For ``ollama`` ``host``/``api_key`` may stay ``None`` so the client keeps its own
    discovery (``OLLAMA_HOST``, a local daemon on the default port). ``openai_compat``
    has no vendor default: ``host`` is required, and neither the key nor the base URL is
    read from ``OPENAI_*`` environment variables.

    ``max_reasoning_tokens`` stays declarative: Chat Completions has no standard field
    for it, so a vendor that supports one gets it through ``extra_body``.
    """

    provider: Annotated[
        LLMProvider,
        Field(
            description=(
                "Wire protocol of the chat model: ``ollama`` (/api/chat) or "
                "``openai_compat`` (any Chat Completions service)."
            ),
        ),
    ] = "ollama"

    model: Annotated[
        str,
        Field(
            min_length=1,
            description="Chat model name handed to the provider.",
        ),
    ] = "gemma4:31b-cloud"

    temperature: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=2.0,
            description=(
                "Sampling temperature; 0.0 is deterministic. ``None`` sends no "
                "temperature at all (reasoning models reject the parameter)."
            ),
        ),
    ] = 0.0

    host: Annotated[
        str | None,
        Field(
            description=(
                "Provider base URL. Required for ``openai_compat`` (``.../v1``); "
                "``None`` lets the Ollama client decide."
            ),
        ),
    ] = None

    api_key: Annotated[
        SecretStr | None,
        Field(
            description=(
                "Provider API key, sent as ``Authorization: Bearer``; without one "
                "no such header is sent. Never logged; use .get_secret_value()."
            ),
        ),
    ] = None

    timeout_seconds: Annotated[
        float | None,
        Field(
            gt=0.0,
            description=(
                "Timeout of one HTTP call to the provider, both adapters. ``None`` "
                "waits forever."
            ),
        ),
    ] = 60.0

    max_retries: Annotated[
        int,
        Field(
            ge=0,
            le=10,
            description=(
                "``openai_compat``: client retries on connection errors, 408/409/"
                "429/5xx. 0 keeps one model call within the goal stall budget."
            ),
        ),
    ] = 0

    reasoning_effort: Annotated[
        Literal["minimal", "low", "medium", "high"] | None,
        Field(
            description=(
                "How much reasoning to spend, sent by ``openai_compat`` (declarative "
                "for ``ollama``). ``None`` leaves the choice to the provider."
            ),
        ),
    ] = None

    max_output_tokens: Annotated[
        int | None,
        Field(
            ge=1,
            description=(
                "Cap on generated output tokens, reasoning included for "
                "providers that do not budget it separately: ``num_predict`` for "
                "``ollama``, the ``max_tokens_param`` field for ``openai_compat``. "
                "``None`` lets the provider decide."
            ),
        ),
    ] = None

    max_tokens_param: Annotated[
        Literal["max_completion_tokens", "max_tokens"],
        Field(
            description=(
                "``openai_compat``: request field carrying ``max_output_tokens``; "
                "older servers and some models only know ``max_tokens``."
            ),
        ),
    ] = "max_completion_tokens"

    max_reasoning_tokens: Annotated[
        int | None,
        Field(
            ge=1,
            description=(
                "Cap on reasoning/thinking tokens specifically, for providers "
                "that budget them apart from output tokens. Declarative: no adapter "
                "sends it; use ``extra_body`` for a vendor equivalent."
            ),
        ),
    ] = None

    parallel_tool_calls: Annotated[
        bool | None,
        Field(
            description=(
                "``openai_compat``: ``parallel_tool_calls`` sent with tool requests. "
                "The engine runs one call per turn, so ``false`` saves tokens; "
                "``None`` sends nothing (not every server accepts it)."
            ),
        ),
    ] = None

    extra_headers: Annotated[
        dict[str, str],
        Field(
            description=(
                "``openai_compat``: extra request headers for a vendor. Values may "
                "be secrets: they are never logged and ``doctor`` prints names only."
            ),
        ),
    ] = {}

    extra_body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "``openai_compat``: vendor fields merged into the request body. "
                "Keys with a typed field of their own are rejected."
            ),
        ),
    ] = {}

    error_preview_chars: Annotated[
        int,
        Field(
            ge=40,
            description=(
                "Characters of a provider error body quoted in the error message."
            ),
        ),
    ] = 300

    prices: Annotated[
        dict[str, ModelPriceSpec],
        Field(
            description=(
                "Price per 1M tokens by model id, for cost reports and money budgets "
                "(loop.max_task_cost). A model without a price has an unknown cost."
            ),
        ),
    ] = {}

    currency: Annotated[
        str,
        Field(min_length=1, description="Currency of llm.prices and of cost reports."),
    ] = "USD"

    @field_validator("temperature", "timeout_seconds", "parallel_tool_calls", mode="before")
    @classmethod
    def _null_string_means_unset(cls, value: Any) -> Any:
        """Let an env var spell ``None`` as ``null``/``none`` for the nullable scalars."""

        return _null_string_to_none(value)

    @field_validator("extra_body", mode="after")
    @classmethod
    def _extra_body_keys_are_not_reserved(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = sorted(RESERVED_EXTRA_BODY_KEYS.intersection(value))
        if reserved:
            raise ValueError(
                f"llm.extra_body may not set {reserved}; use the typed llm.* fields."
            )
        return value

    @model_validator(mode="after")
    def _openai_compat_needs_a_host(self) -> "LLMSettings":
        if self.provider == "openai_compat" and not (self.host or "").strip():
            raise ValueError(
                "llm.host is required for provider openai_compat: set the service "
                "base URL, e.g. https://<service>/v1 (AUTOBROWSER_LLM__HOST)."
            )
        return self


class ModelTierSpec(_Section):
    """One cascade tier above the session model (tier 0).

    The fields a profile names override ``llm``; every other one is inherited from it.
    An explicit ``temperature: null`` sends no temperature (``model_fields_set`` tells it
    apart from an omitted field).
    """

    model: Annotated[
        str, Field(min_length=1, description="Model id handed to the provider; its price is llm.prices[model].")
    ]
    label: Annotated[
        str, Field(description="Label in events, usage by_model and reports; empty: the model id.")
    ] = ""
    provider: Annotated[
        LLMProvider | None,
        Field(description="Wire protocol; None: the session's (--provider or llm.provider)."),
    ] = None
    host: Annotated[str | None, Field(description="Provider base URL; None: llm.host.")] = None
    api_key: Annotated[
        SecretStr | None, Field(description="Provider API key; None: llm.api_key. Never logged.")
    ] = None
    temperature: Annotated[
        float | None,
        Field(ge=0.0, le=2.0, description="Sampling temperature; omitted: llm.temperature."),
    ] = None
    reasoning_effort: Annotated[
        Literal["minimal", "low", "medium", "high"] | None,
        Field(description="Reasoning effort; omitted: llm.reasoning_effort."),
    ] = None
    timeout_seconds: Annotated[
        float | None, Field(gt=0.0, description="Timeout of one call; omitted: llm.timeout_seconds.")
    ] = None

    @property
    def tier_label(self) -> str:
        return self.label or self.model

    def llm_overrides(self) -> dict[str, Any]:
        """The ``llm`` fields this tier sets: the ones its profile names, ``label`` aside."""

        return {
            name: getattr(self, name) for name in self.model_fields_set if name != "label"
        }


class CascadeSettings(_Section):
    """Model tiers above the session model and when the agent climbs them
    (``docs/development/2026-11-08-serve-mode-phase4-design.md``).

    Off by default: one tier, the session model, exactly as without a cascade. Tier 0 is
    always the session model (``llm.model``, ``--model``, ``/model``); ``tiers`` are the
    ones above it. The tier only goes up within a task.
    """

    enabled: Annotated[
        bool, Field(description="Use the tiers above the session model and escalate to them.")
    ] = False
    tiers: Annotated[
        list[ModelTierSpec],
        Field(max_length=3, description="Tiers above tier 0 (the session model), cheapest first."),
    ] = Field(default_factory=list)
    retarget_tier: Annotated[
        int, Field(ge=0, le=3, description="Tier of the replay retarget call (stage 2); <= len(tiers).")
    ] = 0
    escalate_after_failures: Annotated[
        int, Field(ge=1, description="Consecutive failed calls on the current tier before it goes up.")
    ] = 2
    escalate_after_replans: Annotated[
        int, Field(ge=1, description="Replans on the current tier before it goes up.")
    ] = 1
    escalate_on_progress_block: Annotated[
        bool, Field(description="Go up when the progress guard blocks a repeated call.")
    ] = True
    escalate_on_no_action: Annotated[
        bool,
        Field(description="An empty model answer goes up a tier instead of ending the task."),
    ] = True

    @model_validator(mode="after")
    def _consistent(self) -> CascadeSettings:
        if self.retarget_tier > len(self.tiers):
            raise ValueError(
                f"cascade.retarget_tier {self.retarget_tier} is above the last tier ({len(self.tiers)})"
            )
        labels = [tier.tier_label for tier in self.tiers]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        if duplicates:
            raise ValueError(f"cascade.tiers: duplicate label(s) {duplicates}; give each a label")
        return self


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

    cdp_poll_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            le=10.0,
            description=(
                "How often the CDP port is probed while waiting for Chrome, and how long one "
                "probe may take."
            ),
        ),
    ] = 0.5

    launch: Annotated[
        BrowserLaunch,
        Field(
            description=(
                "Who runs the browser. cdp: the session starts Chrome with a CDP port and "
                "waits for it; external: the session starts no browser and the MCP server "
                "launches its own (a stand in Docker), so no mcp_servers entry may still use "
                "{cdp_port} or {cdp_endpoint}."
            ),
        ),
    ] = "cdp"

    headless: Annotated[
        bool,
        Field(description="Start Chrome with --headless=new (launch: cdp only)."),
    ] = False

    extra_args: Annotated[
        list[str],
        Field(
            description=(
                "Further Chrome command-line arguments after the headless flag, e.g. "
                "--no-sandbox in a container (launch: cdp only)."
            ),
        ),
    ] = Field(default_factory=list)

    @field_validator("chrome_path", "user_data_dir", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        """Expand ``%VAR%``/``$VAR`` and ``~`` in configured paths."""

        return Path(os.path.expandvars(str(value))).expanduser()


#: Loop keys that were removed but are still accepted (and ignored, with a warning) so an
#: old ``.env``/``config.yaml`` keeps starting. ``latest_state_timeout_seconds`` bounded the
#: removed latest-state loader; the carried state now comes straight from the result.
_REMOVED_LOOP_KEYS = ("latest_state_timeout_seconds",)


class LoopSettings(_Section):
    """Bounds and phase timeouts for the engine-native agent loop."""

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(key in data for key in _REMOVED_LOOP_KEYS):
            data = {key: value for key, value in data.items() if key not in _REMOVED_LOOP_KEYS}
            warnings.warn(
                "loop.latest_state_timeout_seconds was removed (nothing waits for the carried "
                "state any more); delete it from your .env / config file.",
                FutureWarning,
                stacklevel=2,
            )
        return data

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

    max_steps_without_plan_advance: Annotated[
        int,
        Field(
            ge=1,
            description="Steps allowed without advancing the plan before replanning.",
        ),
    ] = 8

    max_ineffective_actions: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Times the same tool call (same name and arguments) may return an "
                "identical result before a further identical call is blocked and the "
                "agent must replan."
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

    max_task_model_calls: Annotated[
        int | None,
        Field(
            ge=1,
            description=(
                "Model calls one task may make (plan, turns, retargets, approval judge); "
                "None: no cap. A run profile's own budget wins."
            ),
        ),
    ] = None

    max_task_tokens: Annotated[
        int | None,
        Field(ge=1, description="Input plus output tokens one task may use; None: no cap."),
    ] = None

    max_task_cost: Annotated[
        float | None,
        Field(
            gt=0.0,
            description=(
                "Money one task may spend, in llm.currency (needs llm.prices); None: no cap. "
                "Not checked while a call's cost is unknown."
            ),
        ),
    ] = None

    @field_validator("max_task_model_calls", "max_task_tokens", "max_task_cost", mode="before")
    @classmethod
    def _null_string_means_unset(cls, value: Any) -> Any:
        return _null_string_to_none(value)

    @model_validator(mode="after")
    def _clamp_phase_timeouts(self) -> "LoopSettings":
        """Keep the stall budget from exceeding the total task budget."""

        if self.progress_timeout_seconds > self.task_timeout_seconds:
            object.__setattr__(self, "progress_timeout_seconds", self.task_timeout_seconds)
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

    action_history_limit: Annotated[
        int,
        Field(
            ge=1,
            description="Recent tool calls rendered in the Action History context block.",
        ),
    ] = 12

    action_history_preview_chars: Annotated[
        int,
        Field(
            ge=16,
            description="Characters of arguments/result kept per Action History entry.",
        ),
    ] = 200


#: Memory keys that were removed but are still accepted (and ignored, with a warning) so an
#: old ``.env``/``config.yaml`` keeps starting, with the reason shown in the warning.
_REMOVED_MEMORY_KEYS = {
    "max_tool_message_refs": "it was never used",
    "compressed_snapshot_summary_chars": "every tool now uses compressed_tool_result_chars",
    "compressed_snapshot_error_chars": "every tool now uses compressed_tool_result_chars",
}


class MemorySettings(_Section):
    """Agent memory (``docs/development/2026-10-02-memory-implementation-plan.md``).

    History shaping lives in ``src/harness/memory.py`` (compaction, the history budget, the
    task digest); persistent memory files in ``src/harness/memory_store.py``. Everything
    beyond compaction is off by default.
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in [key for key in data if key in _REMOVED_MEMORY_KEYS]:
            warnings.warn(
                f"memory.{key} was removed ({_REMOVED_MEMORY_KEYS[key]}); "
                "delete it from your .env / config file.",
                FutureWarning,
                stacklevel=2,
            )
        return {key: value for key, value in data.items() if key not in _REMOVED_MEMORY_KEYS}

    compact_tool_output_min_chars: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Older tool outputs longer than this are compacted once a newer "
                "result of the same tool exists; shorter outputs are always kept."
            ),
        ),
    ] = 1000

    compressed_tool_result_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "With --compress-tools: characters of the summary or error kept in a "
                "tool message, for every tool alike."
            ),
        ),
    ] = 500

    # -- L1 working context / L3 session -------------------------------------

    history_budget_chars: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Ceiling on the characters of the conversation history; past it the oldest "
                "tool outputs are replaced by a [cleared] placeholder. 0 disables the budget."
            ),
        ),
    ] = 0

    keep_recent_tool_results: Annotated[
        int,
        Field(ge=0, description="The newest tool outputs the history budget never clears."),
    ] = 3

    keep_recent_tasks: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Finished tasks kept verbatim in the session history; older ones are folded "
                "into one digest message each. 0 keeps every task verbatim."
            ),
        ),
    ] = 0

    digest_request_chars: Annotated[
        int,
        Field(ge=1, description="Characters of the user request kept in a task digest."),
    ] = 300

    digest_answer_chars: Annotated[
        int,
        Field(ge=1, description="Characters of the final answer kept in a task digest."),
    ] = 500

    # -- L4 persistent memory -------------------------------------------------

    persistent_enabled: Annotated[
        bool,
        Field(description="Load persistent memory files into a Memory context block."),
    ] = False

    dir: Annotated[
        str,
        Field(min_length=1, description="Memory directory, relative to storage.root_dir."),
    ] = "memory"

    index_max_lines: Annotated[
        int,
        Field(ge=1, description="Lines of the memory index rendered into the prompt."),
    ] = 200

    index_max_chars: Annotated[
        int,
        Field(ge=1, description="Characters of the memory index rendered into the prompt."),
    ] = 25_000

    index_description_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of the first body line used as the index description of a "
                "file whose frontmatter has none."
            ),
        ),
    ] = 120

    block_max_chars: Annotated[
        int,
        Field(ge=1, description="Characters of the whole Memory context block."),
    ] = 12_000

    block_min_section_chars: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "A body the Memory block budget would cut below this many characters is "
                "left out of the block instead of being shown as a stub."
            ),
        ),
    ] = 40

    file_max_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of one memory file: longer bodies are truncated on read and "
                "refused on write."
            ),
        ),
    ] = 8_000

    tool_enabled: Annotated[
        bool,
        Field(
            description=(
                "Register the memory_view / memory_write tools (needs persistent_enabled)."
            ),
        ),
    ] = False

    promote_after_successes: Annotated[
        int,
        Field(
            ge=1,
            description="Successful tasks that load an unverified entry before it is verified.",
        ),
    ] = 2

    stale_after_failures: Annotated[
        int,
        Field(
            ge=1,
            description="Consecutive blocked tasks that load an entry before it is stale.",
        ),
    ] = 2

    stale_after_days: Annotated[
        int,
        Field(
            ge=0,
            description="A verified entry older than this renders as stale. 0 disables the TTL.",
        ),
    ] = 90

    consolidate_on_goal_end: Annotated[
        bool,
        Field(
            description=(
                "After a done task, ask the model for up to consolidation_max_entries "
                "memory entries (written as unverified)."
            ),
        ),
    ] = False

    consolidation_timeout_seconds: Annotated[
        float,
        Field(gt=0, description="Ceiling on the consolidation model call."),
    ] = 30.0

    consolidation_max_entries: Annotated[
        int,
        Field(ge=1, description="Entries one consolidation call may write."),
    ] = 3

    consolidation_field_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of each consolidation prompt field (the task, the final answer, "
                "the action journal)."
            ),
        ),
    ] = 4_000

    event_reason_chars: Annotated[
        int,
        Field(
            ge=1,
            description="Characters of a reason, detail or path recorded in a memory.* event.",
        ),
    ] = 300

    # -- L2 task state ---------------------------------------------------------

    working_notes_max_chars: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Characters of the model's working notes kept for the current task. "
                "0 disables working notes."
            ),
        ),
    ] = 0


class SkillsSettings(_Section):
    """Agent skills: ``SKILL.md`` folders, a catalog in the prompt, bodies loaded on demand
    (``docs/development/2026-10-26-agent-skills-design.md``).

    Built-in skills marked ``always`` reach the prompt whether or not skills are enabled;
    ``enabled`` adds the catalog, the ``skill_view`` tool and the user's skills.
    """

    enabled: Annotated[
        bool,
        Field(
            description=(
                "Show the skill catalog, register the skill_view tool and read the user's "
                "skills. Always-on built-in skills (the browser rules) work without it."
            ),
        ),
    ] = False

    dir: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Folder of the user's skills (one sub-folder with a SKILL.md per skill), "
                "relative to storage.root_dir."
            ),
        ),
    ] = "skills"

    max_skills: Annotated[
        int,
        Field(ge=1, description="Skills read from one source; the rest are skipped."),
    ] = 100

    catalog_max_chars: Annotated[
        int,
        Field(ge=1, description="Characters of the whole Skills catalog block."),
    ] = 4_000

    description_max_chars: Annotated[
        int,
        Field(ge=1, description="Characters of one skill description shown in the catalog."),
    ] = 1_024

    body_max_chars: Annotated[
        int,
        Field(ge=1, description="Characters of a skill body returned by skill_view."),
    ] = 20_000

    file_max_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of a skill file returned by skill_view; a SKILL.md longer than "
                "this is not loaded."
            ),
        ),
    ] = 20_000

    always_max_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of the body of an always-on skill (put into every system "
                "prompt); a longer one is not loaded."
            ),
        ),
    ] = 12_000


#: What of the page URL must match at a playback checkpoint.
CheckpointUrlMode = Literal["path", "host", "off"]


class ReplaySettings(_Section):
    """Recordings of completed runs and their playback without the model
    (``docs/development/2026-11-02-serve-mode-design.md`` §5, §9.1).

    Off by default: nothing is recorded or replayed until ``enabled``.
    """

    enabled: Annotated[
        bool,
        Field(
            description=(
                "Record completed runs (--record, /record) and replay recordings "
                "(-p --recording, autobrowser recordings)."
            ),
        ),
    ] = False

    dir: Annotated[
        str,
        Field(min_length=1, description="Folder of the recordings, relative to storage.root_dir."),
    ] = "recordings"

    max_steps: Annotated[
        int,
        Field(ge=1, description="Steps of one recording; a longer run is not recorded."),
    ] = 100

    checkpoint_min_similarity: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Lowest similarity of a step's output to the recorded one before playback "
                "hands the task to the agent."
            ),
        ),
    ] = 0.6

    checkpoint_url: Annotated[
        CheckpointUrlMode,
        Field(
            description=(
                "What of the page URL must match at a checkpoint: the path (with the host), "
                "the host only, or nothing."
            ),
        ),
    ] = "path"

    describe_context_lines: Annotated[
        int,
        Field(
            ge=0,
            le=10,
            description="Ancestor lines kept in a recorded element descriptor.",
        ),
    ] = 2

    text_shingle_words: Annotated[
        int,
        Field(
            ge=1,
            le=10,
            description=(
                "Words per shingle in the checkpoint fingerprint of a non-browser tool output."
            ),
        ),
    ] = 3

    retarget_enabled: Annotated[
        bool,
        Field(
            description=(
                "Let a model retarget one diverged step (ladder stage 2) before the agent "
                "takes over."
            ),
        ),
    ] = True

    max_retargets_per_run: Annotated[
        int,
        Field(ge=0, description="Retargeted steps per task."),
    ] = 3

    retarget_context_chars: Annotated[
        int,
        Field(ge=1_000, description="Characters of page text sent with a retarget request."),
    ] = 12_000

    retarget_timeout_seconds: Annotated[
        float,
        Field(gt=0, description="Timeout of one retarget model call."),
    ] = 30.0

    default_answer: Annotated[
        Literal["agent", "output", "summary"],
        Field(
            description=(
                "How a new recording answers after its steps: one agent turn, the region of "
                "the last output, or a fixed summary."
            ),
        ),
    ] = "agent"

    keep_previous: Annotated[
        bool,
        Field(description="Keep the replaced recording as previous.json."),
    ] = True

    detail_max_chars: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Characters of a divergence detail or a skip reason in recording and "
                "playback events."
            ),
        ),
    ] = 300

    secret_field_pattern: Annotated[
        str,
        Field(
            description=(
                "Case-insensitive regex over the accessible name of a text-entry field; a "
                "recorded step typing into a matching field keeps no typed value and is "
                "never replayed blindly. Empty: only key-based redaction."
            ),
        ),
    ] = '(?<![a-zа-яё])(password|passcode|passphrase|pin|pin code|cvv|cvc|security code|card number|one-time code|secret|api key|access token|пароль|пин-код|пин)(?![a-zа-яё])'

    @field_validator("secret_field_pattern", mode="after")
    @classmethod
    def _secret_field_pattern_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"replay.secret_field_pattern is not a regex: {exc}") from exc
        return value


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
    """On-disk layout under the storage root, the AutoBrowser home by default."""

    root_dir: Annotated[
        Path,
        Field(
            default_factory=home_dir,
            description=(
                "Root directory for all runtime artifacts (sessions, memory, logs, input "
                "history). Default: the AutoBrowser home (AUTOBROWSER_HOME or "
                "~/.autobrowser). A relative path is relative to the home; the legacy "
                "value .autobrowser means the home itself."
            ),
        ),
    ]

    sessions_subdir: Annotated[str, Field(min_length=1)] = "sessions"

    batches_subdir: Annotated[str, Field(min_length=1)] = "batches"

    workspace_subdir: Annotated[str, Field(min_length=1)] = "workspace"

    persist_state: Annotated[
        bool,
        Field(
            description=(
                "Write <session>/state.json after every task: the dialogue and page state "
                "a following task inherits, so --continue/--resume can pick the session "
                "up later. Stored as is (not redacted), like the page content it holds."
            ),
        ),
    ] = True

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
        """Expand ``%VAR%``/``$VAR`` and ``~``; anchor a relative root at the home."""

        value = Path(os.path.expandvars(str(value))).expanduser()
        if value.is_absolute():
            return value
        if value == LEGACY_STORAGE_ROOT:
            warnings.warn(
                "storage.root_dir: .autobrowser is the old working-directory default; it "
                "now means the AutoBrowser home (AUTOBROWSER_HOME or ~/.autobrowser). "
                "Delete it from your .env / config file, or set an absolute path to keep "
                "using another folder.",
                FutureWarning,
                stacklevel=2,
            )
            return home_dir()
        return home_dir() / value


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


#: Hook events that concern one tool call; only these accept a ``match`` filter.
TOOL_HOOK_EVENTS: frozenset[str] = frozenset(
    {"pre_tool_use", "permission_request", "post_tool_use", "post_tool_use_failure"}
)


class HookMatch(_Section):
    """Tool filter of one hook; an empty filter matches every tool call."""

    server: Annotated[
        str,
        Field(description="Exact MCP server name; empty matches any server."),
    ] = ""

    tool: Annotated[
        str,
        Field(
            description=(
                "Regular expression matched with ``re.fullmatch`` against the exposed "
                "tool name; ``a|b`` works as a list (escape a literal dot). Empty "
                "matches any tool."
            ),
        ),
    ] = ""

    @field_validator("tool", mode="after")
    @classmethod
    def _compile_tool_pattern(cls, value: str) -> str:
        """Reject a pattern that does not compile at startup, not on the first call."""

        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid tool pattern {value!r}: {exc}") from exc
        return value


class HookSpec(_Section):
    """One registered lifecycle hook (see ``src/harness/hooks.py``)."""

    id: Annotated[
        str,
        Field(min_length=1, description="Unique hook id, shown in hook.decided events."),
    ]

    event: Annotated[
        HookEventName,
        Field(description="Lifecycle event the hook runs on."),
    ]

    type: Annotated[
        Literal["python", "command"],
        Field(
            description=(
                "``python`` runs the in-process ``handler``; ``command`` runs ``command`` "
                "as an external process (see ``src/harness/command_hooks.py``)."
            ),
        ),
    ] = "python"

    handler: Annotated[
        str,
        Field(
            pattern=r"^([A-Za-z_][\w.]*:[A-Za-z_][\w.]*)?$",
            description=(
                "Import path ``package.module:attr`` of an async handler, or of a "
                "factory returning one when ``options`` is not empty (``python`` only)."
            ),
        ),
    ] = ""

    command: Annotated[
        str,
        Field(
            description=(
                "Shell command run in the repository root (``command`` only); it reads "
                "the event as JSON on stdin and answers with its exit code and stdout."
            ),
        ),
    ] = ""

    match: Annotated[
        HookMatch,
        Field(description="Tool filter; only valid for tool events."),
    ] = Field(default_factory=HookMatch)

    options: Annotated[
        dict[str, Any],
        Field(description="Keyword arguments for the handler factory."),
    ] = Field(default_factory=dict)

    timeout_seconds: Annotated[
        float | None,
        Field(
            gt=0.0,
            description="Per-hook timeout; ``None`` uses hooks.default_timeout_seconds.",
        ),
    ] = None

    fail_closed: Annotated[
        bool | None,
        Field(
            description=(
                "Deny on timeout/exception. ``None`` keeps the per-event default "
                "(deny for goal_start and pre_tool_use, no decision otherwise)."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def _fields_fit_the_type(self) -> HookSpec:
        if self.type == "python":
            if not self.handler:
                raise ValueError(f"hook {self.id!r}: a python hook needs 'handler'.")
            if self.command:
                raise ValueError(f"hook {self.id!r}: 'command' needs type: command.")
        else:
            if not self.command.strip():
                raise ValueError(f"hook {self.id!r}: a command hook needs 'command'.")
            if self.handler or self.options:
                raise ValueError(
                    f"hook {self.id!r}: 'handler'/'options' are only valid for python hooks."
                )
        return self

    @model_validator(mode="after")
    def _match_only_for_tool_events(self) -> HookSpec:
        if self.event not in TOOL_HOOK_EVENTS and self.match != HookMatch():
            raise ValueError(
                f"hook {self.id!r}: 'match' is only valid for tool events "
                f"({', '.join(sorted(TOOL_HOOK_EVENTS))}), not {self.event!r}."
            )
        return self


class HooksSettings(_Section):
    """Deterministic lifecycle hooks around the agent loop. Disabled by default."""

    enabled: Annotated[
        bool,
        Field(description="Run the hooks in ``registry``; off means no hook runs."),
    ] = False

    max_stop_blocks: Annotated[
        int,
        Field(
            ge=0,
            description="Completions a stop hook may reject per task before done is accepted.",
        ),
    ] = 2

    default_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description="Timeout of a hook without its own timeout_seconds.",
        ),
    ] = 10.0

    command_stderr_tail_chars: Annotated[
        int,
        Field(
            ge=0,
            le=20_000,
            description="Characters of a failed command hook's stderr kept in the error.",
        ),
    ] = 300

    command_kill_wait_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            le=60.0,
            description="How long to wait for a timed-out command hook's process tree to exit.",
        ),
    ] = 2.0

    registry: Annotated[
        list[HookSpec],
        Field(description="Hooks, run sequentially in this order per event."),
    ] = Field(default_factory=list)


def normalize_domain(value: str) -> str:
    """Canonical host form shared by permission rules and resource resolvers.

    Lowercase, no leading ``*.``/``.``/``www.``, IDN labels as punycode; ``""`` stays ``""``.
    Raises ``ValueError`` for a value that is not a host (a URL, a path, a port).
    """

    host = str(value or "").strip().lower().rstrip(".")
    for prefix in ("*.", ".", "www."):
        host = host.removeprefix(prefix)
    if not host:
        return ""
    if any(char in host for char in "/:@ ?#"):
        raise ValueError(f"not a domain: {value!r} (write the host only, e.g. ozon.ru)")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"not a domain: {value!r}: {exc}") from exc


def _compile(pattern: str, what: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid {what} pattern {pattern!r}: {exc}") from exc
    return pattern


class PermissionRule(_Section):
    """One declarative permission rule (see ``src/harness/permissions.py``).

    Every filter that is set must match; conflicts between matching rules resolve
    ``deny > ask > allow`` regardless of order. A filter on a resource (``domains``,
    ``not_domains``, ``target``) whose value could not be resolved fails closed: the rule
    matches for ``deny``/``ask`` and does not match for ``allow``.
    """

    id: Annotated[
        str,
        Field(min_length=1, description="Unique rule id, shown in permission.decided events."),
    ]

    decision: Annotated[
        PermissionDecision,
        Field(description="What a matching call gets: allow, ask (approval) or deny."),
    ]

    tool: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Regular expression matched with ``re.fullmatch`` against the exposed "
                "tool name; ``a|b`` works as a list."
            ),
        ),
    ] = ".*"

    server: Annotated[
        str,
        Field(description="Exact MCP server name; empty matches any server."),
    ] = ""

    args: Annotated[
        dict[str, str],
        Field(
            description=(
                "``{argument: regex}``, each searched (``re.search``) in the string form "
                "of that argument; a missing argument does not match."
            ),
        ),
    ] = Field(default_factory=dict)

    domains: Annotated[
        list[str],
        Field(description="Match only on these domains (suffix: ozon.ru covers www.ozon.ru)."),
    ] = Field(default_factory=list)

    not_domains: Annotated[
        list[str],
        Field(description="Match only outside these domains (suffix match)."),
    ] = Field(default_factory=list)

    target: Annotated[
        str,
        Field(description="Regex searched in the resolved action target (e.g. a button label)."),
    ] = ""

    always_ask: Annotated[
        bool,
        Field(description="``ask`` only: no session grant and no bypass mode can cover it."),
    ] = False

    reason: Annotated[
        str,
        Field(
            description=(
                "Shown to the model and the approver; ``{tool}`` is replaced by the tool "
                "name. Empty generates one from the rule id. Never put secrets here."
            ),
        ),
    ] = ""

    @field_validator("tool", "target", mode="after")
    @classmethod
    def _compile_patterns(cls, value: str) -> str:
        return _compile(value, "rule") if value else value

    @field_validator("args", mode="after")
    @classmethod
    def _compile_arg_patterns(cls, value: dict[str, str]) -> dict[str, str]:
        for key, pattern in value.items():
            _compile(pattern, f"args.{key}")
        return value

    @field_validator("domains", "not_domains", mode="after")
    @classmethod
    def _normalize_domains(cls, value: list[str]) -> list[str]:
        domains = [normalize_domain(item) for item in value]
        if "" in domains:
            raise ValueError("empty domain in the list")
        return domains

    @model_validator(mode="after")
    def _consistent(self) -> PermissionRule:
        if self.always_ask and self.decision != "ask":
            raise ValueError(f"rule {self.id!r}: always_ask is only valid for decision: ask.")
        if self.domains and self.not_domains:
            raise ValueError(f"rule {self.id!r}: use either domains or not_domains, not both.")
        return self


class PermissionsSettings(_Section):
    """Tool authorization (``src/harness/permissions.py``).

    No rule ships with the code: the engine knows no tool names, so what is risky is what
    ``rules`` says plus, when ``approval_judge`` enables them, the model judgments
    (``src/agent_loop/execution/approval.py``). A list set in one source replaces (never
    extends) the list of the sources below it.
    """

    mode: Annotated[
        PermissionMode,
        Field(
            description=(
                "default: calls without a matching rule run; read_only: only readOnlyHint "
                "tools (and allow rules) run; dont_ask: every approval becomes a deny; "
                "bypass: approvals are granted, deny rules and always_ask still hold."
            ),
        ),
    ] = "default"

    rules: Annotated[
        list[PermissionRule],
        Field(description="Permission rules; order does not matter (deny > ask > allow)."),
    ] = Field(default_factory=list)

    approval_judge: Annotated[
        ApprovalJudgeMode,
        Field(
            description=(
                "Who besides the rules may ask the human to approve a state-changing call: "
                "off; model (the acting model fills an approval_request argument offered on "
                "every tool without readOnlyHint); classifier (a separate model call judges "
                "each such call the rules let through); both. Their asks are always_ask and "
                "can never lift a rule."
            ),
        ),
    ] = "off"

    classifier_model: Annotated[
        str | None,
        Field(
            min_length=1,
            description=(
                "Chat model of the approval classifier; ``None`` reuses the session model."
            ),
        ),
    ] = None

    classifier_timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description=(
                "How long one classifier call may take; a timeout or a failure asks the "
                "human (fail closed)."
            ),
        ),
    ] = 30.0

    classifier_temperature: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=2.0,
            description=(
                "Sampling temperature of a separate ``classifier_model``. ``None`` takes "
                "``llm.temperature``; a session that sends no temperature "
                "(``llm.temperature: null``) never sends one to the classifier either."
            ),
        ),
    ] = 0.0

    @field_validator("classifier_temperature", mode="before")
    @classmethod
    def _classifier_temperature_null_string(cls, value: Any) -> Any:
        return _null_string_to_none(value)

    classifier_args_chars: Annotated[
        int,
        Field(
            ge=4,
            description="Characters of the call's JSON arguments shown to the classifier.",
        ),
    ] = 1500

    classifier_description_chars: Annotated[
        int,
        Field(
            ge=4,
            description="Characters of the tool description shown to the classifier.",
        ),
    ] = 400

    approval_false_words: Annotated[
        frozenset[str],
        Field(
            description=(
                "Lower-case values of the model's approval_request argument that mean "
                "\"no approval needed\"."
            ),
        ),
    ] = frozenset({"", "false", "no", "none", "null", "0"})

    approval_timeout_ratio: Annotated[
        float,
        Field(
            gt=0.0,
            lt=1.0,
            description=(
                "Share of loop.progress_timeout_seconds a human may take to answer an "
                "approval; no answer in time is a deny. Below 1 so the goal watchdog, which "
                "sees no events while the human thinks, never fires first."
            ),
        ),
    ] = 0.8

    approval_args_preview_chars: Annotated[
        int,
        Field(
            ge=20,
            le=20_000,
            description="Characters of the call's JSON arguments shown in an approval question.",
        ),
    ] = 300

    @field_validator("approval_judge", mode="before")
    @classmethod
    def _yaml_off(cls, value: Any) -> Any:
        # YAML 1.1 reads a bare ``off`` as ``False``.
        return "off" if value is False else value

    @field_validator("rules", mode="after")
    @classmethod
    def _unique_ids(cls, value: list[PermissionRule]) -> list[PermissionRule]:
        seen: set[str] = set()
        for rule in value:
            if rule.id in seen:
                raise ValueError(f"duplicate permission rule id {rule.id!r}")
            seen.add(rule.id)
        return value


UiFrontend = Literal["auto", "tui", "plain", "classic"]
UiScreen = Literal["inline"]
UiTheme = Literal["dark", "light", "mono"]
UiColor = Literal["auto", "truecolor", "256", "16", "none"]
UiGlyphs = Literal["auto", "unicode", "ascii"]
UiDensity = Literal["comfortable", "compact"]
UiDetail = Literal["compact", "verbose"]
UiSyncOutput = Literal["auto", "on", "off"]
UiPrintFormat = Literal["text", "json", "stream-json"]
UiLocale = Literal["ru", "en"]


class UiSettings(_Section):
    """Interactive front ends: the terminal UI, the plain fallback and print mode.

    Read by ``src/ui/`` and the CLI; the harness reads only the event stream buffer sizes.
    """

    frontend: Annotated[
        UiFrontend,
        Field(
            description=(
                "auto: the terminal UI when the terminal supports it (full or degraded), "
                "else plain; tui; plain: line-by-line output; classic: the cmd2 REPL."
            ),
        ),
    ] = "auto"

    screen: Annotated[
        UiScreen,
        Field(description="inline: the transcript flows into the terminal scrollback."),
    ] = "inline"

    theme: Annotated[UiTheme, Field(description="Colour palette.")] = "dark"

    color: Annotated[
        UiColor,
        Field(
            description=(
                "Colour depth override; auto detects it. NO_COLOR in the environment "
                "means none."
            ),
        ),
    ] = "auto"

    glyphs: Annotated[
        UiGlyphs,
        Field(description="Glyph set; auto picks unicode when the output can encode it."),
    ] = "auto"

    banner: Annotated[bool, Field(description="Show the welcome banner at startup.")] = True

    density: Annotated[
        UiDensity,
        Field(description="comfortable leaves an empty line between blocks; compact does not."),
    ] = "comfortable"

    detail: Annotated[
        UiDetail,
        Field(description="compact: collapsed tool calls; verbose: expanded ones."),
    ] = "compact"

    preview_lines: Annotated[
        int,
        Field(
            ge=0,
            le=50,
            description="Lines of a tool result shown in verbose detail before a 'more' line.",
        ),
    ] = 3

    preview_chars: Annotated[
        int,
        Field(
            ge=40,
            le=20_000,
            description="Characters of tool arguments and results shown in a preview.",
        ),
    ] = 400

    live_max_lines: Annotated[
        int,
        Field(
            ge=4,
            le=60,
            description="Height limit of the live zone, without the input field and status line.",
        ),
    ] = 12

    refresh_hz: Annotated[
        float,
        Field(
            ge=1.0,
            le=30.0,
            description="Redraws of the live zone and spinner frames per second.",
        ),
    ] = 10.0

    sync_output: Annotated[
        UiSyncOutput,
        Field(description="Synchronized output (DEC 2026) around each frame; auto detects it."),
    ] = "auto"

    escape_timeout_ms: Annotated[
        int,
        Field(
            ge=0,
            le=1000,
            description="How long a lone Esc waits for the rest of an escape sequence.",
        ),
    ] = 50

    double_press_seconds: Annotated[
        float,
        Field(
            ge=0.2,
            le=3.0,
            description="Window in which a second Ctrl+C / Ctrl+D counts as a repeat.",
        ),
    ] = 0.8

    size_poll_seconds: Annotated[
        float,
        Field(
            ge=0.1,
            le=5.0,
            description="Terminal size polling interval (Windows has no SIGWINCH).",
        ),
    ] = 0.5

    markdown: Annotated[bool, Field(description="Render the final answer as markdown.")] = True

    input_history: Annotated[
        bool,
        Field(
            description=(
                "Keep the input history in <storage.root_dir>/input_history between runs. "
                "It is plain text: whatever was typed, secrets included, is stored as is."
            )
        ),
    ] = True

    input_history_size: Annotated[
        int,
        Field(ge=0, le=100_000, description="Entries kept in the input history."),
    ] = 1000

    approval_bell: Annotated[
        bool,
        Field(description="Ring the bell and set the window title when an approval is asked."),
    ] = False

    locale: Annotated[UiLocale, Field(description="Language of the interface strings.")] = "ru"

    print_format: Annotated[
        UiPrintFormat,
        Field(
            description="Default output format of print mode (-p); --output-format overrides it."
        ),
    ] = "text"

    log_file: Annotated[
        Path,
        Field(
            description=(
                "Log of logging records, warnings and child-process stderr while the "
                "terminal UI or print mode owns the terminal. A relative path resolves "
                "against storage.root_dir."
            ),
        ),
    ] = Path("logs/autobrowser.log")

    event_buffer_size: Annotated[
        int,
        Field(
            ge=100,
            le=100_000,
            description="Redacted events the session event stream keeps for late subscribers.",
        ),
    ] = 2000

    subscriber_queue_size: Annotated[
        int,
        Field(
            ge=10,
            le=100_000,
            description="Events queued for one slow subscriber before it gets a gap marker.",
        ),
    ] = 1000

    status_interval_seconds: Annotated[
        float,
        Field(
            ge=0.2,
            le=60.0,
            description="How often the status line polls the session (MCP states emit no events).",
        ),
    ] = 1.0

    doctor_probe_timeout_seconds: Annotated[
        float,
        Field(gt=0.0, le=120.0, description="Timeout of each external check of autobrowser doctor."),
    ] = 10.0

    resume_list_size: Annotated[
        int,
        Field(ge=1, le=1000, description="Sessions offered by --resume without an id."),
    ] = 20

    shutdown_timeout_seconds: Annotated[
        float,
        Field(
            ge=0.5,
            le=60.0,
            description="How long stopping a task, the session loop or a client may take.",
        ),
    ] = 5.0

    @field_validator("log_file", mode="after")
    @classmethod
    def _expand_log_file(cls, value: Path) -> Path:
        """Expand ``%VAR%``/``$VAR`` and ``~`` in the configured log path."""

        return Path(os.path.expandvars(str(value))).expanduser()

    def resolved_log_file(self, storage: StorageSettings) -> Path:
        """The log path; a relative one is placed under the storage root."""

        if self.log_file.is_absolute():
            return self.log_file
        return storage.root_dir / self.log_file


class PipelineSettings(_Section):
    """Regression and benchmark tolerances of the test pipeline.

    Read only by the comparison tools (``src/agent_loop/comparison.py``, the eval scripts);
    the agent runtime never reads this section. See
    docs/development/2026-10-19-autonomous-test-pipeline-design.md §4.8.
    """

    request_chars_tolerance: Annotated[
        float,
        Field(
            ge=0.0,
            le=10.0,
            description=(
                "Relative change of an eval's request_chars (prompt plus tool schemas) "
                "against the baseline that is still accepted; a larger growth is a "
                "regression, a larger drop an improvement."
            ),
        ),
    ] = 0.10

    history_max_rows: Annotated[
        int,
        Field(
            ge=100,
            le=10_000_000,
            description=(
                "Rows kept in the batch run history (<storage.batches_dir>/history.jsonl, "
                "one row per scenario per run); past it the oldest rows are dropped."
            ),
        ),
    ] = 50_000

    batch_metric_tolerance: Annotated[
        float,
        Field(
            ge=0.0,
            le=10.0,
            description=(
                "Relative growth of a scenario's duration, model calls or input tokens "
                "between two batch runs (scenario passed in both) that scripts/"
                "compare_batches.py still accepts; more is a slower/costlier finding."
            ),
        ),
    ] = 0.25

    ci_state_keep_batches: Annotated[
        int,
        Field(
            ge=1,
            le=1_000,
            description=(
                "Batches of main measurements kept in the CI state artifact (scripts/"
                "ci_state.py save); the oldest are dropped past it."
            ),
        ),
    ] = 5

    ci_max_inconclusive_ratio: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Share of a CI live run's scenarios that may end in an infra failure "
                "(provider_error, timeout); a larger share makes the verdict infra, not a "
                "regression or a pass."
            ),
        ),
    ] = 0.3

    ci_release_verdict_max_age_days: Annotated[
        int,
        Field(
            ge=1,
            le=3_650,
            description=(
                "Age in days of the green main measurement a release tag needs on its commit "
                "or an ancestor (scripts/ci_state.py verdict)."
            ),
        ),
    ] = 14


class TelemetrySettings(_Section):
    """OpenTelemetry trace export of every session over OTLP/HTTP (off by default).

    Any OTLP backend can receive the spans; the backend is chosen only by the endpoint and
    headers configured here. Read by ``src/harness/otel_sink.py`` through the session's
    event sinks; the engine never reads this section. See
    docs/decisions/2026-10-29-otlp-trace-export.md.
    """

    otlp_enabled: Annotated[
        bool,
        Field(description="Export every session's tasks as OpenTelemetry spans."),
    ] = False

    otlp_endpoint: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "OTLP/HTTP traces URL of the backend. The default is the standard local "
                "OTLP port; a hosted backend names its own URL."
            ),
        ),
    ] = "http://localhost:4318/v1/traces"

    otlp_headers: Annotated[
        dict[str, SecretStr],
        Field(
            description=(
                "Headers of every export request, e.g. Authorization for a hosted "
                "backend; a JSON object in the environment. Never logged."
            ),
        ),
    ] = {}

    otlp_timeout_seconds: Annotated[
        float,
        Field(
            ge=1.0,
            le=120.0,
            description="Timeout of one export request and of the flush at session end.",
        ),
    ] = 10.0

    capture_content: Annotated[
        Literal["none", "redacted"],
        Field(
            description=(
                "none: only ids, names, counts, statuses and durations leave the machine. "
                "redacted: task text, answers and tool arguments/results too, after the "
                "event redaction and truncation."
            ),
        ),
    ] = "none"

    trajectory: Annotated[
        bool,
        Field(
            description=(
                "Put the task's steps (tool calls, gate decisions, outcome; content per "
                "capture_content) on its task span as one JSON object, "
                "autobrowser.trajectory, for evaluators that read one observation only."
            ),
        ),
    ] = False

    service_name: Annotated[
        str,
        Field(min_length=1, description="service.name resource attribute of the spans."),
    ] = "autobrowser"

    environment: Annotated[
        str | None,
        Field(
            min_length=1,
            description=(
                "deployment.environment.name on every span (e.g. interactive); unset: no "
                "attribute. A session label of the same key (batch runs set 'batch') wins."
            ),
        ),
    ] = None

    attribute_aliases: Annotated[
        dict[Annotated[str, Field(min_length=1)], Annotated[str, Field(min_length=1)]],
        Field(
            description=(
                "Copy span attribute A also under key B on every span that has A, for a "
                "backend that reads its own keys; a JSON object in the environment."
            ),
        ),
    ] = {}


def _resolve_config_path() -> Path | None:
    """Return the YAML file to load, if any.

    The path cannot be a settings field itself -- the file has to be located before the
    model that would describe it exists. :data:`CONFIG_FILE_ENV_VAR` wins when set: the
    file it names is mandatory (a typo must fail loudly, not fall back to defaults
    silently). Unset falls back to :data:`DEFAULT_CONFIG_FILE_YAML` -- a fixed path, not a
    working-directory scan -- when that file exists; otherwise there is no file source.
    """

    configured = os.environ.get(CONFIG_FILE_ENV_VAR)

    if not configured:
        home_config = home_dir() / HOME_CONFIG_FILE_NAME
        if home_config.is_file():
            return home_config
        return DEFAULT_CONFIG_FILE_YAML if DEFAULT_CONFIG_FILE_YAML.is_file() else None

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
        # UTF-8 explicitly: without it the file is read in the locale encoding (cp1251 on
        # a Russian Windows), and every non-ASCII value turns into mojibake.
        super().__init__(settings_cls, yaml_file=path, yaml_file_encoding="utf-8")

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


class EvalPlatformSettings(_Section):
    """Sync of batch results to an evaluation platform: datasets, experiments, scores (off).

    Read only by the developer tools in ``scripts/`` (the platform bridge, ``run_batch.py``)
    and the offline ``doctor`` line; the agent runtime never reads this section. The
    platform is chosen by ``host`` alone: no URL default lives in the code. See
    ADR-2026-10-31 in docs/decisions/.
    """

    enabled: Annotated[
        bool,
        Field(description="Sync batch results (datasets, experiments, scores) to the platform."),
    ] = False

    host: Annotated[
        str | None,
        Field(
            pattern=r"^https?://\S+[^/\s]$",
            description="Base URL of the platform (http or https, no trailing slash).",
        ),
    ] = None

    public_key: Annotated[
        SecretStr | None,
        Field(description="Project public key (the Basic-auth user name). Never logged."),
    ] = None

    secret_key: Annotated[
        SecretStr | None,
        Field(description="Project secret key (the Basic-auth password). Never logged."),
    ] = None

    capture_content: Annotated[
        Literal["none", "answers", "redacted"],
        Field(
            description=(
                "What of a run leaves the machine besides repository data (task texts, "
                "expectations): none; answers (final answers, task text on task spans); "
                "redacted (also tool arguments/results after event redaction)."
            ),
        ),
    ] = "none"

    dataset_prefix: Annotated[
        str,
        Field(
            pattern=r"^[a-z0-9/_.-]{1,100}$",
            description="Prefix (folder) of the dataset names, e.g. autobrowser/ -> autobrowser/golden.",
        ),
    ] = "autobrowser/"

    environment: Annotated[
        str,
        Field(
            pattern=r"^[a-z0-9_-]{1,40}$",
            description="Environment of the experiment traces and scores on the platform.",
        ),
    ] = "batch"

    auto_sync: Annotated[
        bool,
        Field(description="run_batch.py syncs the batch right after it ends (when enabled)."),
    ] = True

    sync_timeout_seconds: Annotated[
        float,
        Field(ge=10.0, le=3600.0, description="Budget of the sync after a batch."),
    ] = 300.0

    timeout_seconds: Annotated[
        float,
        Field(ge=1.0, le=300.0, description="Timeout of one HTTP request to the platform."),
    ] = 30.0

    max_retries: Annotated[
        int,
        Field(ge=0, le=10, description="Retries of one request on 429, 5xx and network errors."),
    ] = 3

    retry_backoff_seconds: Annotated[
        float,
        Field(
            ge=0.1,
            le=60.0,
            description="Base of the exponential backoff when the server sends no Retry-After.",
        ),
    ] = 2.0

    requests_per_minute: Annotated[
        int,
        Field(ge=1, le=10_000, description="Client-side rate limit of the REST requests."),
    ] = 25

    score_batch_size: Annotated[
        int,
        Field(ge=1, le=1000, description="Scores per scores request."),
    ] = 100

    max_request_bytes: Annotated[
        int,
        Field(
            ge=10_000,
            le=50_000_000,
            description="Size limit of one trace-export request; a larger trace is split.",
        ),
    ] = 4_000_000

    verify_grace_seconds: Annotated[
        float,
        Field(
            ge=0.0,
            le=3600.0,
            description="Wait before an experiment trace of unknown fate may be sent again.",
        ),
    ] = 60.0

    triage_queue: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=80,
            description="Annotation queue for failed scenarios; unset: no queueing.",
        ),
    ] = None

    triage_kinds: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        Field(
            min_length=1,
            description=(
                "Failure kinds whose scenarios go to the annotation queue; a JSON list in "
                "the environment."
            ),
        ),
    ] = ("permission_denied", "guard_limit", "agent_stopped", "grade_failed", "error", "ungraded")

    triage_max_items: Annotated[
        int,
        Field(ge=0, le=1000, description="Most scenarios queued for annotation per batch."),
    ] = 50


#: What a stand job reports on (``serve.jobs.<id>.notify_on``) and what a channel sends.
NotifyKind = Literal["change", "success", "failure", "blocked", "budget", "approval"]
#: Notification channels of ``autobrowser serve`` (Telegram arrives in phase 4).
ServeChannel = Literal["log", "webhook", "telegram"]
#: What a job does when a trigger fires while its run is busy (design section 6.3).
CoalesceMode = Literal["drop", "queue_one"]
#: Fire missed cron slots after downtime: never, or one catch-up run.
CatchUpMode = Literal["none", "one"]
#: Kinds of job triggers (``cron`` schedules are parsed by ``croniter``).
TriggerType = Literal["interval", "cron", "webhook", "telegram"]
#: How a webhook trigger authenticates its caller.
WebhookAuth = Literal["bearer", "hmac_sha256"]
#: Hosts that only this machine can reach: ``serve.host`` needs no token on them.
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: Runtime placeholder of an ``mcp_servers`` entry: the session's browser profile
#: (``SessionConfig.user_data_dir``; on the stand, the worker's ``<profiles_dir>/w<i>``).
PROFILE_DIR_PLACEHOLDER = "{profile_dir}"
_DEFAULT_NOTIFY_ON: tuple[NotifyKind, ...] = ("change", "failure", "blocked", "budget")


def _check_name(value: str, what: str) -> str:
    if not re.match(RECORDING_NAME_PATTERN, str(value)):
        raise ValueError(f"invalid {what} {value!r}: use 1-64 lowercase letters, digits and hyphens")
    return value


def _normalize_domains(values: list[str]) -> list[str]:
    return [domain for domain in (normalize_domain(value) for value in values) if domain]


class RunBudgetSpec(_Section):
    """Caps of one stand run (``src.contracts.RunBudget``); ``None``: no cap."""

    max_model_calls: Annotated[
        int | None, Field(ge=1, description="Model calls per run.")
    ] = None
    max_tokens: Annotated[
        int | None, Field(ge=1, description="Input + output tokens per run.")
    ] = None
    max_cost: Annotated[
        float | None, Field(gt=0.0, description="Money per run in llm.currency (needs llm.prices).")
    ] = None


class TriggerSpec(_Section):
    """One way a job starts: a period, a cron schedule or an HTTP webhook."""

    id: Annotated[str, Field(min_length=1, description="Trigger id, unique in the job.")]
    type: Annotated[TriggerType, Field(description="interval | cron | webhook.")]
    every_seconds: Annotated[
        float | None, Field(gt=0.0, description="interval: the period, >= serve.min_interval_seconds.")
    ] = None
    schedule: Annotated[
        str | None,
        Field(min_length=1, description="cron: a five-field cron expression in serve.timezone."),
    ] = None
    secret: Annotated[
        SecretStr | None, Field(description="webhook: the shared secret (or secret_env).")
    ] = None
    secret_env: Annotated[
        str | None,
        Field(min_length=1, description="webhook: the environment variable holding the secret."),
    ] = None
    auth: Annotated[
        WebhookAuth,
        Field(description="webhook: bearer (Authorization header) or hmac_sha256 (signed body)."),
    ] = "bearer"

    @field_validator("id")
    @classmethod
    def _id_is_a_name(cls, value: str) -> str:
        return _check_name(value, "trigger id")

    @model_validator(mode="after")
    def _fields_match_the_type(self) -> TriggerSpec:
        if self.type == "interval" and self.every_seconds is None:
            raise ValueError(f"interval trigger {self.id!r} needs every_seconds")
        if self.type == "cron":
            if not self.schedule:
                raise ValueError(f"cron trigger {self.id!r} needs a schedule")
            from croniter import croniter  # imported here: only cron triggers need it

            if not croniter.is_valid(self.schedule):
                raise ValueError(f"cron trigger {self.id!r}: invalid schedule {self.schedule!r}")
        if self.type != "interval" and self.every_seconds is not None:
            raise ValueError(f"{self.type} trigger {self.id!r} takes no every_seconds")
        if self.type != "cron" and self.schedule is not None:
            raise ValueError(f"{self.type} trigger {self.id!r} takes no schedule")
        if self.type == "webhook" and (self.secret is None) == (self.secret_env is None):
            raise ValueError(f"webhook trigger {self.id!r} needs exactly one of secret, secret_env")
        if self.type != "webhook" and (self.secret is not None or self.secret_env is not None):
            raise ValueError(f"{self.type} trigger {self.id!r} takes no secret")
        return self


class ProbeStepSpec(_Section):
    """One model-free tool call of a probe; the tool name is the user's data."""

    server: Annotated[
        str, Field(description="MCP server of the tool; empty: whichever server exposes it.")
    ] = ""
    tool: Annotated[str, Field(min_length=1, description="Exposed tool name.")]
    args: Annotated[dict[str, Any], Field(description="Arguments, passed as written.")] = Field(
        default_factory=dict
    )


class ProbeSpec(_Section):
    """Stage 0: a few tool calls without the model and a fingerprint of their output."""

    steps: Annotated[
        list[ProbeStepSpec], Field(min_length=1, description="The calls, in order.")
    ]
    select: Annotated[
        str | None, Field(description="Regex of the output region to fingerprint; None: all of it.")
    ] = None
    ignore: Annotated[
        list[str], Field(description="Regexes removed from the region before fingerprinting.")
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _patterns_compile(self) -> ProbeSpec:
        for pattern in (self.select, *self.ignore):
            if pattern is not None:
                _compile(pattern, "probe regex")
        return self


class JobSpec(_Section):
    """One stand job: a browser task, its triggers and how far and how dear it may go."""

    task: Annotated[str, Field(min_length=1, description="The task text given to the agent.")]
    enabled: Annotated[bool, Field(description="A disabled job never runs.")] = True
    triggers: Annotated[list[TriggerSpec], Field(description="What starts the job.")] = Field(
        default_factory=list
    )
    probe: Annotated[
        ProbeSpec | None, Field(description="Stage 0: skip the run while the probe output is unchanged.")
    ] = None
    recording: Annotated[
        str | None, Field(description="Recording to replay first (and to save with record_on_success).")
    ] = None
    record_on_success: Annotated[
        bool, Field(description="Save a completed run that needed a model as the recording.")
    ] = False
    max_stage: Annotated[
        LadderStage | None, Field(description="Ladder cap of the run; None: serve.default_max_stage.")
    ] = None
    budget: Annotated[
        RunBudgetSpec | None, Field(description="Run budget; None: serve.default_run_budget.")
    ] = None
    domains: Annotated[
        list[str], Field(description="Domains the run may act on (empty: no task scope).")
    ] = Field(default_factory=list)
    notify_on: Annotated[
        list[NotifyKind], Field(description="Outcomes the channels are told about.")
    ] = Field(default_factory=lambda: list(_DEFAULT_NOTIFY_ON))
    coalesce: Annotated[
        CoalesceMode | None, Field(description="None: serve.coalesce.")
    ] = None
    cooldown_seconds: Annotated[
        float | None, Field(ge=0.0, description="None: serve.cooldown_seconds.")
    ] = None
    worker: Annotated[
        int | None,
        Field(
            ge=0,
            description=(
                "Pin the job to worker w<i> (a profile with logins); that worker then runs "
                "only its pinned jobs. None: any unreserved worker."
            ),
        ),
    ] = None

    @field_validator("recording")
    @classmethod
    def _recording_is_a_name(cls, value: str | None) -> str | None:
        return None if value is None else _check_name(value, "recording name")

    @field_validator("domains")
    @classmethod
    def _domains_are_hosts(cls, values: list[str]) -> list[str]:
        return _normalize_domains(values)

    @model_validator(mode="after")
    def _consistent(self) -> JobSpec:
        if self.record_on_success and self.recording is None:
            raise ValueError("record_on_success needs a recording name")
        ids = [trigger.id for trigger in self.triggers]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate trigger id(s) {duplicates}")
        return self


class WebhookOutSettings(_Section):
    """The outgoing webhook channel: notifications POSTed as signed JSON."""

    url: Annotated[str, Field(min_length=1, description="Where notifications are POSTed (http/https).")]
    secret: Annotated[
        SecretStr | None,
        Field(description="HMAC-SHA256 key of the X-AutoBrowser-Signature header; None: unsigned."),
    ] = None
    timeout_seconds: Annotated[
        float, Field(gt=0.0, description="Timeout of one POST; a failure is logged, not retried.")
    ] = 10.0

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            # The URL is not quoted: it often carries a token.
            raise ValueError("webhook_out.url must be an http(s) URL")
        return value


class TelegramSettings(_Section):
    """The Telegram channel of the stand: notifications, commands and approval buttons over
    long polling (no inbound port). The adapter is ``src/channels/telegram.py``.

    ``bot_token`` is a secret: set it in ``AUTOBROWSER_SERVE__TELEGRAM__BOT_TOKEN``, never in a
    profile file. Only the chats in ``allowed_chat_ids`` are answered and notified.
    """

    bot_token: Annotated[
        SecretStr | None,
        Field(description="The bot's token; set it in the env. Never logged or printed."),
    ] = None
    allowed_chat_ids: Annotated[
        list[int],
        Field(description="Chats the bot answers and notifies; any other chat is ignored."),
    ] = Field(default_factory=list)
    api_base: Annotated[
        str,
        Field(
            description=(
                "Base URL of a self-hosted Bot API server (https, or http on localhost); "
                "empty: the public Bot API."
            )
        ),
    ] = ""
    approvals: Annotated[
        bool, Field(description="Answer approvals with buttons in the allowed chats.")
    ] = True
    poll_timeout_seconds: Annotated[
        int, Field(ge=1, le=60, description="Long-polling timeout of one getUpdates call.")
    ] = 30
    request_timeout_seconds: Annotated[
        float,
        Field(gt=0.0, description="Timeout of the other calls, and the slack above a poll's timeout."),
    ] = 10.0
    poll_backoff_seconds: Annotated[
        float, Field(gt=0.0, description="First pause after a failed poll; doubles up to the max.")
    ] = 1.0
    poll_backoff_max_seconds: Annotated[
        float, Field(gt=0.0, description="Longest pause between failed polls.")
    ] = 60.0
    send_retry_max_seconds: Annotated[
        float,
        Field(
            ge=0.0,
            description="Longest rate-limit wait a send sits out for one retry; 0: never retry.",
        ),
    ] = 30.0
    max_update_age_seconds: Annotated[
        float,
        Field(gt=0.0, description="Older messages are ignored (sent while the stand was down)."),
    ] = 300.0

    @field_validator("api_base")
    @classmethod
    def _safe_api_base(cls, value: str) -> str:
        if not value:
            return value
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        local_http = parts.scheme == "http" and (parts.hostname or "") in LOCAL_HOSTS
        if not (parts.scheme == "https" or local_http) or not parts.hostname:
            # The URL is not quoted: the token travels in its path.
            raise ValueError("serve.telegram.api_base must be https (or http on localhost)")
        return value.rstrip("/")

    @field_validator("allowed_chat_ids")
    @classmethod
    def _unique_chats(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def _backoff_in_order(self) -> TelegramSettings:
        if self.poll_backoff_max_seconds < self.poll_backoff_seconds:
            raise ValueError("serve.telegram.poll_backoff_max_seconds is below poll_backoff_seconds")
        return self


class ServeSettings(_Section):
    """The stand, ``autobrowser serve`` (``docs/development/serve-mode.md``,
    ``docs/development/2026-11-02-serve-mode-design.md`` section 9.4).

    Nothing runs unless the command is started. The API listens on localhost; a remote
    ``host`` needs ``allow_remote`` and a non-empty ``token``.
    """

    host: Annotated[str, Field(min_length=1, description="Address of the HTTP API.")] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535, description="Port of the HTTP API.")] = 8765
    token: Annotated[
        SecretStr | None,
        Field(description="Bearer token of /v1/*; required for a remote host. Set it in the env."),
    ] = None
    allow_remote: Annotated[
        bool, Field(description="Allow a host other than 127.0.0.1, ::1 or localhost.")
    ] = False
    dir: Annotated[
        str, Field(min_length=1, description="Stand files (ledger, events, job state) under storage.root_dir.")
    ] = "serve"
    workers: Annotated[
        int,
        Field(
            ge=1,
            le=8,
            description=(
                "Sessions (browser, MCP) running jobs in parallel; worker w<i> has its own "
                "profile <profiles_dir>/w<i> and CDP port cdp_port_base + i."
            ),
        ),
    ] = 1
    profiles_dir: Annotated[
        str, Field(min_length=1, description="Browser profiles of the workers under storage.root_dir.")
    ] = "serve/profiles"
    cdp_port_base: Annotated[
        int, Field(ge=1, le=65535, description="CDP port of worker w0 (launch: cdp); w<i> uses base + i.")
    ] = 9300
    queue_size: Annotated[int, Field(ge=1, description="Queued runs at most.")] = 20
    coalesce: Annotated[
        CoalesceMode,
        Field(description="drop: a trigger on a busy job is skipped; queue_one: one waits."),
    ] = "drop"
    cooldown_seconds: Annotated[
        float, Field(ge=0.0, description="Least time between the end of a job's run and its next start.")
    ] = 0.0
    min_interval_seconds: Annotated[
        float, Field(ge=1.0, description="Least period of a trigger (interval and cron).")
    ] = 60.0
    catch_up: Annotated[
        CatchUpMode, Field(description="Missed cron slots after downtime: none, or one catch-up run.")
    ] = "none"
    timezone: Annotated[
        str, Field(description="IANA zone of cron and day budgets; empty: the system zone.")
    ] = ""
    daily_max_runs: Annotated[int | None, Field(ge=1, description="Runs per day.")] = None
    daily_max_tokens: Annotated[int | None, Field(ge=1, description="Tokens per day.")] = None
    daily_max_cost: Annotated[
        float | None, Field(gt=0.0, description="Money per day in llm.currency.")
    ] = None
    default_run_budget: Annotated[
        RunBudgetSpec,
        Field(description="Budget of a run without its own; max_model_calls is required."),
    ] = Field(default_factory=lambda: RunBudgetSpec(max_model_calls=30))
    default_max_stage: Annotated[
        LadderStage,
        Field(description="Ladder cap of a run without its own (human: only with api_approvals)."),
    ] = "agent"
    session_max_runs: Annotated[
        int, Field(ge=1, description="Runs before the worker's session is recreated.")
    ] = 200
    shutdown_grace_seconds: Annotated[
        float, Field(gt=0.0, description="How long a running run may finish on shutdown.")
    ] = 60.0
    rate_limit_per_minute: Annotated[
        int, Field(ge=1, description="Requests per minute per token, trigger or failing client.")
    ] = 60
    max_body_bytes: Annotated[
        int, Field(ge=1024, description="Largest request body.")
    ] = 65536
    idempotency_window_seconds: Annotated[
        float, Field(ge=0.0, description="How long an Idempotency-Key is remembered.")
    ] = 3600.0
    free_tasks: Annotated[
        bool, Field(description="Accept free-form tasks on POST /v1/tasks.")
    ] = False
    free_task_domains: Annotated[
        list[str], Field(description="Task scope of free tasks (empty: none).")
    ] = Field(default_factory=list)
    api_approvals: Annotated[
        bool,
        Field(
            description=(
                "Answer approvals on POST /v1/approvals/{id} (tickets go to the channels); "
                "off: every approval is denied (dont_ask)."
            ),
        ),
    ] = False
    log_client_addresses: Annotated[
        bool, Field(description="Write client addresses into the stand events.")
    ] = False
    channels: Annotated[
        list[ServeChannel], Field(description="Notification channels: log, webhook, telegram.")
    ] = Field(default_factory=lambda: ["log"])
    notify_max_chars: Annotated[
        int, Field(ge=100, description="Longest notification body.")
    ] = 1500
    webhook_out: Annotated[
        WebhookOutSettings | None, Field(description="The outgoing webhook channel.")
    ] = None
    telegram: Annotated[
        TelegramSettings | None, Field(description="The Telegram channel (serve.channels: telegram).")
    ] = None
    jobs: Annotated[
        dict[str, JobSpec], Field(description="The stand's jobs, by name.")
    ] = Field(default_factory=dict)

    @field_validator("daily_max_runs", "daily_max_tokens", "daily_max_cost", mode="before")
    @classmethod
    def _null_string(cls, value: Any) -> Any:
        return _null_string_to_none(value)

    @field_validator("free_task_domains")
    @classmethod
    def _free_task_domains_are_hosts(cls, values: list[str]) -> list[str]:
        return _normalize_domains(values)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if value:
            import zoneinfo

            try:
                zoneinfo.ZoneInfo(value)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"unknown time zone {value!r}; on Windows install the IANA database "
                    f"(uv add tzdata) or leave serve.timezone empty for the system zone"
                ) from exc
        return value

    @model_validator(mode="after")
    def _safe_and_consistent(self) -> ServeSettings:
        token = self.token.get_secret_value().strip() if self.token is not None else ""
        if self.host not in LOCAL_HOSTS:
            if not self.allow_remote:
                raise ValueError(
                    f"serve.host {self.host!r} is reachable from other machines: set "
                    f"serve.allow_remote: true (and a token) to allow it"
                )
            if not token:
                raise ValueError(f"serve.host {self.host!r} needs a non-empty serve.token")
        if self.default_run_budget.max_model_calls is None:
            raise ValueError("serve.default_run_budget needs max_model_calls")
        last_port = self.cdp_port_base + self.workers - 1
        if last_port > 65535:
            raise ValueError(
                f"serve.cdp_port_base + serve.workers - 1 is above 65535 ({self.workers} workers)"
            )
        if self.cdp_port_base <= self.port <= last_port:
            raise ValueError("serve.port is one of the workers' CDP ports (cdp_port_base + i)")
        pinned = {job.worker for job in self.jobs.values() if job.worker is not None}
        for name, job in self.jobs.items():
            if job.worker is not None and job.worker >= self.workers:
                raise ValueError(
                    f"job {name!r} is pinned to worker {job.worker}, but serve.workers is {self.workers}"
                )
        unpinned = self.free_tasks or any(job.worker is None for job in self.jobs.values())
        if unpinned and len(pinned) >= self.workers:
            raise ValueError(
                "every worker is reserved by pinned jobs: unpinned jobs and free tasks need "
                "one more worker (serve.workers)"
            )
        if "webhook" in self.channels and self.webhook_out is None:
            raise ValueError("the webhook channel needs serve.webhook_out.url")
        if "telegram" in self.channels:
            telegram = self.telegram
            token = telegram.bot_token.get_secret_value().strip() if telegram and telegram.bot_token else ""
            if not token:
                raise ValueError(
                    "the telegram channel needs serve.telegram.bot_token "
                    "(AUTOBROWSER_SERVE__TELEGRAM__BOT_TOKEN)"
                )
            if telegram is not None and not telegram.allowed_chat_ids:
                raise ValueError("the telegram channel needs serve.telegram.allowed_chat_ids")
        for name, job in self.jobs.items():
            _check_name(name, "job name")
            for trigger in job.triggers:
                if trigger.type == "telegram" and "telegram" not in self.channels:
                    raise ValueError(
                        f"job {name!r}, trigger {trigger.id!r}: a telegram trigger needs the "
                        f"telegram channel (serve.channels)"
                    )
                if trigger.type == "interval" and (trigger.every_seconds or 0) < self.min_interval_seconds:
                    raise ValueError(
                        f"job {name!r}, trigger {trigger.id!r}: every_seconds is below "
                        f"serve.min_interval_seconds ({self.min_interval_seconds})"
                    )
        return self


class Settings(BaseSettings):
    """Root settings object; build once per process via :func:`get_settings`.

    ``extra="ignore"`` keeps unrelated keys in ``.env`` (tracing, provider
    keys used by other tools) from failing validation.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        # Resolved per instance in ``__init__`` (the home is read at call time).
        env_file=None,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    llm: LLMSettings = Field(default_factory=LLMSettings)
    cascade: CascadeSettings = Field(default_factory=CascadeSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    observation: ObservationSettings = Field(default_factory=ObservationSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    replay: ReplaySettings = Field(default_factory=ReplaySettings)
    events: EventSettings = Field(default_factory=EventSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    flags: FlagsSettings = Field(default_factory=FlagsSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    eval_platform: EvalPlatformSettings = Field(default_factory=EvalPlatformSettings)
    serve: ServeSettings = Field(default_factory=ServeSettings)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    #: Name of the ``mcp_servers`` entry that provides browser tools (exposed unprefixed).
    #: ``None`` falls back to ``"playwright"`` when such an entry exists.
    browser_mcp_server: str | None = None

    def __init__(self, **values: Any) -> None:
        # ``_env_file`` given by a caller (tests, scripts) wins, ``None`` included.
        values.setdefault("_env_file", env_files())
        super().__init__(**values)

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
        entirely when :data:`CONFIG_FILE_ENV_VAR` is unset and
        :data:`DEFAULT_CONFIG_FILE_YAML` (``config.yaml`` at the repo root) does not
        exist either, which keeps the environment and ``.env`` behaving as they did
        before this source existed.
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
    """Drop the cached settings and re-read the environment, ``.env`` and the YAML
    file (named by :data:`CONFIG_FILE_ENV_VAR`, or :data:`DEFAULT_CONFIG_FILE_YAML`
    when that is unset)."""

    get_settings.cache_clear()
    return get_settings()


__all__ = [
    "BrowserSettings",
    "CascadeSettings",
    "CONFIG_FILE_ENV_VAR",
    "DEFAULT_CONFIG_FILE_YAML",
    "HOME_ENV_VAR",
    "LEGACY_ENV_FILE",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "EventSettings",
    "FlagsSettings",
    "HookMatch",
    "HookSpec",
    "HooksSettings",
    "LLMSettings",
    "PROFILE_DIR_PLACEHOLDER",
    "LoopSettings",
    "MemorySettings",
    "ModelPriceSpec",
    "ModelTierSpec",
    "ObservationSettings",
    "PermissionRule",
    "PermissionsSettings",
    "PipelineSettings",
    "ReplaySettings",
    "Settings",
    "StorageSettings",
    "TelegramSettings",
    "TOOL_HOOK_EVENTS",
    "UiSettings",
    "active_config_file",
    "env_files",
    "get_settings",
    "home_dir",
    "normalize_domain",
    "reload_settings",
]
