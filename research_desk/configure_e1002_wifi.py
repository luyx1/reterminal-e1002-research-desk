#!/usr/bin/env python3
"""Securely enter and write a 2.4 GHz Wi-Fi profile to the paired E1002."""

from __future__ import annotations

import getpass
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
BEFORE = STATE / "e1002-config-wifi-before.json"
UPDATED = STATE / "e1002-config-wifi.json"
VERIFY = STATE / "e1002-config-wifi-verify.json"
LAN_TARGET = STATE / "opendisplay-lan.json"
WIFI_PACKET = "38"
DEFAULT_PORT = 2446


def _run(*args: str, key: str, secrets: tuple[str, ...] = ()) -> str:
    if DEVICE is None:
        raise RuntimeError("E1002 display name not configured")
    result = subprocess.run(
        [str(CLI), *args, "--device", DEVICE, "--key", key, "--timeout", "30"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    details = result.stdout + result.stderr
    for secret in (key, *secrets):
        if secret:
            details = details.replace(secret, "[REDACTED]")
    if result.returncode:
        raise RuntimeError(f"OpenDisplay command failed ({result.returncode}): {details[-800:]}")
    return details


def _export(path: Path, key: str) -> dict:
    _run("export-config", str(path), key=key)
    path.chmod(0o600)
    return json.loads(path.read_text(encoding="utf-8"))


def _wifi_packet(config: dict) -> dict | None:
    packets = [packet for packet in config.get("packets", []) if str(packet.get("id")) == WIFI_PACKET]
    if len(packets) > 1:
        raise ValueError("Device config contains duplicate Wi-Fi packets")
    return packets[0] if packets else None


def _parse_int(value: object, default: int) -> int:
    if value is None:
        return default
    return int(value, 0) if isinstance(value, str) else int(value)


def _scan_lan() -> list[dict]:
    result = subprocess.run(
        [str(CLI), "scan", "--lan", "--timeout", "4", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        return []
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    devices = value.get("devices", []) if isinstance(value, dict) else []
    return [device for device in devices if isinstance(device, dict)]


def main() -> None:
    if DEVICE is None:
        raise SystemExit("E1002 display name not configured; set RESEARCH_DESK_DISPLAY_NAME or .state/display_name")
    if not KEY_PATH.is_file() or not CLI.is_file():
        raise SystemExit("OpenDisplay CLI and paired key are required in .state")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    os.umask(0o077)

    ssid = input("E1002 的 2.4 GHz Wi-Fi 名称 (SSID): ").strip()
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise SystemExit("SSID must be 1–32 UTF-8 bytes")
    security = input("安全类型 [WPA2=3 / WPA=2 / WPA3=4 / 开放网络=0]（默认 WPA2）: ").strip().upper()
    security_types = {"": 3, "WPA2": 3, "3": 3, "WPA": 2, "2": 2, "WPA3": 4, "4": 4, "OPEN": 0, "0": 0}
    if security not in security_types:
        raise SystemExit("Unknown Wi-Fi security type")
    encryption = security_types[security]
    password = getpass.getpass("Wi-Fi 密码（输入时不显示）: ")
    if len(password.encode("utf-8")) > 32:
        raise SystemExit("This firmware config has a 32-byte password field")
    if encryption == 0 and password:
        raise SystemExit("Open networks must have an empty password")
    if encryption != 0 and not 8 <= len(password.encode("utf-8")) <= 32:
        raise SystemExit("WPA password must be 8–32 UTF-8 bytes for this device config")

    key = KEY_PATH.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", key):
        raise SystemExit("OpenDisplay key must contain 32 hexadecimal characters")

    config = _export(BEFORE, key)
    packet = _wifi_packet(config)
    if packet is None:
        packet = {"id": WIFI_PACKET, "name": "wifi_config", "fields": {}}
        config.setdefault("packets", []).append(packet)
    fields = packet.setdefault("fields", {})
    fields.update({
        "ssid": ssid,
        "password": password,
        "encryption_type": str(encryption),
        "server_url": str(fields.get("server_url", "")),
        "server_port": str(fields.get("server_port", hex(DEFAULT_PORT))),
        "reserved": str(fields.get("reserved", "0x0")),
    })
    UPDATED.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    UPDATED.chmod(0o600)
    _run("write-config", str(UPDATED), key=key, secrets=(ssid, password))

    verified = _export(VERIFY, key)
    wifi = _wifi_packet(verified)
    values = wifi.get("fields", {}) if wifi else {}
    if (values.get("ssid") != ssid or values.get("password") != password
            or _parse_int(values.get("encryption_type"), -1) != encryption):
        raise RuntimeError("E1002 did not retain the requested Wi-Fi profile")
    print("E1002 Wi-Fi profile written and verified; credentials remain only in the private .state folder.")

    _run("reboot", key=key, secrets=(ssid, password))
    print("E1002 rebooted; waiting for Wi-Fi/LAN discovery…")
    devices = []
    for _ in range(8):
        time.sleep(3)
        devices = _scan_lan()
        matching = [device for device in devices if device.get("name") == DEVICE]
        if matching or len(devices) == 1:
            devices = matching or devices
            break
    if len(devices) == 1:
        device = devices[0]
        target = {"name": device.get("name"), "host": device.get("host"),
                  "port": device.get("port"), "tls": bool(device.get("tls"))}
        if isinstance(target["host"], str) and isinstance(target["port"], int):
            temporary = LAN_TARGET.with_suffix(".tmp")
            temporary.write_text(json.dumps(target) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(LAN_TARGET)
            print(f"E1002 is reachable over Wi-Fi at {target['host']}:{target['port']}.")
            return
    print("Wi-Fi credentials were saved, but the E1002 did not appear on LAN yet; check 2.4 GHz reachability and run `opendisplay scan --lan` again after it wakes.")


if __name__ == "__main__":
    main()
