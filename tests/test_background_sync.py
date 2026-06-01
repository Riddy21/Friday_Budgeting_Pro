"""tests/test_background_sync.py — tests for the daemon background sync loop.

Covers:
  - _background_sync_loop calls sync() exactly once after sleeping once
  - _background_sync_loop continues (does not crash) when sync() raises
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


async def _run_loop_n_iterations(n: int, interval: float = 0.0) -> MagicMock:
    """Run _background_sync_loop for exactly *n* sleep cycles, then cancel.

    Returns the mock used for ``server.main.sync`` so callers can assert on
    call counts.
    """
    from server.daemon import _background_sync_loop

    sleep_count = 0
    mock_sync = MagicMock(return_value={"status": "ok", "new_transactions": 0})

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > n:
            # Stop the loop by raising CancelledError after n iterations.
            raise asyncio.CancelledError

    with (
        patch("asyncio.sleep", new=fake_sleep),
        patch("server.daemon.asyncio.get_running_loop") as mock_get_loop,
        patch("server.main.sync", mock_sync),
    ):
        # Simulate run_in_executor calling sync() synchronously.
        mock_loop = MagicMock()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future.set_result(mock_sync())

        async def fake_run_in_executor(_executor, func, *args):  # noqa: ANN001
            return func(*args) if args else func()

        mock_loop.run_in_executor = fake_run_in_executor
        mock_get_loop.return_value = mock_loop

        try:
            await _background_sync_loop(interval)
        except asyncio.CancelledError:
            pass

    return mock_sync


# ---------------------------------------------------------------------------
# Test: sync is called exactly once after one sleep
# ---------------------------------------------------------------------------


def test_background_sync_calls_sync_once_after_sleep():
    """sync() is invoked exactly once after the first sleep completes."""
    from server.daemon import _background_sync_loop

    calls = []

    async def run():
        sleep_count = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        mock_sync = MagicMock(return_value={"status": "ok"})

        with (
            patch("asyncio.sleep", new=fake_sleep),
            patch("server.daemon.asyncio.get_running_loop") as mock_get_loop,
            patch("server.main.sync", mock_sync),
        ):
            mock_loop = MagicMock()

            async def fake_run_in_executor(_executor, func, *args):
                result = func(*args) if args else func()
                calls.append(result)
                return result

            mock_loop.run_in_executor = fake_run_in_executor
            mock_get_loop.return_value = mock_loop

            try:
                await _background_sync_loop(0.0)
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert len(calls) == 1, f"Expected sync to be called once, got {len(calls)}"


# ---------------------------------------------------------------------------
# Test: loop continues after an exception from sync()
# ---------------------------------------------------------------------------


def test_background_sync_continues_after_exception():
    """_background_sync_loop does not crash when sync() raises an exception."""
    from server.daemon import _background_sync_loop

    sync_attempts = []

    async def run():
        sleep_count = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 2:
                raise asyncio.CancelledError

        def failing_sync():
            sync_attempts.append("attempt")
            raise RuntimeError("Plaid exploded")

        with (
            patch("asyncio.sleep", new=fake_sleep),
            patch("server.daemon.asyncio.get_running_loop") as mock_get_loop,
            patch("server.main.sync", failing_sync),
        ):
            mock_loop = MagicMock()

            async def fake_run_in_executor(_executor, func, *args):
                return func(*args) if args else func()

            mock_loop.run_in_executor = fake_run_in_executor
            mock_get_loop.return_value = mock_loop

            try:
                await _background_sync_loop(0.0)
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    # The loop should have attempted sync twice (once per iteration) without crashing.
    assert len(sync_attempts) == 2, (
        f"Expected 2 sync attempts (loop should not crash), got {len(sync_attempts)}"
    )
