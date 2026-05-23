"""
server/scheduler.py — In-process async scheduler for Friday Budgeting Pro.

Schedules two recurring jobs without any external dependencies (no OpenClaw,
no cron, no third-party libraries):

1. **Daily sync** — calls server.main.sync() once per day at a configurable
   hour (default 06:00 local time).
2. **Hourly drift check** — calls server.main.list_connections() every hour
   (at the top of the hour) and logs connection statuses.

Both jobs share the single-flight sync lock already enforced inside
server.main.sync(); the scheduler does not need to acquire it separately.

Public API
----------
next_run_at(now: datetime, hour: int = 6) -> datetime
    Returns the next wall-clock moment when *hour*:00:00 occurs.  If *now*
    is at or past *hour* today, returns *hour*:00:00 tomorrow.

seconds_until(now: datetime, target: datetime) -> float
    Returns (target - now).total_seconds().  Always positive when target > now.

class Scheduler
    async def run()  — start the combined scheduling loop as a background task
    def stop()       — cancel the background task
    async def run_daily_sync_job()  — execute the daily sync
    async def run_hourly_drift_job() — execute the hourly drift check
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import server.main as _main

__all__ = [
    "Scheduler",
    "next_run_at",
    "seconds_until",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure time-math helpers
# ---------------------------------------------------------------------------


def _local_zone() -> ZoneInfo:
    """Return the system's local time zone as a ZoneInfo object."""
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def next_run_at(now: datetime, hour: int = 6) -> datetime:
    """Return the next datetime when *hour*:00:00 occurs in *now*'s timezone.

    If *now* is strictly before *hour* today, returns today at *hour*:00:00.
    If *now* is at or past *hour* today, returns tomorrow at *hour*:00:00.

    Parameters
    ----------
    now:
        A timezone-aware (or naive-local) datetime representing the current time.
    hour:
        The target hour of day (0-23).  Default is 6 (06:00).
    """
    tz = now.tzinfo
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < candidate:
        return candidate
    # At or past the target hour — schedule for tomorrow.
    return candidate + timedelta(days=1)


def seconds_until(now: datetime, target: datetime) -> float:
    """Return the number of seconds between *now* and *target*.

    Parameters
    ----------
    now:
        Current time.
    target:
        Future time.

    Returns
    -------
    float
        Positive seconds when *target* is in the future relative to *now*.
    """
    return (target - now).total_seconds()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Async in-process scheduler for daily sync and hourly drift checks.

    Usage::

        scheduler = Scheduler(sync_hour=6)
        await scheduler.run()  # returns immediately; runs as a background task
        ...
        scheduler.stop()
    """

    def __init__(self, sync_hour: int = 6) -> None:
        self._sync_hour: int = sync_hour
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the scheduler loop as a background asyncio task.

        Returns immediately.  The task runs until :meth:`stop` is called or
        the event loop shuts down.
        """
        if self._task is not None and not self._task.done():
            log.warning("Scheduler already running; ignoring duplicate run() call.")
            return
        self._task = asyncio.create_task(self._loop(), name="friday-scheduler")
        log.info("Scheduler started (daily sync at %02d:00 local time).", self._sync_hour)

    def stop(self) -> None:
        """Cancel the background scheduler task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            log.info("Scheduler stop requested.")

    async def run_daily_sync_job(self) -> None:
        """Execute the daily sync.

        Delegates directly to server.main.sync().  The sync_lock single-flight
        guard is already enforced inside sync(), so no extra locking is needed
        here.
        """
        log.info("Scheduler: starting daily sync job.")
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _main.sync)
            log.info("Scheduler: daily sync completed — %s", result)
        except Exception as exc:  # noqa: BLE001
            log.error("Scheduler: daily sync failed: %s", exc, exc_info=True)

    async def run_hourly_drift_job(self) -> None:
        """Execute the hourly drift check.

        Calls server.main.list_connections() and logs a summary of connection
        statuses.  No Plaid API calls are made.
        """
        log.info("Scheduler: starting hourly drift check.")
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, _main.list_connections
            )
            connections = data.get("connections", [])
            status_counts: dict[str, int] = {}
            for conn in connections:
                status = conn.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            active = status_counts.get("active", 0)
            needs_reauth = status_counts.get("needs_reauth", 0)
            log.info(
                "Scheduler: drift check — %d connection(s) total | active=%d needs_reauth=%d %s",
                len(connections),
                active,
                needs_reauth,
                " ".join(f"{k}={v}" for k, v in status_counts.items() if k not in ("active", "needs_reauth")),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Scheduler: drift check failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main scheduling loop.

        On each iteration:
        1. Compute seconds until the next daily-sync time and the next
           top-of-hour.
        2. Sleep the shorter of the two intervals.
        3. Run the job(s) whose deadline has arrived (both may coincide).
        4. Repeat.
        """
        while True:
            now = datetime.now().astimezone()

            # Next daily sync
            next_sync = next_run_at(now, hour=self._sync_hour)
            secs_sync = seconds_until(now, next_sync)

            # Next top-of-hour
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            secs_hour = seconds_until(now, next_hour)

            sleep_secs = min(secs_sync, secs_hour)
            log.debug(
                "Scheduler sleeping %.1f s (next sync in %.1f s, next drift in %.1f s).",
                sleep_secs,
                secs_sync,
                secs_hour,
            )

            await asyncio.sleep(max(sleep_secs, 0))

            # Re-check current time after sleep to decide which job(s) to run.
            now_after = datetime.now().astimezone()

            if now_after >= next_sync:
                await self.run_daily_sync_job()

            if now_after >= next_hour:
                await self.run_hourly_drift_job()
