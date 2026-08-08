"""Adapters that translate transitional runtime objects into legacy updates."""

from src.agent_loop.adapters.langgraph import proposed_action_to_legacy_update

__all__ = ["proposed_action_to_legacy_update"]
