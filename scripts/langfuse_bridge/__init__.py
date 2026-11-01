"""The Langfuse bridge: batch folders -> Langfuse datasets, experiments and scores.

Everything that knows Langfuse lives here (endpoints, ``langfuse.*`` attributes, Basic auth,
score mapping); the neutral records it maps come from ``src/agent_loop/platform_records.py``
and ``src/harness/otel_spans.py``. The package is not named ``langfuse`` so that running
``scripts/langfuse_sync.py`` (which puts ``scripts/`` on ``sys.path``) never shadows a real
``langfuse`` package. See docs/development/eval-platform.md.
"""
