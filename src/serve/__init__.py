"""The stand, ``autobrowser serve``: jobs run by triggers over a :class:`SessionController`.

A client of the session like the terminal UI (``docs/development/serve-mode.md``): it imports
``src.ui``, ``src.contracts``, ``src.config`` and ``src.agent_loop.events`` /
``src.agent_loop.recordings``, never the engine, the browser layer, the harness, the CLI or
the providers (``tests/test_serve_boundaries.py``). ``src/cli/serve.py`` composes it.
"""
