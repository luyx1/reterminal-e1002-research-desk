#!/usr/bin/env python3
"""Render the current real data pages at the native 800x480 device size."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright


BASE = Path(__file__).resolve().parent


async def capture_device(page, destination: Path):
    await page.locator("#device").screenshot(path=str(destination))
    with Image.open(destination) as image:
        if image.size == (800, 480):
            return
        if image.width < 800 or image.height < 480:
            raise AssertionError((destination.name, image.size))
        image.crop((0, 0, 800, 480)).save(destination)


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        allowed = {"/": "research_desk.html", "/research_desk.html": "research_desk.html", "/data.json": "data.json"}
        name = allowed.get(path)
        if not name:
            self.send_error(404)
            return
        try:
            body = (BASE / name).read_bytes()
        except OSError:
            self.send_error(503)
            return
        kind = "application/json; charset=utf-8" if name.endswith(".json") else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


async def main():
    for old in BASE.glob("zotero_rss_page*.png"):
        old.unlink()
    http = ThreadingHTTPServer(("127.0.0.1", 0), PreviewHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{http.server_port}"
    errors = []
    try:
        async with async_playwright() as p:
            chrome = os.environ.get("RESEARCH_DESK_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            executable = chrome if Path(chrome).is_file() else p.chromium.executable_path
            browser = await p.chromium.launch(headless=True, executable_path=executable)
            page = await browser.new_page(viewport={"width": 920, "height": 640}, device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(address + "/research_desk.html?pause=1", wait_until="load")
            await page.wait_for_function("window.__researchDesk && window.__researchDesk.pages.length")
            pages = await page.evaluate("window.__researchDesk.pages")
            print("actual RSS page row counts:", pages)
            for index, row_count in enumerate(pages):
                if index:
                    await page.evaluate("window.__researchDesk.next()")
                metrics = await page.evaluate("""() => {
                  const device=document.querySelector('#device'), list=document.querySelector('#paper-list');
                  const rows=[...list.querySelectorAll('.paper-row')];
                  return {size:[device.clientWidth,device.clientHeight],available:list.clientHeight,
                    used:rows.reduce((sum,row)=>sum+row.getBoundingClientRect().height,0),
                    rows:rows.length,largestRow:Math.max(0,...rows.map(row=>row.getBoundingClientRect().height)),
                    titleFont:getComputedStyle(document.querySelector('.paper-title')).fontSize};
                }""")
                assert metrics["size"] == [800, 480], metrics
                assert metrics["rows"] == row_count, metrics
                assert metrics["used"] <= metrics["available"] + 1, metrics
                await capture_device(page, BASE / f"zotero_rss_page{index + 1}.png")
                print(f"feed page {index + 1}:", json.dumps(metrics, ensure_ascii=False))
            await page.evaluate("""() => { while(window.__researchDesk.active.type!=='token') window.__researchDesk.next(); }""")
            ai = await page.evaluate("""() => {
              const screen=document.querySelector('#token-screen'), body=document.querySelector('.token-body');
              return {size:[document.querySelector('#device').clientWidth,document.querySelector('#device').clientHeight],
                overflow:screen.scrollHeight>screen.clientHeight,
                bodyBottom:body.getBoundingClientRect().bottom,screenBottom:screen.getBoundingClientRect().bottom,
                quotas:[document.querySelectorAll('#codex-quota .quota-row').length,document.querySelectorAll('#go-quota .quota-row').length]};
            }""")
            assert ai["size"] == [800, 480] and not ai["overflow"] and ai["bodyBottom"] <= ai["screenBottom"], ai
            await capture_device(page, BASE / "ai_token_page.png")
            print("actual AI page:", json.dumps(ai, ensure_ascii=False))
            assert not errors, errors
            await browser.close()
    finally:
        http.shutdown()
        http.server_close()
    outputs = [*(BASE / f"zotero_rss_page{i + 1}.png" for i in range(len(pages))), BASE / "ai_token_page.png"]
    for output in outputs:
        with Image.open(output) as image:
            assert image.size == (800, 480), (output.name, image.size)
    with Image.new("RGB", (1600, 480), "#f6f4ec") as preview:
        with Image.open(outputs[0]) as feed, Image.open(BASE / "ai_token_page.png") as ai:
            preview.paste(feed.convert("RGB"), (0, 0))
            preview.paste(ai.convert("RGB"), (800, 0))
        preview.save(BASE / "preview_side_by_side.png")
    print(f"Rendered {len(outputs)} native 800x480 PNGs")


if __name__ == "__main__":
    asyncio.run(main())
