#!/usr/bin/env python3
"""Set the paired E1002 to USB power and disable idle/deep sleep."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
KEY_PATH = STATE / "opendisplay.key"
CLI = STATE / "opendisplay-venv" / "bin" / "opendisplay"


def _resolve_display_name() -> str | None:
    value = os.environ.get("RESEARCH_DESK_DISPLAY_NAME")
    if value and value.strip():
        return value.strip()
    try:
        text = (STATE / "display_name").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return None


DEVICE = _resolve_display_name()
BEFORE = STATE / "e1002-config-before.json"
AFTER = STATE / "e1002-config-verify.json"
TARGET_POWER = {"power_mode": 2, "sleep_timeout_ms": 0, "deep_sleep_time_seconds": 0}


def _run(*args: str) -> None:
    if DEVICE is None:
        raise RuntimeError("E1002 display name not configured")
    key = KEY_PATH.read_text(encoding="ascii").strip()
    result = subprocess.run(
        [str(CLI), *args, "--device", DEVICE, "--key", key, "--timeout", "30"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    details = (result.stdout + result.stderr).replace(key, "[REDACTED]").strip()
    if result.returncode:
        raise RuntimeError(details[-1200:] or f"OpenDisplay command failed ({result.returncode})")


def _export(path: Path) -> dict:
    _run("export-config", str(path))
    path.chmod(0o600)
    return json.loads(path.read_text(encoding="utf-8"))


def _power_packet(config: dict) -> dict:
    matches = [packet for packet in config.get("packets", []) if str(packet.get("id")) == "4"]
    if len(matches) != 1:
        raise ValueError(f"Expected one power configuration packet, found {len(matches)}")
    return matches[0]


def _power_values(fields: dict) -> dict[str, int]:
    def as_int(value: object) -> int:
        return int(value, 0) if isinstance(value, str) else int(value)

    return {key: as_int(fields.get(key, "0")) for key in TARGET_POWER}


def main() -> None:
    if DEVICE is None:
        raise SystemExit("E1002 display name not configured; set RESEARCH_DESK_DISPLAY_NAME or .state/display_name")
    if not KEY_PATH.is_file() or not CLI.is_file():
        raise SystemExit("OpenDisplay CLI and paired key are required in .state")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    os.umask(0o077)

    config = _export(BEFORE)
    packet = _power_packet(config)
    fields = packet.setdefault("fields", {})
    current = _power_values(fields)
    print(f"E1002 power settings before change: {current}")

    if current != TARGET_POWER:
        fields["power_mode"] = str(TARGET_POWER["power_mode"])
        fields["sleep_timeout_ms"] = "0x0"
        fields["deep_sleep_time_seconds"] = "0x0"
        updated = STATE / "e1002-config-usb.json"
        updated.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated.chmod(0o600)
        _run("write-config", str(updated))
        print("Applied USB power mode; idle and deep sleep timers set to Never/disabled.")
    else:
        print("E1002 already uses USB power with sleep timers disabled.")

    actual = None
    for attempt in range(5):
        try:
            verified = _export(AFTER)
            actual = _power_values(_power_packet(verified).get("fields", {}))
            break
        except (RuntimeError, ValueError):
            if attempt == 4:
                raise
            time.sleep(3)
    if actual != TARGET_POWER:
        raise RuntimeError(f"Power configuration did not verify: {actual}")
    print("Verified E1002 USB power mode (2), idle sleep disabled, deep sleep disabled.")


if __name__ == "__main__":
    if not re.fullmatch(r"[0-9a-fA-F]{32}", KEY_PATH.read_text(encoding="ascii").strip() if KEY_PATH.is_file() else ""):
        raise SystemExit("OpenDisplay key must contain 32 hexadecimal characters")
    main()
