#!/bin/bash
# scripts/deploy.sh — Deploy Friday Budgeting Pro
# Pull latest code, restart service, ensure no MCP duplication.
set -euo pipefail

LABEL="ai.openclaw.friday-budgeting-pro"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$HOME/.friday-bp/deploy.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

cd "$REPO_DIR"
log "=== Starting deployment ==="

# 1. Stop the service gracefully
log "Stopping service..."
launchctl unload "$PLIST" 2>/dev/null || true

# 2. Wait for process to fully exit (prevents MCP duplication)
log "Waiting for daemon to exit..."
for i in {1..10}; do
    if ! pgrep -f "server\.daemon" >/dev/null 2>&1; then
        log "Daemon stopped."
        break
    fi
    sleep 1
done

# 3. Kill any orphaned processes (belt and suspenders)
pkill -f "python.*server\.daemon" 2>/dev/null || true
pkill -f "python.*server\.main" 2>/dev/null || true
sleep 1

# 4. Double-check no Friday processes running
if pgrep -f "server\.(daemon|main)" >/dev/null 2>&1; then
    log "WARNING: Orphaned processes still running, force killing..."
    pkill -9 -f "python.*server\.(daemon|main)" 2>/dev/null || true
    sleep 1
fi

# 5. Pull latest code
log "Pulling latest from origin/main..."
git fetch origin
git reset --hard origin/main
log "Now at: $(git log --oneline -1)"

# 6. Re-register MCP (idempotent — overwrites existing entry, no duplicates)
log "Re-registering MCP server..."
/usr/local/opt/python@3.14/bin/python3.14 -m server.installer install 2>/dev/null || true

# 7. Start the service
log "Starting service..."
launchctl load "$PLIST"
sleep 2

# 8. Verify it's running
if launchctl list "$LABEL" >/dev/null 2>&1; then
    PID=$(launchctl list "$LABEL" | awk 'NR==2 {print $1}')
    log "Service started (PID: $PID)"
else
    log "ERROR: Service failed to start!"
    exit 1
fi

# 9. Check health
sleep 2
if curl -sk https://127.0.0.1:6789/health >/dev/null 2>&1; then
    log "Health check passed ✓"
else
    log "WARNING: Health check failed (service may still be starting)"
fi

log "=== Deployment complete ==="
