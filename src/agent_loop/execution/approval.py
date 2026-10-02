"""Model judgments that escalate a tool call to human approval.

The :class:`~src.harness.permissions.PermissionEngine` is deterministic and knows no tool
names; without rules nothing asks. ``settings.permissions.approval_judge`` adds two model-based
sources of an ``ask`` that need no rule at all (``docs/decisions/2026-10-02-model-approval-judge.md``):

* **model** -- every tool without the MCP ``readOnlyHint`` is offered one extra optional
  argument, :data:`APPROVAL_ARGUMENT`. The acting model fills it with one sentence for the
  user when the call spends money, places an order, sends data and so on. The loop strips the
  argument before anything else sees the request (progress guard, hooks, the tool) and hands
  the sentence to the engine as ``PermissionCheck.model_ask_reason``. No extra model call.
* **classifier** -- a separate model call (:class:`ApprovalClassifier`) judges each
  state-changing call that the rules let through by default: task, page URL, tool, arguments
  and the snapshot line of the target. "Needs approval" becomes
  ``PermissionCheck.classifier_ask_reason``; a failure or timeout asks too (fail closed).

Both can only add an approval: the engine still applies deny rules first, an ``allow`` rule or
a session grant skips the classifier, and a judge ``ask`` is ``always_ask`` (no session grant,
no ``bypass``; ``dont_ask`` turns it into a deny the model reads).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.agent_loop.prompts import (
    APPROVAL_CLASSIFIER_SYSTEM_PROMPT,
    APPROVAL_CLASSIFIER_USER_PROMPT,
)
from src.config import PermissionsSettings
from src.contracts import ApprovalJudgeMode, ToolDef, ToolRequest
from src.harness.tools import to_tool_def, tool_is_read_only
from src.messages import system_message, user_message

#: The optional argument offered to the acting model on every state-changing tool. A protocol
#: name the agent prompt spells out, not a tunable: keep it aligned with ``prompts.py``.
APPROVAL_ARGUMENT = "approval_request"

APPROVAL_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Fill ONLY when this call itself spends or commits money (pay, buy now, place or "
        "confirm an order, subscribe), cancels an order or starts a return, submits personal "
        "or payment data, publishes or sends something on the user's behalf, or changes or "
        "deletes account data. Write one short sentence for the user, in the user's language, "
        "saying what will happen (what, how much, where). The user approves it before the "
        "call runs. Omit it for navigation, search, filters, sorting, reading, opening a "
        "product, and adding items to a cart."
    ),
}


def offer_approval_argument(tools: Sequence[Any]) -> tuple[list[ToolDef], frozenset[str]]:
    """Model-visible defs with :data:`APPROVAL_ARGUMENT` on every state-changing tool.

    Returns the defs and the names of the tools whose own schema already has a property of
    that name: their argument belongs to the tool, so it is neither added nor stripped.
    """

    defs: list[ToolDef] = []
    colliding: set[str] = set()
    for tool in tools:
        tool_def = to_tool_def(tool)
        properties = (tool_def.input_schema or {}).get("properties")
        if isinstance(properties, Mapping) and APPROVAL_ARGUMENT in properties:
            colliding.add(tool_def.name)
            defs.append(tool_def)
            continue
        if tool_is_read_only(tool):
            defs.append(tool_def)
            continue
        schema = copy.deepcopy(dict(tool_def.input_schema or {}))
        if schema.get("type", "object") != "object":
            defs.append(tool_def)
            continue
        schema["type"] = "object"
        schema["properties"] = {
            **dict(schema.get("properties") or {}),
            APPROVAL_ARGUMENT: dict(APPROVAL_ARGUMENT_SCHEMA),
        }
        defs.append(
            ToolDef(name=tool_def.name, description=tool_def.description, input_schema=schema)
        )
    return defs, frozenset(colliding)


def split_approval_request(
    request: Mapping[str, Any],
    colliding: frozenset[str] = frozenset(),
    false_words: frozenset[str] | None = None,
) -> tuple[ToolRequest, str]:
    """Remove :data:`APPROVAL_ARGUMENT` from ``request``; return it and the model's reason.

    The reason is ``""`` when the model did not ask or answered one of ``false_words``
    (default: ``permissions.approval_false_words``). A bare ``true`` gets a generic reason.
    """

    if false_words is None:
        false_words = PermissionsSettings().approval_false_words

    cleaned: dict[str, Any] = dict(request)
    name = str(cleaned.get("name", "") or "")
    args = cleaned.get("args")
    if name in colliding or not isinstance(args, Mapping) or APPROVAL_ARGUMENT not in args:
        return cleaned, ""  # type: ignore[return-value]
    remaining = dict(args)
    value = remaining.pop(APPROVAL_ARGUMENT)
    cleaned["args"] = remaining
    if value is True:
        return cleaned, f"The model asked for approval of {name}."  # type: ignore[return-value]
    text = "" if value is None or value is False else str(value).strip()
    if text.lower() in false_words:
        return cleaned, ""  # type: ignore[return-value]
    return cleaned, text  # type: ignore[return-value]


@dataclass(frozen=True)
class ClassifierJudgment:
    """One classifier answer: ask the human or not; ``error`` set when it failed (asks)."""

    needs_approval: bool
    reason: str = ""
    error: str = ""


class ApprovalClassifier:
    """Ask a chat model whether one state-changing call needs the user's approval."""

    def __init__(
        self,
        llm: Any,
        *,
        timeout_seconds: float | None = None,
        args_chars: int | None = None,
        description_chars: int | None = None,
    ) -> None:
        """An omitted limit takes its ``permissions.classifier_*`` code default."""

        defaults = PermissionsSettings()
        self._llm = llm
        self._timeout = (
            defaults.classifier_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._args_chars = defaults.classifier_args_chars if args_chars is None else args_chars
        self._description_chars = (
            defaults.classifier_description_chars
            if description_chars is None
            else description_chars
        )

    async def judge(
        self,
        *,
        task: str,
        tool: str,
        description: str = "",
        args: Mapping[str, Any] | None = None,
        resources: Mapping[str, str] | None = None,
    ) -> ClassifierJudgment:
        """Never raises: a failure, a timeout or an unreadable answer asks (fail closed)."""

        resources = resources or {}
        prompt = APPROVAL_CLASSIFIER_USER_PROMPT.format(
            task=task.strip() or "(none)",
            url=resources.get("url") or resources.get("domain") or "(unknown)",
            tool=tool,
            description=_clip(description, self._description_chars) or "(none)",
            args=_clip(_dump(args or {}), self._args_chars),
            target=resources.get("target") or "(none)",
        )
        messages = [system_message(APPROVAL_CLASSIFIER_SYSTEM_PROMPT), user_message(prompt)]
        try:
            response = await asyncio.wait_for(self._llm.complete(messages), self._timeout)
        except Exception as exc:  # noqa: BLE001 - authorization must fail closed
            return _failed(tool, type(exc).__name__)
        return _parse(str(getattr(response, "content", response) or ""), tool)


@dataclass(frozen=True)
class ApprovalJudge:
    """Which model judgments the loop collects (``settings.permissions.approval_judge``).

    The default judges nothing, so evals and tests that pass no judge see no change.
    """

    mode: ApprovalJudgeMode = "off"
    classifier: ApprovalClassifier | None = None
    #: Values of :data:`APPROVAL_ARGUMENT` that mean "no approval needed".
    false_words: frozenset[str] = field(
        default_factory=lambda: PermissionsSettings().approval_false_words
    )

    @property
    def model_signal(self) -> bool:
        return self.mode in ("model", "both")

    @property
    def classifies(self) -> bool:
        return self.mode in ("classifier", "both") and self.classifier is not None

    @classmethod
    def from_settings(
        cls,
        settings: PermissionsSettings,
        *,
        llm: Any,
        llm_factory: Callable[..., Any] | None = None,
    ) -> ApprovalJudge:
        """Build from ``settings.permissions``; the classifier reuses ``llm`` unless
        ``classifier_model`` names another model and ``llm_factory`` can build it."""

        classifier = None
        if settings.approval_judge in ("classifier", "both"):
            classifier_llm = llm
            if settings.classifier_model and llm_factory is not None:
                classifier_llm = llm_factory(model=settings.classifier_model, temperature=0.0)
            classifier = ApprovalClassifier(
                classifier_llm,
                timeout_seconds=settings.classifier_timeout_seconds,
                args_chars=settings.classifier_args_chars,
                description_chars=settings.classifier_description_chars,
            )
        return cls(
            mode=settings.approval_judge,
            classifier=classifier,
            false_words=settings.approval_false_words,
        )


def _parse(content: str, tool: str) -> ClassifierJudgment:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        return _failed(tool, "no JSON answer")
    try:
        data = json.loads(content[start : end + 1])
    except (ValueError, TypeError):
        return _failed(tool, "unreadable JSON answer")
    if not isinstance(data, dict):
        return _failed(tool, "unreadable JSON answer")
    raw = data.get("approval", data.get("needs_approval"))
    if isinstance(raw, str):
        raw = {"true": True, "yes": True, "false": False, "no": False}.get(raw.strip().lower())
    if not isinstance(raw, bool):
        return _failed(tool, "no approval verdict")
    reason = str(data.get("reason", "") or "").strip()
    if raw and not reason:
        reason = f"The approval classifier flagged {tool}."
    return ClassifierJudgment(needs_approval=raw, reason=reason)


def _failed(tool: str, error: str) -> ClassifierJudgment:
    return ClassifierJudgment(
        needs_approval=True,
        reason=f"The approval classifier could not judge {tool} ({error}), so it needs approval.",
        error=error,
    )


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = [
    "APPROVAL_ARGUMENT",
    "APPROVAL_ARGUMENT_SCHEMA",
    "ApprovalClassifier",
    "ApprovalJudge",
    "ClassifierJudgment",
    "offer_approval_argument",
    "split_approval_request",
]
