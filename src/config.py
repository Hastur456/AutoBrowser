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

The configuration file is resolved as follows:

1. If `AUTOBROWSER_CONFIG_FILE` is set, that path is used. If the file does
   not exist, startup fails.
2. If `AUTOBROWSER_CONFIG_FILE` is not set, `config.yaml` at the repo root
   (`DEFAULT_CONFIG_FILE_YAML`) is used when it exists. This is a fixed path,
   not a working-directory scan: a `config.yaml` elsewhere is never picked up.
3. If neither exists, no file source is used at all.

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
    ] = "gemma4:31b-cloud"

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
#: old ``.env``/``config.yaml`` keeps starting. ``max_tool_message_refs`` was never read.
_REMOVED_MEMORY_KEYS = ("max_tool_message_refs",)


class MemorySettings(_Section):
    """Agent memory (``docs/development/2026-10-02-memory-implementation-plan.md``).

    History shaping lives in ``src/harness/memory.py`` (compaction, the history budget, the
    task digest); persistent memory files in ``src/harness/memory_store.py``. Everything
    beyond compaction is off by default.
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(key in data for key in _REMOVED_MEMORY_KEYS):
            data = {key: value for key, value in data.items() if key not in _REMOVED_MEMORY_KEYS}
            warnings.warn(
                "memory.max_tool_message_refs was removed (it was never used); "
                "delete it from your .env / config file.",
                FutureWarning,
                stacklevel=2,
            )
        return data

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
                "tool message."
            ),
        ),
    ] = 500

    compressed_snapshot_summary_chars: Annotated[
        int,
        Field(
            ge=1,
            description="With --compress-tools: characters of a snapshot summary kept.",
        ),
    ] = 300

    compressed_snapshot_error_chars: Annotated[
        int,
        Field(
            ge=1,
            description="With --compress-tools: characters of a snapshot error kept.",
        ),
    ] = 400

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
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    #: Name of the ``mcp_servers`` entry that provides browser tools (exposed unprefixed).
    #: ``None`` falls back to ``"playwright"`` when such an entry exists.
    browser_mcp_server: str | None = None

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
    "get_settings",
    "normalize_domain",
    "reload_settings",
]
