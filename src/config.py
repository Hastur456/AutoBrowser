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
from src.contracts import ApprovalJudgeMode, HookEventName, PermissionDecision, PermissionMode
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
        # Resolved per instance in ``__init__`` (the home is read at call time).
        env_file=None,
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
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    flags: FlagsSettings = Field(default_factory=FlagsSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
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
    "LoopSettings",
    "MemorySettings",
    "ObservationSettings",
    "PermissionRule",
    "PermissionsSettings",
    "Settings",
    "StorageSettings",
    "TOOL_HOOK_EVENTS",
    "UiSettings",
    "active_config_file",
    "env_files",
    "get_settings",
    "home_dir",
    "normalize_domain",
    "reload_settings",
]
