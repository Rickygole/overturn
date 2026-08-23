"""The human approval interface.

The gate a drafted appeal passes through before it is transmitted to an
insurer. Server-rendered HTML with no client framework and no external assets,
so the screen that carries the most consequential decision in the system has the
fewest moving parts.
"""

from services.approval_ui.app import create_app

__all__ = ["create_app"]
