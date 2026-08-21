"""Command line entry point: ``mec-cast-admin``."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("mec_cast_admin.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mec-cast-admin",
        description="Run the mec-cast admin service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the HTTP and WebSocket service.")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8099)
    serve.add_argument("--reload", action="store_true", help="Reload on source changes.")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        # A single worker, always: the registry of connected nodes and the run
        # state machine are in-process. A second worker would hold a second,
        # divergent view of the fleet.
        uvicorn.run(
            "mec_cast_admin.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=1,
        )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
