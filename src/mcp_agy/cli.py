"""Command-line interface entrypoints for mcp-agy."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from mcp_agy import __description__, __title__, __version__
from mcp_agy.core.backend import AGYBackend, get_backend, set_backend
from mcp_agy.core.mock_backend import MockAGYBackend
from mcp_agy.server import create_mcp_server, mcp
from mcp_agy.utils.logger import configure_logging, get_logger

logger = get_logger("mcp_agy.cli")

_TRUTHY = ("1", "true", "yes", "on")
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _env_flag(name: str) -> bool:
    """Read a boolean env var using the same truthy vocabulary as MCP_AGY_AUTO_FALLBACK."""
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def _env_log_level() -> Optional[str]:
    """Read MCP_AGY_LOG_LEVEL, ignoring unset/blank/unrecognized values.

    An unrecognized level is ignored rather than raising: the server is usually launched by
    an MCP client that only shows a dead process, so a typo'd log level must not be the
    reason the whole server refuses to boot.
    """
    raw = (os.environ.get("MCP_AGY_LOG_LEVEL") or "").strip().upper()
    if not raw:
        return None
    if raw not in _VALID_LOG_LEVELS:
        logger.warning(
            f"Ignoring MCP_AGY_LOG_LEVEL={raw!r}: expected one of {', '.join(_VALID_LOG_LEVELS)}."
        )
        return None
    return raw


def print_banner(
    transport: str,
    host: str,
    port: int,
    backend: str,
    debug: bool,
    stream: Optional[Any] = None,
) -> None:
    """Render startup banner and connection parameters strictly to sys.stderr.

    Guarantees sys.stdout remains pristine and free from any non-JSON-RPC framing
    or ANSI escape bytes during stdio communication.
    """
    target_file = stream if stream is not None else sys.stderr
    console = Console(file=target_file, highlight=False)

    title_text = Text()
    title_text.append("Google Antigravity MCP Server\n", style="bold cyan")
    title_text.append(f"Version: {__version__} | Transport: {transport}\n", style="green")
    if transport in ("sse", "streamable-http"):
        title_text.append(f"Listening on: http://{host}:{port}\n", style="bold yellow")
    title_text.append(f"Backend: {backend} | Debug: {debug}", style="magenta")

    panel = Panel(
        title_text,
        title=f"[bold white]{__title__}[/bold white]",
        border_style="bright_blue",
        subtitle="Autonomous Coding Worker for AI Architects",
    )
    console.print(panel)


@click.command(
    name="mcp-agy",
    help="FastMCP Server exposing Google Antigravity (AGY) as an autonomous coding worker for AI architect agents.",
)
@click.version_option(
    version=__version__,
    prog_name="mcp-agy",
    message="%(prog)s %(version)s",
)
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "sse", "streamable-http"], case_sensitive=False),
    default="stdio",
    show_default=True,
    help="Transport mechanism for MCP communication (stdio, sse, streamable-http).",
)
@click.option(
    "--host",
    "-h",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="Host interface to bind on (for SSE or streamable-http transports).",
)
@click.option(
    "--port",
    "-p",
    type=int,
    default=8000,
    show_default=True,
    help="Port number to listen on (for SSE or streamable-http transports).",
)
@click.option(
    "--debug",
    "-d",
    is_flag=True,
    default=False,
    help="Enable verbose DEBUG logging to stderr.",
)
@click.option(
    "--log-level",
    "-l",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    show_default=False,
    help="Logging verbosity level on stderr (DEBUG, INFO, WARNING, ERROR).",
)
@click.option(
    "--backend",
    "-b",
    type=click.Choice(["sdk", "cli", "mock", "auto"], case_sensitive=False),
    default=None,
    show_default="auto",
    help="Preferred AGY execution engine (sdk: python SDK, cli: agy subprocess, mock: simulated, auto: 3-tier fallback).",
)
@click.option(
    "--banner/--no-banner",
    default=True,
    show_default=True,
    help="Display rich startup information banner on stderr.",
)
def main(
    transport: str,
    host: str,
    port: int,
    debug: bool,
    log_level: Optional[str],
    backend: Optional[str],
    banner: bool,
) -> None:
    """Parse CLI arguments, configure stderr logging, and run the FastMCP server."""
    transport_norm = transport.lower()

    # 1. Configure logging strictly on sys.stderr.
    #
    # MCP clients (Claude Desktop, Cursor, Cline, Roo Code) launch this server from a JSON
    # config whose only tunable is the `env` block — they cannot append CLI flags. So
    # MCP_AGY_LOG_LEVEL and MCP_AGY_DEBUG have to work, and every client config shipped in
    # configs/ sets MCP_AGY_LOG_LEVEL. Precedence is explicit flag > env > default, so a
    # human typing `mcp-agy --log-level DEBUG` is never overruled by a stale env var.
    env_level = _env_log_level()
    env_debug = _env_flag("MCP_AGY_DEBUG")
    debug = debug or env_debug

    if log_level is not None:
        effective_level = getattr(logging, log_level.upper(), logging.INFO)
    elif env_level is not None:
        effective_level = getattr(logging, env_level, logging.INFO)
    elif debug:
        effective_level = logging.DEBUG
    else:
        effective_level = logging.INFO

    configure_logging(level=effective_level)

    # 2. Configure backend engine preference
    if backend is not None:
        backend_norm = backend.lower()
        if backend_norm == "mock":
            os.environ["MCP_AGY_BACKEND"] = "mock"
            set_backend(MockAGYBackend())
        elif backend_norm in ("sdk", "cli"):
            os.environ["MCP_AGY_BACKEND"] = backend_norm
            from mcp_agy.core.backend_manager import BackendManager, set_backend_manager

            set_backend_manager(BackendManager(preferred_backend=backend_norm))
        elif backend_norm == "auto":
            os.environ.pop("MCP_AGY_BACKEND", None)
            from mcp_agy.core.backend_manager import reset_backend_manager

            reset_backend_manager()
    else:
        backend_norm = os.environ.get("MCP_AGY_BACKEND", "auto").lower()
        if backend_norm == "mock":
            set_backend(MockAGYBackend())
        elif backend_norm in ("sdk", "cli"):
            from mcp_agy.core.backend_manager import BackendManager, set_backend_manager

            set_backend_manager(BackendManager(preferred_backend=backend_norm))

    # 3. Output banner on stderr if requested
    if banner:
        print_banner(
            transport=transport_norm,
            host=host,
            port=port,
            backend=backend_norm,
            debug=debug,
        )

    logger.info(
        f"Booting mcp-agy FastMCP server (transport={transport_norm}, "
        f"host={host}, port={port}, backend={backend_norm}, debug={debug}, log_level={effective_level})"
    )

    # 4. Create and configure FastMCP instance
    # FastMCP's own logger follows the level resolved above, so raising verbosity through
    # the environment lights up the framework's frames too, not just mcp_agy's.
    server_instance = create_mcp_server(
        host=host,
        port=port,
        debug=debug,
        log_level=logging.getLevelName(effective_level),
    )

    # 5. Execute server transport loop
    if transport_norm == "stdio":
        server_instance.run(transport="stdio")
    elif transport_norm == "sse":
        server_instance.run(transport="sse")
    elif transport_norm == "streamable-http":
        server_instance.run(transport="streamable-http")
    else:
        server_instance.run(transport=transport_norm)


if __name__ == "__main__":
    main()
