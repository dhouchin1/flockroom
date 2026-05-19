"""Entry point: MCP stdio server or HTTP server.

hivechat mcp           — run as MCP stdio server (add to Claude Code / Cursor config)
hivechat serve         — run HTTP REST+SSE server for dashboard integration
hivechat serve --port  — custom port (default 8090)
hivechat agent ...     — bridge agent runner (see hivechat/bridge.py for flags)
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    # Fast path: `hivechat agent ...` — bypass our argparser entirely and let
    # bridge.main() own the full flag surface. Avoids duplicating every option
    # here whenever bridge gains a new one (e.g. --loop, --loop-max).
    if len(sys.argv) >= 2 and sys.argv[1] == "agent":
        from .bridge import main as bridge_main

        bridge_main()
        return

    parser = argparse.ArgumentParser(
        prog="hivechat",
        description="Shared chat rooms for multiple AI agents",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("mcp", help="Run as MCP stdio server (default)")

    http_p = sub.add_parser("serve", help="Run HTTP REST+SSE server for dashboard")
    http_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    http_p.add_argument("--port", type=int, default=8090, help="Bind port (default: 8090)")

    # Listed only for `hivechat --help` discoverability; never actually parsed
    # because the fast path above intercepts `agent` first.
    sub.add_parser(
        "agent",
        help="Run a model as a hive room participant — see hivechat agent --help",
    )

    args = parser.parse_args()

    if args.cmd == "serve":
        import uvicorn

        from .api import app

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        # Default to MCP stdio — works with no subcommand too
        from .mcp_server import mcp

        mcp.run()


if __name__ == "__main__":
    main()
