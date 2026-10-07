"""`MCP_AGY_MAX_CONCURRENCY` bounds how many agy processes run at once, and nothing else.

Measured: each agy run loads the user's whole Antigravity MCP config (~18 child processes,
~730 MB). Fifty background jobs started together would ask for ~36 GB, and the job registry
puts no limit on how many it starts. The cap makes the surplus wait inside the server instead.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_agy.core import cli_backend
from mcp_agy.core.cli_backend import MAX_CONCURRENCY_ENV, _stream_in_slot, max_concurrency


class _Gauge:
    """Counts how many fake agy streams are inside their run at the same moment."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.started = 0

    async def stream(self, *_args, **_kwargs):
        self.live += 1
        self.started += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(0.05)
            yield "line"
        finally:
            self.live -= 1


@pytest.fixture
def gauge(monkeypatch) -> _Gauge:
    g = _Gauge()
    monkeypatch.setattr(cli_backend, "stream_subprocess_lines", g.stream)
    return g


async def _drain(n: int) -> None:
    async def one() -> None:
        async for _ in _stream_in_slot(["agy"]):
            pass

    await asyncio.gather(*[one() for _ in range(n)])


class TestMaxConcurrencyParsing:
    @pytest.mark.parametrize("raw", [None, "", "   ", "0", "-3", "abc", "1.5"])
    def test_anything_unusable_means_no_limit(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv(MAX_CONCURRENCY_ENV, raising=False)
        else:
            monkeypatch.setenv(MAX_CONCURRENCY_ENV, raw)
        assert max_concurrency() == 0

    def test_a_positive_integer_is_the_limit(self, monkeypatch):
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, " 16 ")
        assert max_concurrency() == 16


class TestCapIsEnforced:
    async def test_runs_beyond_the_limit_wait_for_a_slot(self, monkeypatch, gauge):
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "3")
        await _drain(12)
        assert gauge.started == 12, "every queued run must still happen"
        assert gauge.peak == 3, f"cap of 3 allowed {gauge.peak} at once"

    async def test_no_limit_by_default(self, monkeypatch, gauge):
        monkeypatch.delenv(MAX_CONCURRENCY_ENV, raising=False)
        await _drain(12)
        assert gauge.peak == 12

    async def test_a_cancelled_run_gives_its_slot_back(self, monkeypatch, gauge):
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "1")

        async def stalls_inside_agy(*_a, **_k):
            gauge.live += 1
            try:
                await asyncio.sleep(30)
                yield "never"
            finally:
                gauge.live -= 1

        # Cancellation arrives while the consumer is waiting on the stream - which is where a
        # cancelled job is in practice - so it is thrown into the generator and unwinds it.
        monkeypatch.setattr(cli_backend, "stream_subprocess_lines", stalls_inside_agy)

        async def hold() -> None:
            async for _ in _stream_in_slot(["agy"]):
                pass

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.02)
        assert gauge.live == 1

        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        monkeypatch.setattr(cli_backend, "stream_subprocess_lines", gauge.stream)

        # With the only slot leaked this would hang until the per-test timeout.
        await asyncio.wait_for(_drain(1), timeout=5)
        assert gauge.live == 0

    async def test_a_failing_run_gives_its_slot_back(self, monkeypatch):
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "1")

        async def boom(*_a, **_k):
            raise RuntimeError("agy exploded")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(cli_backend, "stream_subprocess_lines", boom)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                async for _line in _stream_in_slot(["agy"]):
                    pass
