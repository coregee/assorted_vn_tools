"""Local, context-aware visual-novel translation editor."""

__version__ = "0.1.0"

from .server import create_server, run

__all__ = ("__version__", "create_server", "run")

