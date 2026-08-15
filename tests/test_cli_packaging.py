"""Comprehensive automated test suite for Click CLI runner, options, stdout cleanliness, and packaging integrity."""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import sys
import tomllib
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner
import pytest

import mcp_agy
from mcp_agy import __version__
from mcp_agy.cli import main, print_banner
from mcp_agy.core.backend import MockAGYBackend, get_backend
from mcp_agy.utils.logger import get_logger


@pytest.fixture(autouse=True)
def preserve_env_and_backend_state():
    """Preserve environment variables and backend manager state across CLI tests."""
    old_env = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(old_env)
    from mcp_agy.core.backend import reset_backend
    from mcp_agy.core.backend_manager import reset_backend_manager
    reset_backend()
    reset_backend_manager()


@pytest.fixture
def runner():
    """Provides Click CliRunner for isolated CLI invocations."""
    return CliRunner()


# ============================================================================
# 1. CLI Help and Version Flag Tests
# ============================================================================


class TestCliHelpAndVersion:
    """Verifies help output, version output, and option documentation."""

    def test_cli_help_displays_options(self, runner):
        """Verifies --help outputs all supported options and exits with 0."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "--transport" in result.output
        assert "--host" in result.output
        assert "--port" in result.output
        assert "--debug" in result.output
        assert "--backend" in result.output
        assert "--banner" in result.output
        assert "--version" in result.output

    def test_cli_version_flag(self, runner):
        """Verifies --version outputs package name and version string."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert f"mcp-agy {__version__}" in result.output


# ============================================================================
# 2. CLI Validation and Error Handling Tests
# ============================================================================


class TestCliValidationAndErrorHandling:
    """Verifies validation of CLI options and error exit codes."""

    def test_invalid_transport_choice(self, runner):
        """Verifies unsupported transport choice fails with exit code 2."""
        result = runner.invoke(main, ["--transport", "invalid_transport"])
        assert result.exit_code == 2
        assert "Invalid value for '--transport'" in result.output or "invalid_transport" in result.output

    def test_invalid_backend_choice(self, runner):
        """Verifies unsupported backend choice fails with exit code 2."""
        result = runner.invoke(main, ["--backend", "unsupported_backend"])
        assert result.exit_code == 2
        assert "Invalid value for '--backend'" in result.output or "unsupported_backend" in result.output

    def test_invalid_port_type(self, runner):
        """Verifies non-integer port fails with exit code 2."""
        result = runner.invoke(main, ["--port", "not_a_number"])
        assert result.exit_code == 2
        assert "is not a valid integer" in result.output or "not_a_number" in result.output


# ============================================================================
# 3. CLI Logging and Backend Configuration Tests
# ============================================================================


class TestCliLoggingAndBackendConfiguration:
    """Verifies CLI flag effects on logging levels and backend selection."""

    def test_cli_debug_flag_configures_logging(self, runner):
        """Verifies --debug flag sets log level to DEBUG."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--debug", "--no-banner"])
            assert result.exit_code == 0
            mock_create_server.assert_called_once_with(
                host="127.0.0.1",
                port=8000,
                debug=True,
                log_level="DEBUG",
            )

    def test_cli_backend_mock_selection(self, runner):
        """Verifies --backend mock registers MockAGYBackend."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--backend", "mock", "--no-banner"])
            assert result.exit_code == 0
            assert os.environ.get("MCP_AGY_BACKEND") == "mock"
            assert isinstance(get_backend(), MockAGYBackend)

    def test_cli_backend_sdk_selection(self, runner):
        """Verifies --backend sdk sets MCP_AGY_BACKEND=sdk."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--backend", "sdk", "--no-banner"])
            assert result.exit_code == 0
            assert os.environ.get("MCP_AGY_BACKEND") == "sdk"

    def test_cli_backend_cli_selection(self, runner):
        """Verifies --backend cli sets MCP_AGY_BACKEND=cli."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--backend", "cli", "--no-banner"])
            assert result.exit_code == 0
            assert os.environ.get("MCP_AGY_BACKEND") == "cli"

    def test_cli_backend_auto_selection(self, runner):
        """Verifies --backend auto removes MCP_AGY_BACKEND override."""
        os.environ["MCP_AGY_BACKEND"] = "mock"
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--backend", "auto", "--no-banner"])
            assert result.exit_code == 0
            assert "MCP_AGY_BACKEND" not in os.environ


# ============================================================================
# 4. CLI Transport Dispatching Tests
# ============================================================================


class TestCliTransportDispatching:
    """Verifies that CLI dispatches correctly to FastMCP.run for each transport."""

    def test_cli_stdio_transport_dispatch(self, runner):
        """Verifies stdio transport executes server.run(transport='stdio')."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--transport", "stdio", "--no-banner"])
            assert result.exit_code == 0
            mock_inst.run.assert_called_once_with(transport="stdio")

    def test_cli_sse_transport_dispatch(self, runner):
        """Verifies sse transport passes host/port and runs SSE loop."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--transport", "sse", "--host", "0.0.0.0", "--port", "9090", "--no-banner"])
            assert result.exit_code == 0
            mock_create_server.assert_called_once_with(
                host="0.0.0.0",
                port=9090,
                debug=False,
                log_level="INFO",
            )
            mock_inst.run.assert_called_once_with(transport="sse")

    def test_cli_streamable_http_transport_dispatch(self, runner):
        """Verifies streamable-http transport executes server.run(transport='streamable-http')."""
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_inst = MagicMock()
            mock_create_server.return_value = mock_inst

            result = runner.invoke(main, ["--transport", "streamable-http", "--no-banner"])
            assert result.exit_code == 0
            mock_inst.run.assert_called_once_with(transport="streamable-http")


# ============================================================================
# 5. Stderr Banner and Output Hygiene Tests
# ============================================================================


class TestCliStdoutCleanlinessAndBanner:
    """Verifies that banners and runtime logs strictly output to stderr, preserving clean stdout."""

    def test_print_banner_renders_clean_panel(self):
        """Verifies print_banner outputs rich panel text to provided stream."""
        buffer = io.StringIO()
        print_banner(
            transport="stdio",
            host="127.0.0.1",
            port=8000,
            backend="auto",
            debug=False,
            stream=buffer,
        )
        output = buffer.getvalue()
        assert "Google Antigravity MCP Server" in output
        assert f"Version: {__version__}" in output
        assert "Transport: stdio" in output
        assert "Backend: auto" in output

    def test_print_banner_includes_url_for_sse(self):
        """Verifies print_banner includes HTTP url when transport is SSE."""
        buffer = io.StringIO()
        print_banner(
            transport="sse",
            host="localhost",
            port=8888,
            backend="sdk",
            debug=True,
            stream=buffer,
        )
        output = buffer.getvalue()
        assert "Listening on: http://localhost:8888" in output


# ============================================================================
# 6. Packaging Metadata and File Integrity Tests
# ============================================================================


class TestPackagingMetadataAndIntegrity:
    """Verifies pyproject.toml, requirements.txt, and client config files validity."""

    def test_pyproject_toml_validity(self):
        """Verifies pyproject.toml exists and conforms to PEP 621 metadata standard."""
        repo_root = Path(__file__).parent.parent
        pyproject_path = repo_root / "pyproject.toml"
        assert pyproject_path.exists(), "pyproject.toml is missing"

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        assert data["build-system"]["build-backend"] == "hatchling.build"
        assert data["project"]["name"] == "mcp-agy"
        assert data["project"]["version"] == "0.1.0"
        deps = data["project"]["dependencies"]
        mcp_req = next(
            (d for d in deps if d.startswith(("mcp>", "mcp=", "mcp<", "mcp~", "mcp!"))),
            None,
        )
        assert mcp_req is not None, f"no `mcp` requirement in dependencies: {deps}"
        assert ">=1.26.0" in mcp_req, f"mcp floor must stay at 1.26.0, got {mcp_req!r}"
        # The upper bound is the substance of this assertion, not decoration. mcp 2.0 deleted
        # `mcp.server.fastmcp` and renamed FastMCP to MCPServer, so an unbounded `mcp>=1.26.0`
        # resolves into 2.x and every import in src/ and tests/ dies at collection time with
        # ModuleNotFoundError. This assertion used to pin the exact string "mcp>=1.26.0", which
        # checked the spelling while permitting exactly the resolution that breaks the package.
        assert "<2" in mcp_req, (
            f"mcp must be capped below 2.0 or the FastMCP imports in src/ break, got {mcp_req!r}"
        )
        assert "click>=8.0.0" in deps
        assert "mcp-agy" in data["project"]["scripts"]
        assert data["project"]["scripts"]["mcp-agy"] == "mcp_agy.cli:main"

    def test_requirements_txt_matches_dependencies(self):
        """Verifies requirements.txt exists and contains core dependencies."""
        repo_root = Path(__file__).parent.parent
        req_path = repo_root / "requirements.txt"
        assert req_path.exists(), "requirements.txt is missing"

        content = req_path.read_text(encoding="utf-8")
        assert "mcp" in content
        # Same hazard as pyproject.toml: an uncapped `mcp` here installs 2.x, which has no
        # `mcp.server.fastmcp`. Keep the two files agreeing on the cap.
        assert "<2" in content, "requirements.txt must cap mcp below 2.0, matching pyproject.toml"
        assert "pydantic" in content
        assert "rich" in content
        assert "click" in content

    def test_client_configs_are_valid_json(self):
        """Verifies all 5 client config files in configs/ exist and are valid JSON."""
        repo_root = Path(__file__).parent.parent
        config_files = [
            "configs/claude_desktop_config.json",
            "configs/cursor_mcp.json",
            "configs/cline_mcp_settings.json",
            "configs/roo_code_mcp_settings.json",
            "configs/claude_code_mcp.json",
        ]
        for cfg in config_files:
            cfg_path = repo_root / cfg
            assert cfg_path.exists(), f"Missing config file: {cfg}"
            with open(cfg_path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            assert "mcpServers" in parsed
            assert "mcp-agy" in parsed["mcpServers"]
            server_cfg = parsed["mcpServers"]["mcp-agy"]
            assert "command" in server_cfg

    def test_claude_code_config_raises_the_call_timeout(self):
        """Verifies the Claude Code config still carries a timeout above the client default.

        Claude Code caps a single tool call at 60s unless the server entry raises it, and a
        real AGY task runs for minutes. Without this key every long call dies with
        `Error: Request timed out` and the work is discarded -- the reason this config file
        exists at all. A silent drop of the key would restore that failure with no test to
        notice, so the value is asserted, not merely its presence.
        """
        repo_root = Path(__file__).parent.parent
        parsed = json.loads((repo_root / "configs/claude_code_mcp.json").read_text(encoding="utf-8"))
        timeout_ms = parsed["mcpServers"]["mcp-agy"].get("timeout")
        assert timeout_ms is not None, "claude_code_mcp.json must set an explicit timeout"
        assert timeout_ms > 60_000, (
            f"timeout {timeout_ms}ms is at or below Claude Code's 60s default, "
            "so a real AGY task would still be cut off"
        )

    def test_main_module_delegates_to_cli_main(self):
        """Verifies mcp_agy.__main__ imports cli.main."""
        import mcp_agy.__main__ as main_mod

        assert hasattr(main_mod, "main")
        assert main_mod.main == main


# ============================================================================
# 7. Environment Variable Configuration Tests
# ============================================================================


class TestEnvironmentVariableConfiguration:
    """Verifies env-only configuration, the sole channel MCP clients have.

    Claude Desktop, Cursor, Cline and Roo Code launch this server from a JSON config whose
    only tunable is the `env` block - they cannot append CLI flags. Every variable the README
    documents therefore has to be honored here, or a user's config silently does nothing.
    """

    @pytest.mark.parametrize(
        "env_level,expected",
        [("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")],
    )
    def test_env_log_level_is_honored(self, runner, env_level, expected):
        """Verifies MCP_AGY_LOG_LEVEL sets the effective logging level."""
        os.environ["MCP_AGY_LOG_LEVEL"] = env_level
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["log_level"] == expected
            assert logging.getLogger("mcp_agy").level == getattr(logging, expected)

    def test_env_log_level_is_case_insensitive(self, runner):
        """Verifies a lowercase MCP_AGY_LOG_LEVEL is accepted."""
        os.environ["MCP_AGY_LOG_LEVEL"] = "debug"
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["log_level"] == "DEBUG"

    def test_explicit_log_level_flag_overrides_env(self, runner):
        """Verifies an explicit --log-level flag wins over MCP_AGY_LOG_LEVEL."""
        os.environ["MCP_AGY_LOG_LEVEL"] = "ERROR"
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--log-level", "DEBUG", "--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["log_level"] == "DEBUG"

    def test_invalid_env_log_level_is_ignored_not_fatal(self, runner):
        """Verifies a mistyped MCP_AGY_LOG_LEVEL falls back to INFO instead of killing the server.

        The server is launched by a client that only surfaces a dead process, so a bad log
        level must never be the reason it refuses to boot.
        """
        os.environ["MCP_AGY_LOG_LEVEL"] = "VERBOSE"
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["log_level"] == "INFO"

    def test_blank_env_log_level_falls_back_to_default(self, runner):
        """Verifies an empty MCP_AGY_LOG_LEVEL is treated as unset."""
        os.environ["MCP_AGY_LOG_LEVEL"] = "   "
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["log_level"] == "INFO"

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
    def test_env_debug_enables_debug_mode(self, runner, truthy):
        """Verifies MCP_AGY_DEBUG turns on debug mode and DEBUG logging."""
        os.environ["MCP_AGY_DEBUG"] = truthy
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["debug"] is True
            assert mock_create_server.call_args.kwargs["log_level"] == "DEBUG"

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
    def test_env_debug_falsy_values_leave_debug_off(self, runner, falsy):
        """Verifies falsy MCP_AGY_DEBUG values do not enable debug mode."""
        os.environ["MCP_AGY_DEBUG"] = falsy
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["debug"] is False
            assert mock_create_server.call_args.kwargs["log_level"] == "INFO"

    def test_env_log_level_wins_over_env_debug(self, runner):
        """Verifies an explicit MCP_AGY_LOG_LEVEL is not overridden by MCP_AGY_DEBUG.

        Debug mode still reports as enabled; only the log verbosity respects the explicit ask.
        """
        os.environ["MCP_AGY_DEBUG"] = "1"
        os.environ["MCP_AGY_LOG_LEVEL"] = "ERROR"
        with patch("mcp_agy.cli.create_mcp_server") as mock_create_server:
            mock_create_server.return_value = MagicMock()

            result = runner.invoke(main, ["--no-banner"])
            assert result.exit_code == 0
            assert mock_create_server.call_args.kwargs["debug"] is True
            assert mock_create_server.call_args.kwargs["log_level"] == "ERROR"

    def test_shipped_client_configs_only_set_implemented_env_vars(self):
        """Verifies every env var in the shipped configs/ files is actually read by the code.

        A config that sets a variable nothing consumes is worse than no config: the user
        believes the setting took effect.
        """
        repo_root = Path(__file__).parent.parent
        implemented = {
            "MCP_AGY_BACKEND",
            "MCP_AGY_AUTO_FALLBACK",
            "MCP_AGY_CLI_PATH",
            "MCP_AGY_LOG_LEVEL",
            "MCP_AGY_DEBUG",
            "MCP_AGY_MODEL",
            "MCP_AGY_DEFAULT_MODEL",
            "MCP_AGY_EFFORT",
            "AGY_BIN_PATH",
            "PYTHONIOENCODING",
            "PYTHONUTF8",
        }
        for cfg in (
            "configs/claude_desktop_config.json",
            "configs/cursor_mcp.json",
            "configs/cline_mcp_settings.json",
            "configs/roo_code_mcp_settings.json",
            "configs/claude_code_mcp.json",
        ):
            parsed = json.loads((repo_root / cfg).read_text(encoding="utf-8"))
            env_block = parsed["mcpServers"]["mcp-agy"].get("env", {})
            unknown = set(env_block) - implemented
            assert not unknown, f"{cfg} sets env vars no code reads: {sorted(unknown)}"


class TestDefaultModelEnvironmentAliases:
    """Verifies both documented spellings of the default-model variable reach the backend."""

    def _manager(self):
        from mcp_agy.core.backend_manager import BackendManager

        return BackendManager()

    def test_mcp_agy_model_sets_default_model(self):
        """Verifies MCP_AGY_MODEL populates the manager and CLI backend defaults."""
        os.environ.pop("MCP_AGY_DEFAULT_MODEL", None)
        os.environ["MCP_AGY_MODEL"] = "gemini-3.1-pro-high"
        mgr = self._manager()
        assert mgr.default_model == "gemini-3.1-pro-high"
        assert mgr.cli_backend.default_model == "gemini-3.1-pro-high"

    def test_mcp_agy_default_model_alias_is_honored(self):
        """Verifies the README's MCP_AGY_DEFAULT_MODEL spelling also works."""
        os.environ.pop("MCP_AGY_MODEL", None)
        os.environ["MCP_AGY_DEFAULT_MODEL"] = "claude-sonnet-4-6"
        mgr = self._manager()
        assert mgr.default_model == "claude-sonnet-4-6"
        assert mgr.cli_backend.default_model == "claude-sonnet-4-6"

    def test_mcp_agy_model_takes_precedence_over_alias(self):
        """Verifies MCP_AGY_MODEL wins when both spellings are present."""
        os.environ["MCP_AGY_MODEL"] = "gemini-3.7-flash-high"
        os.environ["MCP_AGY_DEFAULT_MODEL"] = "claude-sonnet-4-6"
        assert self._manager().default_model == "gemini-3.7-flash-high"

    def test_explicit_argument_beats_both_env_vars(self):
        """Verifies a constructor argument overrides every environment default."""
        from mcp_agy.core.backend_manager import BackendManager

        os.environ["MCP_AGY_MODEL"] = "gemini-3.7-flash-high"
        os.environ["MCP_AGY_DEFAULT_MODEL"] = "claude-sonnet-4-6"
        assert BackendManager(default_model="gpt-oss-120b-medium").default_model == "gpt-oss-120b-medium"

    def test_no_model_env_leaves_default_unset(self):
        """Verifies absence of both variables leaves the agy CLI default in charge."""
        os.environ.pop("MCP_AGY_MODEL", None)
        os.environ.pop("MCP_AGY_DEFAULT_MODEL", None)
        assert self._manager().default_model is None
