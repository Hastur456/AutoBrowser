"""Provider adapters for the provider-neutral chat contract.

Each concrete provider lives behind :class:`src.llm.ChatModel` and owns nothing but wire
serialization: map :class:`~src.messages.Message` / :class:`~src.contracts.ToolDef` to the
provider API and parse the reply back into a :class:`~src.llm.ModelResponse`. No policy or
execution decisions live here.
"""
