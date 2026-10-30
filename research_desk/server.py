#!/usr/bin/env python3
"""Loopback Research Desk preview/data server with background collection."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import refresh_data
import screen_cycle


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("RESEARCH_DESK_PORT", "8000"))
HOST = os.environ.get("RESEARCH_DESK_HOST", "127.0.0.1")
REFRESH_SECONDS = 300
SENSOR_SECONDS = 300
DATA_LOCK = threading.Lock()
SCREEN_LOCK = threading.Lock()
SCREEN_ENABLED = False


def _resolve_display_name() -> str | None:
    value = os.environ.get("RESEARCH_DESK_DISPLAY_NAME")
    if value and value.strip():
        return value.strip()
    try:
        text = (ROOT / ".state" / "display_name").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return None


DISPLAY_NAME = _resolve_display_name()


def refresh_all(sample_sensor: bool = False) -> None:
    with DATA_LOCK:
        payload = refresh_data.collect(sample_sensor=sample_sensor)
        refresh_data.write_payload(payload)
        rss = payload["sources"]["zotero"]
        print(f"data refreshed: {len(payload['rss']['items'])} papers today; "
              f"Codex {payload['tokens']['codex']['today']}; "
              f"OpenCode {payload['tokens']['opencode']['today']}; "
              f"feeds {rss.get('failed', 0)} failed", flush=True)


def collection_loop(screen_enabled: bool = False) -> None:
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            refresh_all(sample_sensor=False)
        except Exception as exc:
            print(f"data refresh failed: {type(exc).__name__}", flush=True)
            continue
        if screen_enabled:
            try:
                with SCREEN_LOCK:
                    screen_cycle.upload_next_frame()
            except Exception as exc:
                print(f"screen update failed: {type(exc).__name__}: {exc}", flush=True)


def sensor_loop(initial_delay: float = 0) -> None:
    if initial_delay:
        time.sleep(initial_delay)
    while True:
        time.sleep(SENSOR_SECONDS)
        db = None
        try:
            db = refresh_data.db_connect()
            with DATA_LOCK:
                env, status = refresh_data.sample_environment(db)
            print("E1002 sensor sampled" if env.get("temperatureC") is not None else f"E1002 sensor unavailable: {status}", flush=True)
        except Exception as exc:
            print(f"E1002 sensor scan failed: {type(exc).__name__}", flush=True)
        finally:
            if db:
                db.close()


def _handle_button(button_id: int) -> None:
    """Map the three configured E1002 input IDs to previous, next, and refresh."""
    action = ("previous", "next", "refresh")[button_id] if 0 <= button_id <= 2 else "next"
    try:
        if action == "refresh":
            refresh_all(sample_sensor=True)
            with SCREEN_LOCK:
                frame = screen_cycle.refresh_current_frame()
        else:
            with SCREEN_LOCK:
                frame = (screen_cycle.upload_previous_frame() if action == "previous"
                         else screen_cycle.upload_next_frame())
        print(f"E1002 button {button_id}: {action} -> {frame}", flush=True)
    except Exception as exc:
        print(f"E1002 button {button_id} action failed: {type(exc).__name__}", flush=True)


def button_loop() -> None:
    """Watch E1002's configured BLE input byte and dispatch button presses."""
    if DISPLAY_NAME is None:
        print("E1002 button listener disabled: RESEARCH_DESK_DISPLAY_NAME not configured", flush=True)
        return
    async def watch() -> None:
        from bleak import BleakScanner

        last: dict[str, tuple[int, int, bool]] = {}
        last_fired: dict[str, float] = {}

        def callback(device: object, advertisement: object) -> None:
            names = (getattr(device, "name", "") or "", getattr(advertisement, "local_name", "") or "")
            if DISPLAY_NAME is None or not any(DISPLAY_NAME.lower() in name.lower() for name in names):
                return
            payload = getattr(advertisement, "manufacturer_data", {}).get(0x2446)
            if not payload or len(payload) != 14:
                return
            address = getattr(device, "address", "")
            raw = int(payload[0])
            event = (raw & 0x07, (raw >> 3) & 0x0F, bool(raw & 0x80))
            previous = last.get(address)
            last[address] = event
            if previous is None:
                return
            button_id, count, pressed = event
            _, prev_count, prev_pressed = previous
            count_delta = (count - prev_count) & 0x0F
            new_press = 0 < count_delta < 8 or (pressed and not prev_pressed)
            now = time.monotonic()
            if new_press and now - last_fired.get(address, 0) > 1.0:
                last_fired[address] = now
                threading.Thread(target=_handle_button, args=(button_id,), daemon=True).start()

        scanner = BleakScanner(callback)
        await scanner.start()
        print("E1002 physical buttons enabled (0=previous, 1=next, 2=refresh)", flush=True)
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await scanner.stop()

    try:
        asyncio.run(watch())
    except Exception as exc:
        print(f"E1002 button listener unavailable: {type(exc).__name__}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        names = {"/": "research_desk.html", "/research_desk.html": "research_desk.html",
                 "/data.json": "data.json", "/demo-data.json": "demo-data.json"}
        name = names.get(path)
        if not name:
            self.send_error(404)
            return
        target = ROOT / name
        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(503, "Data not ready")
            return
        content_type = "application/json; charset=utf-8" if name.endswith(".json") else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/refresh", "/api/next", "/api/previous"):
            self.send_error(404)
            return
        if path != "/api/refresh" and not SCREEN_ENABLED:
            self._send_json(409, {"ok": False, "error": "screen mode is not enabled"})
            return
        try:
            if path == "/api/refresh":
                refresh_all(sample_sensor=True)
                frame = None
                if SCREEN_ENABLED:
                    with SCREEN_LOCK:
                        frame = screen_cycle.refresh_current_frame()
            elif path == "/api/next":
                with SCREEN_LOCK:
                    frame = screen_cycle.upload_next_frame()
            else:
                with SCREEN_LOCK:
                    frame = screen_cycle.upload_previous_frame()
        except Exception as exc:
            print(f"manual {path.rsplit('/', 1)[-1]} failed: {type(exc).__name__}", flush=True)
            self._send_json(502, {"ok": False, "error": type(exc).__name__})
            return
        self._send_json(200, {"ok": True, "frame": frame})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep request logs free of query parameters and any authorization material.
        path = urlsplit(self.path).path
        print(f"http {self.command} {path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Desk local preview and data service")
    parser.add_argument("--screen", action="store_true", help="rotate pages on the paired OpenDisplay E1002")
    args = parser.parse_args()
    global SCREEN_ENABLED
    SCREEN_ENABLED = args.screen

    try:
        refresh_all(sample_sensor=True)
    except Exception as exc:
        print(f"initial refresh failed: {type(exc).__name__}; serving last saved data if present", flush=True)
    threading.Thread(target=collection_loop, args=(args.screen,), name="data-refresh", daemon=True).start()
    # Stagger passive BLE sensor sampling from the 5-minute OpenDisplay upload slot.
    threading.Thread(target=sensor_loop, args=(150 if args.screen else 0,), name="environment-sensor", daemon=True).start()
    if args.screen:
        if DISPLAY_NAME is None:
            print("E1002 button listener disabled: RESEARCH_DESK_DISPLAY_NAME not configured", flush=True)
        else:
            threading.Thread(target=button_loop, name="e1002-buttons", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Research Desk at http://{HOST}:{PORT}/research_desk.html", flush=True)
    if args.screen:
        print("E1002 rotation enabled: one full-frame upload every 5 minutes", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
