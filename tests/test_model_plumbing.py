"""The model/effort selection must actually reach the agy process, and the mock must not lie.

Both properties were broken and neither was covered:

* `BackendManager.execute_task` built its `ExecutionRequest` without `model`/`effort`, and
  `default_model` was handed to `PythonSDKBackend` only. A caller naming a model therefore got
  agy's default, with no error and no log line — the run looked correct and was answered by a
  different model than the one the architect chose.
* `auto_fallback` defaulted to True, so a failing real backend was replaced by the simulated
  `MockAGYBackend` and the caller received `status="success"` for work that never happened.
"""

from __future__ import annotations

import pytest

from mcp_agy.core.backend_manager import BackendManager
from mcp_agy.core.cli_backend import SubprocessCLIBackend
from mcp_agy.core.models import BackendType, ExecutionRequest


class TestModelReachesTheProcess:
    def test_explicit_model_and_effort_land_in_the_command(self, monkeypatch):
        monkeypatch.setattr(
            "mcp_agy.core.cli_backend.find_agy_executable", lambda: __file__
        )
        backend = SubprocessCLIBackend()
        cmd = backend._build_command(prompt="p", model="gemini-3.7-flash-high", effort="high")

        assert "--model" in cmd, f"no --model in {cmd}"
        assert cmd[cmd.index("--model") + 1] == "gemini-3.7-flash-high"
        assert "--effort" in cmd, f"no --effort in {cmd}"
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_backend_default_model_is_used_when_request_names_none(self, monkeypatch):
        monkeypatch.setattr(
            "mcp_agy.core.cli_backend.find_agy_executable", lambda: __file__
        )
        backend = SubprocessCLIBackend(
            default_model="gemini-3.1-pro-high", default_effort="low"
        )
        request = ExecutionRequest(prompt="p", workspace_path="", backend_preference=BackendType.CLI)

        cmd = backend._build_command(
            prompt=request.prompt,
            model=request.model or backend.default_model,
            effort=request.effort or backend.default_effort,
        )
        assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
        assert cmd[cmd.index("--effort") + 1] == "low"


class TestManagerForwardsSelection:
    @pytest.mark.asyncio
    async def test_execute_task_forwards_model_and_effort(self, monkeypatch):
        """The regression: these two used to be absent from the request entirely."""
        captured: dict[str, ExecutionRequest] = {}

        mgr = BackendManager(preferred_backend="cli", auto_fallback=False)

        async def spy(request: ExecutionRequest):
            captured["req"] = request
            raise RuntimeError("stop after capture")

        monkeypatch.setattr(mgr.cli_backend, "execute", spy)
        monkeypatch.setattr(mgr.cli_backend, "is_available", lambda: True)

        with pytest.raises(RuntimeError):
            await mgr.execute_task(
                workspace_path=".",
                prompt="p",
                model="gemini-3.7-flash-high",
                effort="high",
            )

        req = captured["req"]
        assert req.model == "gemini-3.7-flash-high"
        assert req.effort == "high"

    @pytest.mark.asyncio
    async def test_chat_forwards_model_and_effort(self, monkeypatch):
        captured: dict[str, ExecutionRequest] = {}

        mgr = BackendManager(preferred_backend="cli", auto_fallback=False)

        async def spy(request: ExecutionRequest):
            captured["req"] = request
            raise RuntimeError("stop after capture")

        monkeypatch.setattr(mgr.cli_backend, "execute", spy)
        monkeypatch.setattr(mgr.cli_backend, "is_available", lambda: True)

        with pytest.raises(RuntimeError):
            await mgr.chat(prompt="p", model="gemini-3.1-pro-high", effort="medium")

        assert captured["req"].model == "gemini-3.1-pro-high"
        assert captured["req"].effort == "medium"

    def test_manager_passes_default_model_to_the_cli_backend(self):
        """It used to reach PythonSDKBackend only, so CLI runs silently ignored it."""
        mgr = BackendManager(default_model="gemini-3.7-flash-high", default_effort="high")
        assert mgr.cli_backend.default_model == "gemini-3.7-flash-high"
        assert mgr.cli_backend.default_effort == "high"
        assert mgr.sdk_backend.default_model == "gemini-3.7-flash-high"

    def test_env_supplies_the_default_model(self, monkeypatch):
        monkeypatch.setenv("MCP_AGY_MODEL", "gemini-3.6-flash-high")
        monkeypatch.setenv("MCP_AGY_EFFORT", "medium")
        mgr = BackendManager()
        assert mgr.default_model == "gemini-3.6-flash-high"
        assert mgr.cli_backend.default_model == "gemini-3.6-flash-high"
        assert mgr.default_effort == "medium"


class TestMockDoesNotMasqueradeAsSuccess:
    def test_auto_fallback_is_off_by_default(self, monkeypatch):
        """On, a failing real backend returns a fabricated mock success to the architect."""
        monkeypatch.delenv("MCP_AGY_AUTO_FALLBACK", raising=False)
        assert BackendManager().auto_fallback is False

    def test_env_can_opt_back_into_fallback(self, monkeypatch):
        monkeypatch.setenv("MCP_AGY_AUTO_FALLBACK", "1")
        assert BackendManager().auto_fallback is True

    def test_strict_cli_preference_raises_instead_of_faking_it(self, monkeypatch):
        mgr = BackendManager(preferred_backend="cli", auto_fallback=False)
        monkeypatch.setattr(mgr.cli_backend, "is_available", lambda: False)
        monkeypatch.setattr(mgr.sdk_backend, "is_available", lambda: False)

        with pytest.raises(RuntimeError, match="unavailable"):
            mgr.get_best_available_backend()
