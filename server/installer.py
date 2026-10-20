"""server/installer.py — Friday Budgeting Pro Python installer.

Replaces the bash scripts/install.sh approach (superseded by issue #94).

Usage:
    python3 -m server.installer          # install (default)
    python3 -m server.installer install
    python3 -m server.installer uninstall
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LABEL = "ai.openclaw.friday-budgeting-pro"
PLIST_NAME = f"{LABEL}.plist"

# Resolve install directory as the parent of the server/ package.
# When running as `python3 -m server.installer` from the skill directory,
# __file__ is <install_dir>/server/installer.py.
_INSTALL_DIR = Path(__file__).parent.parent.resolve()


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / PLIST_NAME


def _openclaw_config_path() -> Path:
    # OpenClaw uses openclaw.json as its primary config file
    return Path.home() / ".openclaw" / "openclaw.json"


def _get_public_url() -> str | None:
    """Detect the best public URL for this machine.
    
    Checks in order:
    1. FRIDAY_BP_PUBLIC_URL env var (explicit override)
    2. Tailscale hostname (if tailscale is available)
    3. None (falls back to localhost in the app)
    """
    # Check explicit override first
    if url := os.environ.get("FRIDAY_BP_PUBLIC_URL"):
        return url
    
    # Try to detect Tailscale hostname
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            import json
            status = json.loads(result.stdout)
            if dns_name := status.get("Self", {}).get("DNSName", "").rstrip("."):
                # Use HTTPS - Friday uses TLS when certs are available
                return f"https://{dns_name}:6789"
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    
    return None


def _render_plist(install_dir: Path, public_url: str | None = None) -> str:
    """Return the launchd plist XML for the daemon.
    
    Parameters
    ----------
    install_dir:
        The directory where the server package is installed.
    public_url:
        Optional public URL for remote access (e.g. via Tailscale).
        If provided, sets FRIDAY_BP_PUBLIC_URL so reconnect links work remotely.
    """
    log_path = Path.home() / ".friday-bp" / "daemon.log"
    python_bin = sys.executable
    
    # Build environment variables section if needed
    env_section = ""
    if public_url:
        env_section = f"""
    <key>EnvironmentVariables</key>
    <dict>
        <key>FRIDAY_BP_PUBLIC_URL</key>
        <string>{public_url}</string>
    </dict>
"""
    
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>-m</string>
        <string>server.daemon</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{install_dir}</string>
{env_section}
    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def _register_mcp(install_dir: Path) -> None:
    """Add friday-budgeting-pro to OpenClaw's mcpServers config (idempotent)."""
    config_path = _openclaw_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            config = {}
    else:
        config = {}

    # OpenClaw MCP servers live under mcp.servers
    if "mcp" not in config:
        config["mcp"] = {}
    if "servers" not in config["mcp"]:
        config["mcp"]["servers"] = {}
    config["mcp"]["servers"]["friday-budgeting-pro"] = {
        "command": sys.executable,
        "args": ["-m", "server.main"],
        "cwd": str(install_dir),
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def install(install_dir: Path | None = None) -> None:
    """Install the Friday Budgeting Pro daemon and register it with OpenClaw.

    Steps:
      1. Write ~/Library/LaunchAgents/<label>.plist
      2. Register the service with launchd (bootstrap)
      3. Add the MCP server entry to ~/.openclaw/config.json
    """
    if install_dir is None:
        install_dir = _INSTALL_DIR

    plist_path = _plist_path()

    # Detect public URL for remote access (e.g. Tailscale)
    public_url = _get_public_url()

    # 1. Ensure the LaunchAgents directory exists and write the plist.
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_render_plist(install_dir, public_url=public_url))
    plist_path.chmod(0o644)

    # 2. Register with launchd — ignore errors (already loaded is harmless).
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        check=False,
    )

    # 3. Register MCP server in OpenClaw config.
    _register_mcp(install_dir)

    # Show the appropriate URL based on whether we detected a public URL
    setup_url = public_url or "http://127.0.0.1:6789"
    print(f"✓ Friday Budgeting Pro installed. Open {setup_url} to finish setup.")
    if public_url:
        print(f"  (Remote access enabled via {public_url})")
    else:
        print("  (Tip: Install Tailscale for remote access support)")


def uninstall() -> None:
    """Remove the launchd service and plist.

    Data in ~/.friday-bp/ is intentionally preserved.
    The OpenClaw config entry is intentionally preserved (ClawHub manages it).
    """
    plist_path = _plist_path()

    # 1. Unload from launchd (ignore errors if not loaded).
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        check=False,
    )

    # 2. Remove the plist.
    plist_path.unlink(missing_ok=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if action == "install":
        install()
    elif action == "uninstall":
        uninstall()
    else:
        print(f"Unknown action: {action!r}. Use 'install' or 'uninstall'.", file=sys.stderr)
        sys.exit(1)
