"""
ui/server.py — Minimal FastAPI application for Friday Budgeting Pro.

This module defines the FastAPI ``app`` instance that is mounted and served
by ``server.daemon`` via uvicorn on 127.0.0.1:6789.

Current surface area (issue #52 scope):
  - GET /healthz → {"status": "ok"}

All real routes (login, setup, profile, transaction views, etc.) are
tracked in issue #14 and will be added there.  This stub intentionally
ships only the health-check endpoint so the daemon scaffold can be tested
end-to-end without pulling in unfinished features.

Host binding (127.0.0.1 only) is configured in server.daemon via
uvicorn.Config; this module is transport-agnostic.
"""

from fastapi import FastAPI

app = FastAPI(title="Friday Budgeting Pro UI", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe — returns 200 OK when the daemon is running."""
    return {"status": "ok"}
