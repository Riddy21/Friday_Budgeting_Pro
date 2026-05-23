"""
tests/test_scheduler.py — Unit tests for server/scheduler.py.

pytest-asyncio is not available, so async tests are wrapped with asyncio.run()
in a top-level synchronous test function.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from server.scheduler import Scheduler, next_run_at, seconds_until


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_aware(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a local-timezone-aware datetime."""
    return datetime(year, month, day, hour, minute).astimezone()


# ---------------------------------------------------------------------------
# next_run_at
# ---------------------------------------------------------------------------


def test_next_run_at_before_hour():
    """now = 05:00 → next run is today at 06:00."""
    now = _local_aware(2026, 5, 23, 5, 0)
    result = next_run_at(now, hour=6)
    assert result.year == 2026
    assert result.month == 5
    assert result.day == 23
    assert result.hour == 6
    assert result.minute == 0
    assert result.second == 0


def test_next_run_at_after_hour():
    """now = 07:00 → next run is tomorrow at 06:00."""
    now = _local_aware(2026, 5, 23, 7, 0)
    result = next_run_at(now, hour=6)
    assert result.year == 2026
    assert result.month == 5
    assert result.day == 24
    assert result.hour == 6


def test_next_run_at_exactly_on_hour():
    """now = 06:00:00 exactly → treat as past; next run is tomorrow."""
    now = _local_aware(2026, 5, 23, 6, 0)
    result = next_run_at(now, hour=6)
    assert result.day == 24
    assert result.hour == 6


# ---------------------------------------------------------------------------
# seconds_until
# ---------------------------------------------------------------------------


def test_seconds_until_positive():
    """seconds_until should return a positive value when target is in the future."""
    now = _local_aware(2026, 5, 23, 5, 0)
    target = _local_aware(2026, 5, 23, 6, 0)
    result = seconds_until(now, target)
    assert result > 0
    assert abs(result - 3600.0) < 1.0  # roughly one hour


# ---------------------------------------------------------------------------
# run_daily_sync_job
# ---------------------------------------------------------------------------


def test_run_daily_sync_job_calls_sync():
    """run_daily_sync_job() should call server.main.sync exactly once."""
    async def _inner():
        mock_sync = MagicMock(return_value={"synced": 0})
        with patch("server.main.sync", mock_sync):
            scheduler = Scheduler()
            await scheduler.run_daily_sync_job()
        mock_sync.assert_called_once()

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# run_hourly_drift_job
# ---------------------------------------------------------------------------


def test_run_hourly_drift_job_logs_active_count(caplog):
    """run_hourly_drift_job() should log a line containing 'active'."""
    async def _inner():
        mock_list = MagicMock(return_value={
            "connections": [
                {"status": "active"},
                {"status": "active"},
                {"status": "needs_reauth"},
            ]
        })
        with patch("server.main.list_connections", mock_list):
            scheduler = Scheduler()
            with caplog.at_level(logging.INFO, logger="server.scheduler"):
                await scheduler.run_hourly_drift_job()

    asyncio.run(_inner())

    combined = " ".join(caplog.messages)
    assert "active" in combined


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


def test_stop_cancels_task():
    """stop() should cancel the background task."""
    async def _inner():
        scheduler = Scheduler()
        await scheduler.run()
        # Give the loop one iteration to start
        await asyncio.sleep(0)
        assert scheduler._task is not None
        assert not scheduler._task.done()
        scheduler.stop()
        # Allow cancellation to propagate
        await asyncio.sleep(0)
        assert scheduler._task.cancelled() or scheduler._task.done()

    asyncio.run(_inner())
