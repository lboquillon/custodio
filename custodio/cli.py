# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Command-line entry point for Custodio.

Installed as the ``custodio`` console script (see pyproject.toml), and also
runnable as ``python -m custodio``.
"""

from __future__ import annotations

import argparse
import os
import sys


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("custodio")
    except Exception:  # noqa: BLE001
        return "1.0.0"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custodio",
        description="Transparent PII-anonymizing reverse proxy for the Anthropic API.",
    )
    parser.add_argument("--version", action="version", version=f"custodio {_version()}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the proxy server (default command)")
    serve.add_argument("--host", default=os.getenv("CUSTODIO_HOST", "127.0.0.1"),
                       help="bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=int(os.getenv("CUSTODIO_PORT", "3000")),
                       help="bind port (default: 3000)")
    serve.add_argument("--engine", choices=["presidio", "regex"], default=None,
                       help="detection engine (overrides CUSTODIO_ENGINE)")
    serve.add_argument("--upstream", default=None,
                       help="upstream base URL (overrides CUSTODIO_UPSTREAM)")
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    serve.add_argument("--log-level", default=os.getenv("CUSTODIO_LOG_LEVEL", "info"))
    return parser


def _serve(args: argparse.Namespace) -> int:
    if args.engine:
        os.environ["CUSTODIO_ENGINE"] = args.engine
    if args.upstream:
        os.environ["CUSTODIO_UPSTREAM"] = args.upstream

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("error: uvicorn is not installed. Install with: pip install custodio",
              file=sys.stderr)
        return 1

    engine = os.getenv("CUSTODIO_ENGINE", "presidio")
    upstream = os.getenv("CUSTODIO_UPSTREAM", "https://api.anthropic.com")
    print(f"custodio {_version()}  engine={engine}  upstream={upstream}")
    if args.host in ("0.0.0.0", "::"):
        # In a container the bind port is internal; the user reaches it via the
        # host port they mapped, which this process cannot know. Don't print a
        # misleading localhost:<internal-port> URL.
        print(f"listening on port {args.port} inside the container")
        print(f"  reach it at the HOST port you mapped to container port {args.port}")
        print(f"  e.g. `-p 3100:{args.port}`  ->  http://localhost:3100")
        print("  then: export ANTHROPIC_BASE_URL=http://localhost:<hostport>")
        print("        dashboard http://localhost:<hostport>/custodio/dashboard")
    else:
        print(f"listening on http://{args.host}:{args.port}")
        print(f"dashboard:   http://{args.host}:{args.port}/custodio/dashboard")
        print(f"point your client at it:  export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}")

    uvicorn.run(
        "custodio.proxy:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `serve` is the default command: `custodio` and `custodio --port 8080`
    # both behave like `custodio serve ...`. -h/--help/--version pass through.
    if not argv:
        argv = ["serve"]
    elif argv[0] != "serve" and argv[0] not in ("-h", "--help", "--version"):
        argv = ["serve", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
