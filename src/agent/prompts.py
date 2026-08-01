"""Compatibility exports for the agent prompt boundary."""

from src.agent_loop.prompts import AGENT_SYSTEM_PROMPT
from src.agent_loop.prompts import AGENT_USER_PROMPT
from src.agent_loop.prompts import BROWSER_CONTRACT_PROMPT
from src.agent_loop.prompts import COMPLETION_PROMPT
from src.agent_loop.prompts import CORE_RUNTIME_PROMPT
from src.agent_loop.prompts import LOOP_GUARD_PROMPT
from src.agent_loop.prompts import OUTPUT_FORMAT_PROMPT
from src.agent_loop.prompts import render_compatibility_system_prompt

__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "AGENT_USER_PROMPT",
    "BROWSER_CONTRACT_PROMPT",
    "COMPLETION_PROMPT",
    "CORE_RUNTIME_PROMPT",
    "LOOP_GUARD_PROMPT",
    "OUTPUT_FORMAT_PROMPT",
    "render_compatibility_system_prompt",
]
