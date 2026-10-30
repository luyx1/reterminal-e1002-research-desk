#!/usr/bin/env python3
"""Render the latest dashboard and upload the next frame to the paired E1002."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
STATE_PATH = STATE_DIR / "screen_cycle.json"
KEY_PATH = STATE_DIR / "opendisplay.key"
LAN_STATE_PATH = STATE_DIR / "opendisplay-lan.json"
OPEN_DISPLAY = STATE_DIR / "opendisplay-venv" / "bin" / "opendisplay"


def _resolve_display_name() -> str | None:
    value = os.environ.get("RESEARCH_DESK_DISPLAY_NAME")
    if value and value.strip():
        return value.strip()
    try:
        text = (STATE_DIR / "display_name").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return None


DISPLAY_NAME = _resolve_display_name()
UPLOAD_ATTEMPTS = 4
UPLOAD_RETRY_SECONDS = 12


def _frame_paths() -> list[Path]:
    rss_pages = sorted(
        ROOT.glob("zotero_rss_page*.png"),
        key=lambda path: int(re.search(r"page(\d+)", path.stem).group(1)),
    )
    ai_page = ROOT / "ai_token_page.png"
    if ai_page.is_file():
        rss_pages.append(ai_page)
    return rss_pages


def _read_state() -> dict[str, str]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(frame_name: str) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE_PATH.parent.chmod(0o700)
    payload = {
        "frame": frame_name,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATE_PATH)


def mark_current_frame(frame_name: str) -> None:
    if frame_name not in {path.name for path in _frame_paths()}:
        raise ValueError(f"Unknown frame: {frame_name}")
    _write_state(frame_name)


def _discover_lan_target() -> dict[str, object] | None:
    """Find the paired display over Wi-Fi; use BLE when LAN discovery is empty."""
    try:
        found = subprocess.run(
            [str(OPEN_DISPLAY), "scan", "--lan", "--timeout", "3", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=12,
        )
        if found.returncode:
            return None
        devices = json.loads(found.stdout).get("devices", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError):
        return None
    if DISPLAY_NAME is not None:
        matches = [device for device in devices if device.get("name") == DISPLAY_NAME]
        if not matches and len(devices) == 1:
            matches = devices
    else:
        matches = devices if len(devices) == 1 else []
    if len(matches) != 1:
        return None
    device = matches[0]
    target = {"name": device.get("name"), "host": device.get("host"),
              "port": device.get("port"), "tls": bool(device.get("tls"))}
    if not isinstance(target["host"], str) or not isinstance(target["port"], int):
        return None
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    temporary = LAN_STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(target) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(LAN_STATE_PATH)
    return target


def upload_frame(*, step: int | None) -> str:
    if not KEY_PATH.is_file():
        raise FileNotFoundError("OpenDisplay key is missing from .state/opendisplay.key")
    key = KEY_PATH.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", key):
        raise ValueError("OpenDisplay key must contain 32 hexadecimal characters")

    rendered = subprocess.run(
        [sys.executable, str(ROOT / "render_v6.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if rendered.returncode:
        details = (rendered.stderr or rendered.stdout).strip()[-1600:]
        raise RuntimeError(f"render failed: {details}")

    frames = _frame_paths()
    if not frames:
        raise FileNotFoundError("Renderer did not create any dashboard frames")
    names = [path.name for path in frames]
    current = _read_state().get("frame")
    if current not in names:
        target = frames[0]
    else:
        index = names.index(current)
        target = frames[(index + step) % len(frames)] if step else frames[index]

    lan = _discover_lan_target()
    if lan:
        address_args = ["--host", str(lan["host"]), "--port", str(lan["port"])]
    elif DISPLAY_NAME is not None:
        address_args = ["--device", DISPLAY_NAME]
    else:
        raise RuntimeError("E1002 display name not configured; BLE upload disabled")
    if lan and lan.get("tls"):
        address_args.append("--tls")
    command = [str(OPEN_DISPLAY), "upload", *address_args, "--key", key,
               "--timeout", "25", "--refresh-mode", "full", "--dither-mode", "burkes",
               "--fit", "contain", str(target)]
    details = ""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            uploaded = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=90)
        except subprocess.TimeoutExpired as exc:
            details = f"OpenDisplay upload timed out on attempt {attempt}/{UPLOAD_ATTEMPTS}"
            if attempt == UPLOAD_ATTEMPTS:
                raise RuntimeError(f"{details}; the display may be asleep or out of range") from exc
            time.sleep(UPLOAD_RETRY_SECONDS)
            continue

        details = (uploaded.stdout + uploaded.stderr).replace(key, "[REDACTED]").strip()
        if uploaded.returncode == 0:
            break

        transient = any(
            phrase in details.lower()
            for phrase in ("not found during discovery", "ble connection failed", "ble timeout", "timed out")
        )
        if not transient or attempt == UPLOAD_ATTEMPTS:
            suffix = "; wake E1002 with its top button and retry" if "not found during discovery" in details.lower() else ""
            raise RuntimeError(f"upload failed: {details[-1600:]}{suffix}")
        print(f"E1002 not reachable; retry {attempt + 1}/{UPLOAD_ATTEMPTS} in {UPLOAD_RETRY_SECONDS}s", flush=True)
        time.sleep(UPLOAD_RETRY_SECONDS)

    _write_state(target.name)
    print(f"E1002 displayed {target.name}: {details or 'upload complete'}", flush=True)
    return target.name


def upload_next_frame() -> str:
    return upload_frame(step=1)


def upload_previous_frame() -> str:
    return upload_frame(step=-1)


def refresh_current_frame() -> str:
    return upload_frame(step=None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true", help="advance and upload one frame now")
    action.add_argument("--refresh-current", action="store_true", help="re-render and upload the current frame")
    action.add_argument("--mark-current", metavar="FRAME", help="record the frame already on the device")
    args = parser.parse_args()
    if args.mark_current:
        mark_current_frame(args.mark_current)
        print(f"Current E1002 frame recorded: {args.mark_current}")
    elif args.refresh_current:
        refresh_current_frame()
    else:
        upload_next_frame()


if __name__ == "__main__":
    main()
