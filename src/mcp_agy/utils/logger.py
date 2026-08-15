"""Logging utilities for mcp_agy ensuring strict stderr routing.

MCP servers communicating over stdio JSON-RPC require sys.stdout to remain
completely unpolluted by log output, debug prints, or framework banners.
This module guarantees that all logging across mcp_agy is directed exclusively
to sys.stderr with logger propagation disabled.
"""

from __future__ import annotations

import logging
import sys
from typing import Union

DEFAULT_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
ROOT_LOGGER_NAME = "mcp_agy"


def setup_logger(
    name: str = ROOT_LOGGER_NAME,
    level: Union[str, int] = logging.INFO,
    log_format: str = DEFAULT_LOG_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
) -> logging.Logger:
    """Create or configure a logger that writes exclusively to sys.stderr.

    Args:
        name: Name of the logger (defaults to 'mcp_agy').
        level: Logging level (e.g., 'DEBUG', 'INFO', logging.INFO).
        log_format: Format string for log records.
        date_format: Date format string for timestamps.

    Returns:
        logging.Logger: Configured logger instance with stderr StreamHandler and propagate=False.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Remove any existing handlers to prevent duplicate logging
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    # Attach a dedicated stderr stream handler
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    formatter = logging.Formatter(log_format, datefmt=date_format)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def get_logger(name: str = ROOT_LOGGER_NAME) -> logging.Logger:
    """Retrieve or initialize a logger instance for mcp_agy modules.

    If the logger name does not start with 'mcp_agy', it will be automatically
    namespaced under 'mcp_agy.<name>' to inherit strict stderr logging and propagation rules.

    Args:
        name: Logger name (e.g., 'mcp_agy.server', 'server', 'mcp_agy.backend').

    Returns:
        logging.Logger: Safe stderr logger instance.
    """
    if name != ROOT_LOGGER_NAME and not name.startswith(f"{ROOT_LOGGER_NAME}."):
        full_name = f"{ROOT_LOGGER_NAME}.{name}"
    else:
        full_name = name

    # Ensure root package logger is initialized with stderr handler
    root_pkg_logger = logging.getLogger(ROOT_LOGGER_NAME)
    if not root_pkg_logger.handlers:
        setup_logger(ROOT_LOGGER_NAME)

    return logging.getLogger(full_name)


def configure_logging(
    level: Union[str, int] = "INFO",
    name: str = ROOT_LOGGER_NAME,
) -> logging.Logger:
    """Globally configure mcp_agy logging on sys.stderr.

    Args:
        level: Logging level (e.g. 'DEBUG', 'INFO', logging.INFO).
        name: Root logger name to configure.

    Returns:
        logging.Logger: The configured root logger.
    """
    return setup_logger(name=name, level=level)


# Alias for backward compatibility
setup_logging = configure_logging
