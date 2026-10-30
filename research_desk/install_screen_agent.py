#!/usr/bin/env python3
"""Install the Research Desk local preview, data refresh and E1002 rotation agent."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
LABEL = "com.researchdesk.screen"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STATE = ROOT / ".state"


def main() -> None:
    python = ROOT / ".venv" / "bin" / "python"
    key = STATE / "opendisplay.key"
    if not python.is_file() or not key.is_file():
        raise SystemExit("Set up the project environment and pair the OpenDisplay key before installing the agent.")

    runtime = Path(os.path.realpath(python))
    runtime_info = runtime.parent.parent / "Resources" / "Python.app" / "Contents" / "Info.plist"
    if runtime_info.is_file():
        with runtime_info.open("rb") as file:
            runtime_metadata = plistlib.load(file)
        if not runtime_metadata.get("NSBluetoothAlwaysUsageDescription"):
            raise SystemExit(
                "This Python runtime has no NSBluetoothAlwaysUsageDescription. macOS will terminate its "
                "LaunchAgent on the first Bluetooth access (OS_REASON_TCC). Run server.py --screen from "
                "the authorized foreground session, or install a signed app helper with a Bluetooth purpose string."
            )

    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PLIST.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (ROOT, python, ROOT / "server.py", STATE):
        if not path.exists():
            raise SystemExit(f"Required path is missing: {path}")

    args = [str(python), str(ROOT / "server.py"), "--screen"]
    strings = "\n".join(f"        <string>{escape(value)}</string>" for value in args)
    plist_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{strings}
    </array>
    <key>WorkingDirectory</key><string>{escape(str(ROOT))}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>{escape(str(STATE / "server.stdout.log"))}</string>
    <key>StandardErrorPath</key><string>{escape(str(STATE / "server.stderr.log"))}</string>
</dict>
</plist>
"""
    PLIST.write_text(plist_text, encoding="utf-8")
    PLIST.chmod(0o600)

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    existing = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, check=False)
    if existing.returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=False, capture_output=True, text=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, check=True)
    if "state = running" not in status.stdout:
        raise SystemExit("LaunchAgent loaded but the Research Desk server is not running; inspect .state/server.stderr.log.")
    print(f"Installed and started {LABEL}; local preview: http://127.0.0.1:8000/research_desk.html")


if __name__ == "__main__":
    main()
