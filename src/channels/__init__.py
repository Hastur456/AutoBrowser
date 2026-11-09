"""Chat channel adapters of the stand (``autobrowser serve``).

Each module speaks one messaging service and plugs into the stand's neutral ports
(``src.serve.notify.NotificationChannel``, ``src.serve.commands.CommandPort``). Only the CLI
composition (``src/cli/``) imports this package; vendor names and URLs live here only.
"""
