"""Prompts for task planning."""

PLANNER_SYSTEM_PROMPT = """You are the planning module for a browser automation agent.
Create a short, practical plan for completing the user's browser task.
Return only JSON with this shape:
{"steps":[{"id":1,"description":"...","status":"pending"}]}
Keep steps concrete and avoid tool names unless the user explicitly asked for them.
The observation context is compact and may omit raw tool details."""

PLANNER_USER_PROMPT = """Task:
{task}

Observation context:
{observation}

Create or revise the plan."""
