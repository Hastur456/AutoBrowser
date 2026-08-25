"""Engine-native adapters package.

The transitional LangGraph adapter (``proposed_action_to_legacy_update``) has been removed:
the engine now maps :class:`~src.agent_loop.actions.ProposedAction` values to
:class:`~src.agent_loop.execution.state.LoopState` updates natively inside
:meth:`~src.agent_loop.execution.loop.TurnController._classify_action`.
"""

__all__: list[str] = []
