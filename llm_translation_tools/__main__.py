"""Command-line entry point for ``python -m llm_translation_tools``."""

import argparse

from .project import ProjectError
from .server import DEFAULT_HOST, DEFAULT_PORT, run


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local LM Studio visual-novel translation editor")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="loopback bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="local port, or 0 for an available port (default: %(default)s)")
    parser.add_argument("--project", "--game", dest="project",
                        help="game/tool root or extracted script directory to open")
    parser.add_argument("--script-dir",
                        help="script directory inside --project (default: script/ or project itself)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the editor in the default browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.script_dir and not args.project:
        parser.error("--script-dir requires --project")
    try:
        run(args.host, args.port, not args.no_browser, args.project, args.script_dir)
    except (ValueError, OSError, ProjectError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
