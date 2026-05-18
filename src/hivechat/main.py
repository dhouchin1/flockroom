"""Entry point: MCP stdio server or HTTP server.

hivechat mcp           — run as MCP stdio server (add to Claude Code / Cursor config)
hivechat serve         — run HTTP REST+SSE server for dashboard integration
hivechat serve --port  — custom port (default 8090)
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hivechat",
        description="Shared chat rooms for multiple AI agents",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("mcp", help="Run as MCP stdio server (default)")

    http_p = sub.add_parser("serve", help="Run HTTP REST+SSE server for dashboard")
    http_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    http_p.add_argument("--port", type=int, default=8090, help="Bind port (default: 8090)")

    # Bridge agent — spawned by brain-bridge launch-team for non-claude or wait-for-role agents
    agent_p = sub.add_parser("agent", help="Run a model as a hive room participant")
    agent_p.add_argument("--room", required=True)
    agent_p.add_argument("--name", required=True)
    agent_p.add_argument("--role", default="assistant")
    agent_p.add_argument("--backend", choices=["claude", "openai"], default="claude")
    agent_p.add_argument("--model", default=None)
    agent_p.add_argument("--base-url", default="http://localhost:11434")
    agent_p.add_argument("--api-key", default="ollama")
    agent_p.add_argument("--wait-for-role", default=None)
    agent_p.add_argument("--wait-timeout", type=int, default=600)
    agent_p.add_argument("--prompt", required=True)

    args = parser.parse_args()

    if args.cmd == "serve":
        import uvicorn

        from .api import app

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    elif args.cmd == "agent":
        from .bridge import main

        main()
    else:
        # Default to MCP stdio — works with no subcommand too
        from .mcp_server import mcp

        mcp.run()


if __name__ == "__main__":
    main()
