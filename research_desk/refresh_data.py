#!/usr/bin/env python3
"""Collect real Research Desk data from local Zotero, Codex, OpenCode and E1002."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import select
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
STATE_DB = STATE_DIR / "research_desk.sqlite3"
DATA_FILE = ROOT / "data.json"
TZ = ZoneInfo(os.environ.get("RESEARCH_DESK_TIMEZONE", "Asia/Shanghai"))
RSS_BASIS = os.environ.get("RESEARCH_DESK_RSS_TODAY_BASIS", "first_seen").lower()
if RSS_BASIS not in {"first_seen", "published"}:
    RSS_BASIS = "first_seen"

ZOTERO_DB = Path(os.environ.get("RESEARCH_DESK_ZOTERO_DB", str(Path.home() / "Zotero/zotero.sqlite"))).expanduser()
OPENCODE_DB = Path(os.environ.get("RESEARCH_DESK_OPENCODE_DB", str(Path.home() / ".local/share/opencode/opencode.db"))).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
CODEX_BIN = os.environ.get("RESEARCH_DESK_CODEX_BIN") or shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
OPENCODE_BIN = os.environ.get("RESEARCH_DESK_OPENCODE_BIN") or shutil.which("opencode") or "/opt/homebrew/bin/opencode"
OPENCODE_API_KEY_FILE = STATE_DIR / "opencode_api_key"


def opencode_api_key() -> str | None:
    value = os.environ.get("RESEARCH_DESK_OPENCODE_API_KEY")
    if value:
        return value.strip()
    try:
        return OPENCODE_API_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


OPENCODE_CONSOLE_BASE = "https://opencode.ai"


def _opencode_console_json(path: str, params: dict[str, str] | None = None) -> Any:
    """Read account-level Console data with the local service API key."""
    key = opencode_api_key()
    if not key:
        raise RuntimeError("OpenCode API key unavailable")
    url = OPENCODE_CONSOLE_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            # Cloudflare rejects the default Python-urllib signature (HTTP 403).
            "User-Agent": "curl/8.7.1",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def opencode_account_usage(now: datetime) -> tuple[dict[str, Any], str]:
    """Read account-wide OpenCode token totals from the Console usage API.

    The Console daily export uses UTC days, but the dashboard is local-time.
    ``since`` lets us request the local day explicitly, matching the number a
    user sees for "today" in their own timezone.
    """
    empty = {"today": None, "week": None, "total": None,
             "input": None, "output": None, "cacheRead": None, "cacheWrite": None,
             "requests": None}
    try:
        daily = _opencode_console_json("/console/api/usage/cost-by-day", {"range": "all"})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        daily = None

    local_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        today_summary = _opencode_console_json("/console/api/usage/summary", {"since": since})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        today_summary = None

    rows: list[tuple[date, int]] = []
    if isinstance(daily, list):
        for row in daily:
            if not isinstance(row, dict):
                continue
            try:
                day = date.fromisoformat(str(row.get("date") or ""))
            except ValueError:
                continue
            rows.append((day, _as_int(row.get("totalTokens"))))
    if not rows and not isinstance(today_summary, dict):
        return empty, "OpenCode account usage unavailable"

    utc_today = now.astimezone(timezone.utc).date()
    week_start = utc_today - timedelta(days=6)
    total = sum(tokens for _, tokens in rows) if rows else None
    week = sum(tokens for day, tokens in rows if week_start <= day <= utc_today) if rows else None

    if isinstance(today_summary, dict):
        input_tokens = _as_int(today_summary.get("totalInputTokens"))
        output_tokens = _as_int(today_summary.get("totalOutputTokens"))
        cache_read = _as_int(today_summary.get("totalCacheReadTokens"))
        cache_write = _as_int(today_summary.get("totalCacheWrite5mTokens")) + _as_int(today_summary.get("totalCacheWrite1hTokens"))
        today = input_tokens + output_tokens + cache_read + cache_write
        requests = _as_int(today_summary.get("totalRequests"))
    else:
        input_tokens = output_tokens = cache_read = cache_write = None
        today = next((tokens for day, tokens in rows if day == utc_today), None)
        requests = None

    return {
        "today": today,
        "week": week,
        "total": total,
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "requests": requests,
        "date": now.date().isoformat(),
    }, "ok"


MAX_FEED_BYTES = 5 * 1024 * 1024
RSS_TIMEOUT_SECONDS = 8
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.I)


def now_local() -> datetime:
    return datetime.now(TZ)


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.date(), dtime.min, tzinfo=TZ)
    return start, now


def db_connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
    db = sqlite3.connect(STATE_DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS articles (
            article_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            doi TEXT,
            guid TEXT,
            url TEXT,
            published_at TEXT,
            first_seen TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codex_files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            offset INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codex_usage (
            response_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            ts REAL NOT NULL,
            input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS codex_usage_ts ON codex_usage(ts);
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            temperature_c REAL NOT NULL,
            humidity_pct REAL NOT NULL,
            observed_at TEXT NOT NULL
        );
        """
    )
    try:
        STATE_DB.chmod(0o600)
    except OSError:
        pass
    return db


def normalized_doi(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = DOI_RE.search(value.strip().removeprefix("doi:"))
        if match:
            doi = match.group(0).rstrip(".,;:)]}")
            return doi.lower()
    return None


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if host.startswith("www."):
            host = host[4:]
        port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
        path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
        return urllib.parse.urlunsplit((parts.scheme.lower(), host + port, path, "", ""))
    except (ValueError, TypeError):
        return None


def article_key(doi: str | None, guid: str | None, url: str | None) -> str | None:
    if doi:
        return "doi:" + doi
    if guid and guid.strip():
        return "guid:" + guid.strip()
    canonical = canonical_url(url)
    return "url:" + canonical if canonical else None


def upsert_article(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    title = (item.get("title") or "").strip()
    if not title:
        return False
    doi = normalized_doi(item.get("doi"), item.get("url"), item.get("guid"))
    guid = (item.get("guid") or "").strip() or None
    url = canonical_url(item.get("url")) or item.get("url")
    key = article_key(doi, guid, url)
    matches = []
    values: list[Any] = []
    for column, value in (("doi", doi), ("guid", guid), ("url", canonical_url(url))):
        if value:
            matches.append(f"{column}=?")
            values.append(value)
    if matches:
        existing = db.execute(
            "SELECT article_key FROM articles WHERE " + " OR ".join(matches) +
            " ORDER BY first_seen ASC LIMIT 1", values,
        ).fetchone()
        if existing:
            key = str(existing[0])
    if not key:
        key = "title:" + hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()
    first_seen = item.get("first_seen") or to_utc_iso(now_local())
    published_at = item.get("published_at")
    cur = db.execute(
        """INSERT INTO articles(article_key,title,doi,guid,url,published_at,first_seen,source)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(article_key) DO UPDATE SET
             title=excluded.title,
             doi=COALESCE(articles.doi,excluded.doi),
             url=COALESCE(articles.url,excluded.url),
             published_at=COALESCE(articles.published_at,excluded.published_at),
             first_seen=CASE WHEN excluded.first_seen < articles.first_seen THEN excluded.first_seen ELSE articles.first_seen END""",
        (key, title, doi, guid, url, published_at, first_seen, item.get("source") or "Zotero Feeds"),
    )
    return cur.rowcount > 0


def read_zotero_cache(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[tuple[int, str, str]]]:
    if not ZOTERO_DB.is_file():
        raise FileNotFoundError("Zotero database not found")
    source = sqlite3.connect(f"file:{ZOTERO_DB}?mode=ro&immutable=1", uri=True, timeout=5)
    source.row_factory = sqlite3.Row
    try:
        field_ids = {row["fieldName"]: row["fieldID"] for row in source.execute(
            "SELECT fieldID,fieldName FROM fields WHERE fieldName IN ('title','date','DOI','url')"
        )}
        feeds = [(int(r["libraryID"]), str(r["name"]), str(r["url"])) for r in source.execute(
            "SELECT libraryID,name,url FROM feeds ORDER BY name"
        )]
        feed_items: list[dict[str, Any]] = []
        if feeds:
            title_id = field_ids.get("title", -1)
            date_id = field_ids.get("date", -1)
            doi_id = field_ids.get("DOI", -1)
            url_id = field_ids.get("url", -1)
            query = """
                SELECT f.name AS source, fi.guid AS guid, i.dateAdded AS first_seen,
                       MAX(CASE WHEN d.fieldID=? THEN v.value END) AS title,
                       MAX(CASE WHEN d.fieldID=? THEN v.value END) AS published_at,
                       MAX(CASE WHEN d.fieldID=? THEN v.value END) AS doi,
                       MAX(CASE WHEN d.fieldID=? THEN v.value END) AS url
                  FROM feeds f
                  JOIN items i ON i.libraryID=f.libraryID
                  JOIN feedItems fi ON fi.itemID=i.itemID
             LEFT JOIN itemData d ON d.itemID=i.itemID
             LEFT JOIN itemDataValues v ON v.valueID=d.valueID
              GROUP BY i.itemID
            """
            for r in source.execute(query, (title_id, date_id, doi_id, url_id)):
                first_seen = parse_date(r["first_seen"])
                published = parse_date(r["published_at"])
                feed_items.append({
                    "source": r["source"], "guid": r["guid"], "title": r["title"],
                    "published_at": to_utc_iso(published) if published else None,
                    "first_seen": to_utc_iso(first_seen) if first_seen else to_utc_iso(now_local()),
                    "doi": r["doi"], "url": r["url"],
                })
        return feed_items, feeds
    finally:
        source.close()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def text_of(parent: ET.Element, names: set[str]) -> str | None:
    for child in list(parent):
        if local_name(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return None


def feed_entries(payload: bytes, feed_name: str, observed: datetime) -> list[dict[str, Any]]:
    if len(payload) > MAX_FEED_BYTES or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("feed document rejected")
    root = ET.fromstring(payload)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    found: list[dict[str, Any]] = []
    for node in entries:
        title = text_of(node, {"title"})
        guid = text_of(node, {"guid", "id"})
        pub = text_of(node, {"pubdate", "published", "updated", "date", "issued"})
        url = None
        for child in list(node):
            if local_name(child.tag) == "link":
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")
                value = href or (child.text or "").strip()
                if value and (rel == "alternate" or not url):
                    url = value
                    if rel == "alternate":
                        break
        if not url:
            url = text_of(node, {"link"})
        published = parse_date(pub)
        found.append({
            "source": feed_name, "title": title, "guid": guid, "url": url,
            "doi": normalized_doi(guid, url, title),
            "published_at": to_utc_iso(published) if published else None,
            "first_seen": to_utc_iso(observed),
        })
    return found


def fetch_feed(feed: tuple[int, str, str]) -> tuple[str, list[dict[str, Any]], str | None]:
    _, name, url = feed
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            return name, [], "unsupported scheme"
        request = urllib.request.Request(url, headers={"User-Agent": "ResearchDesk/1.0 (RSS reader)"})
        with urllib.request.urlopen(request, timeout=RSS_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_FEED_BYTES + 1)
        return name, feed_entries(raw, name, now_local()), None
    except urllib.error.HTTPError as exc:
        return name, [], f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return name, [], "network error"
    except (ET.ParseError, ValueError):
        return name, [], "invalid feed"


def sync_zotero(db: sqlite3.Connection) -> dict[str, Any]:
    cached, feeds = read_zotero_cache(db)
    db.execute("BEGIN")
    for item in cached:
        upsert_article(db, item)
    db.commit()
    if not feeds:
        return {"status": "no feeds", "feeds": 0, "cached": len(cached), "fetched": 0, "failed": 0}
    fetched = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=min(6, len(feeds))) as pool:
        results = list(pool.map(fetch_feed, feeds))
    db.execute("BEGIN")
    for _name, items, error in results:
        if error:
            errors += 1
            continue
        fetched += len(items)
        for item in items:
            upsert_article(db, item)
    db.commit()
    return {"status": "ok" if errors < len(feeds) else "stale", "feeds": len(feeds), "cached": len(cached), "fetched": fetched, "failed": errors}


def today_articles(db: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    start, end = local_day_bounds(now)
    day = now.date()
    all_rows = db.execute(
        "SELECT article_key,title,doi,guid,url,published_at,first_seen,source FROM articles ORDER BY first_seen DESC"
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in all_rows:
        first_seen = parse_date(row["first_seen"])
        published = parse_date(row["published_at"])
        include = (first_seen is not None and first_seen.date() == day) if RSS_BASIS == "first_seen" else (published is not None and published.date() == day)
        if not include:
            continue
        aliases = set()
        if row["doi"]:
            aliases.add("doi:" + str(row["doi"]).lower())
        if row["guid"]:
            aliases.add("guid:" + str(row["guid"]).strip())
        canonical = canonical_url(row["url"])
        if canonical:
            aliases.add("url:" + canonical)
        if not aliases:
            aliases.add(str(row["article_key"]))
        if aliases & seen:
            continue
        seen.update(aliases)
        result.append({
            "id": row["article_key"], "title": row["title"], "doi": row["doi"],
            "url": row["url"], "publishedAt": row["published_at"],
            "firstSeen": row["first_seen"], "source": row["source"],
        })
    result.sort(key=lambda r: (r["firstSeen"] or ""), reverse=True)
    return result


def parse_epoch_or_iso(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value > 10_000_000_000 else float(value)
    parsed = parse_date(str(value)) if value else None
    return parsed.timestamp() if parsed else None


def index_codex_files(db: sqlite3.Connection) -> dict[str, Any]:
    scanned = 0
    added = 0
    if not CODEX_HOME.exists():
        return {"status": "not found", "records": 0}
    paths = list(CODEX_HOME.rglob("rollout-*.jsonl"))
    for path in paths:
        try:
            stat = path.stat()
            key = str(path)
            cursor = db.execute("SELECT size,mtime_ns,offset FROM codex_files WHERE path=?", (key,)).fetchone()
            if cursor and cursor[0] == stat.st_size and cursor[1] == stat.st_mtime_ns:
                continue
            start = int(cursor[2]) if cursor and stat.st_size >= int(cursor[2]) else 0
            if start:
                try:
                    with path.open("rb") as stream:
                        stream.seek(start - 1)
                        if stream.read(1) != b"\n":
                            start = 0
                except OSError:
                    start = 0
            with path.open("rb") as stream:
                stream.seek(start)
                chunk = stream.read()
            last_newline = chunk.rfind(b"\n")
            if last_newline < 0:
                complete, new_offset = b"", start
            else:
                complete, new_offset = chunk[:last_newline + 1], start + last_newline + 1
            scanned += 1
            for line_number, raw in enumerate(complete.splitlines(), start=1):
                if b"token_usage_record" not in raw:
                    continue
                try:
                    event = json.loads(raw)
                    if event.get("type") != "token_usage_record":
                        continue
                    payload = event.get("payload") or {}
                    usage = payload.get("usage") or {}
                    ts = parse_epoch_or_iso(event.get("timestamp"))
                    if ts is None:
                        continue
                    response = payload.get("response_id") or payload.get("responseId")
                    if not response:
                        response = hashlib.sha256(raw).hexdigest()
                    response_id = str(response)
                    result = db.execute(
                        """INSERT OR IGNORE INTO codex_usage
                           (response_id,source_path,ts,input_tokens,cached_input_tokens,cache_write_tokens,output_tokens)
                           VALUES(?,?,?,?,?,?,?)""",
                        (response_id, key, ts, int(usage.get("input_tokens") or 0),
                         int(usage.get("cached_input_tokens") or 0),
                         int(usage.get("cache_write_input_tokens") or 0),
                         int(usage.get("output_tokens") or 0)),
                    )
                    added += result.rowcount
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
            db.execute(
                "INSERT INTO codex_files(path,size,mtime_ns,offset) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,offset=excluded.offset",
                (key, stat.st_size, stat.st_mtime_ns, new_offset),
            )
        except OSError:
            continue
    db.commit()
    count = db.execute("SELECT count(*) FROM codex_usage").fetchone()[0]
    return {"status": "ok" if paths else "no records", "files": len(paths), "updatedFiles": scanned, "newRecords": added, "records": count}


def sum_codex(db: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    local_start, _ = local_day_bounds(now)
    now_epoch = now.timestamp()
    today_epoch = local_start.timestamp()
    week_epoch = (now - timedelta(days=7)).timestamp()

    def total(since: float) -> dict[str, int]:
        row = db.execute(
            "SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),"
            "COALESCE(SUM(cache_write_tokens),0),COALESCE(SUM(output_tokens),0) "
            "FROM codex_usage WHERE ts>=? AND ts<=?", (since, now_epoch),
        ).fetchone()
        incoming, cached, cache_write, outgoing = map(int, row)
        return {"total": max(0, incoming - cached) + cached + cache_write + outgoing,
                "input": max(0, incoming - cached), "output": outgoing,
                "cacheRead": cached + cache_write}
    return {"today": total(today_epoch), "week": total(week_epoch)}


def codex_rate_limits() -> tuple[list[dict[str, Any]], str]:
    if not Path(CODEX_BIN).is_file() and not shutil.which(str(CODEX_BIN)):
        return [], "Codex CLI unavailable"
    initialize = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "research-desk", "version": "1.0"}}}
    read_limits = {"id": 2, "method": "account/rateLimits/read", "params": {}}
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen([str(CODEX_BIN), "app-server", "--stdio"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        assert process.stdin and process.stdout

        def send(value: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def read_message(deadline: float) -> dict[str, Any] | None:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0.05, deadline - time.monotonic()))
                if not ready:
                    return None
                line = process.stdout.readline()
                if not line:
                    return None
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return None

        send(initialize)
        initialized = False
        until = time.monotonic() + 10
        while time.monotonic() < until:
            message = read_message(until)
            if message is None:
                break
            if message.get("id") == 1 and message.get("result"):
                initialized = True
                break
        if not initialized:
            return [], "Codex app-server initialization failed"
        send({"method": "initialized", "params": {}})
        send(read_limits)
        response = None
        until = time.monotonic() + 12
        while time.monotonic() < until:
            value = read_message(until)
            if value is None:
                break
            if value.get("id") == 2:
                response = value.get("result") or {}
                break
        if not response:
            return [], "Codex rate-limit response unavailable"
        root = response.get("rateLimitsByLimitId") or response.get("rate_limits_by_limit_id") or {}
        codex = root.get("codex") or root.get("chatgpt") or {}
        windows = [("5 小时", codex.get("primary") or codex.get("fiveHour")),
                   ("每周", codex.get("secondary") or codex.get("weekly"))]
        rows = []
        for label, window in windows:
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent", window.get("used_percent"))
            reset = window.get("resetsAt", window.get("resets_at"))
            reset_dt = datetime.fromtimestamp(float(reset), TZ) if reset else None
            reset_label = "重置时间 N/A"
            if reset_dt:
                reset_label = (reset_dt.strftime("%H:%M") if reset_dt.date() == now_local().date()
                               else reset_dt.strftime("%-m月%-d日 %H:%M")) + " 重置"
            rows.append({"label": label, "usedPercent": float(used) if used is not None else None, "resetLabel": reset_label})
        return rows, "ok" if rows else "Codex rate-limit fields unavailable"
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], "Codex rate-limit request failed"
    finally:
        if process:
            try:
                if process.stdin and not process.stdin.closed:
                    process.stdin.write(json.dumps({"method": "exit", "params": {}}) + "\n")
                    process.stdin.close()
                process.wait(timeout=1)
            except Exception:
                process.kill()


def codex_account_usage(now: datetime) -> tuple[dict[str, Any], str]:
    """Read account-level Codex token totals from the official app-server API.

    ``account/usage/read`` returns ChatGPT-account usage, including lifetime
    tokens and daily buckets.  It is not device-local history.
    """
    empty = {"today": None, "total": None, "peakDaily": None,
             "currentStreak": None, "longestStreak": None}
    if not Path(CODEX_BIN).is_file() and not shutil.which(str(CODEX_BIN)):
        return empty, "Codex CLI unavailable"

    initialize = {"id": 1, "method": "initialize",
                  "params": {"clientInfo": {"name": "research-desk", "version": "1.0"}}}
    read_usage = {"id": 2, "method": "account/usage/read", "params": {}}
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen([str(CODEX_BIN), "app-server", "--stdio"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1)
        assert process.stdin and process.stdout

        def send(value: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def read_message(deadline: float) -> dict[str, Any] | None:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [],
                                            max(0.05, deadline - time.monotonic()))
                if not ready:
                    return None
                line = process.stdout.readline()
                if not line:
                    return None
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return None

        send(initialize)
        initialized = False
        until = time.monotonic() + 10
        while time.monotonic() < until:
            message = read_message(until)
            if message is None:
                break
            if message.get("id") == 1 and message.get("result"):
                initialized = True
                break
        if not initialized:
            return empty, "Codex app-server initialization failed"
        send({"method": "initialized", "params": {}})
        send(read_usage)
        response = None
        until = time.monotonic() + 12
        while time.monotonic() < until:
            value = read_message(until)
            if value is None:
                break
            if value.get("id") == 2:
                response = value.get("result") or {}
                break
        if not response:
            return empty, "Codex usage response unavailable"

        summary = response.get("summary") or {}
        lifetime = _as_int(summary.get("lifetimeTokens"))
        peak = _as_int(summary.get("peakDailyTokens"))
        current = _as_int(summary.get("currentStreakDays"))
        longest = _as_int(summary.get("longestStreakDays"))
        today: int | None = None
        week = 0
        week_start = now.date() - timedelta(days=6)
        for bucket in response.get("dailyUsageBuckets") or []:
            if not isinstance(bucket, dict):
                continue
            start = str(bucket.get("startDate") or "")
            tokens = _as_int(bucket.get("tokens"))
            if start == now.date().isoformat():
                today = tokens
            try:
                day = date.fromisoformat(start)
                if week_start <= day <= now.date():
                    week += tokens
            except ValueError:
                pass
        if today is None:
            today = 0
        if lifetime == 0 and not (response.get("dailyUsageBuckets") or []):
            return empty, "Codex usage fields unavailable"
        return {
            "today": today,
            "total": lifetime,
            "week": week,
            "peakDaily": peak,
            "currentStreak": current,
            "longestStreak": longest,
            "input": None,
            "output": None,
            "cacheRead": None,
        }, "ok"
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return empty, "Codex account usage request failed"
    finally:
        if process:
            try:
                if process.stdin and not process.stdin.closed:
                    process.stdin.write(json.dumps({"method": "exit", "params": {}}) + "\n")
                    process.stdin.close()
                process.wait(timeout=1)
            except Exception:
                process.kill()


def opencode_metrics(now: datetime) -> tuple[dict[str, Any], str]:
    if not OPENCODE_DB.is_file():
        return {"today": None, "week": None, "input": None, "output": None, "cacheRead": None}, "OpenCode database not found"
    source = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = source.execute("SELECT id,time_created,data FROM session_message WHERE type='assistant' ORDER BY time_created").fetchall()
    except sqlite3.Error:
        source.close()
        return {"today": None, "week": None, "input": None, "output": None, "cacheRead": None}, "OpenCode database schema unavailable"
    source.close()
    local_start, _ = local_day_bounds(now)
    today_epoch = local_start.timestamp()
    week_epoch = (now - timedelta(days=7)).timestamp()
    now_epoch = now.timestamp()
    totals = {"today": {"input": 0, "output": 0, "cacheRead": 0},
              "week": {"input": 0, "output": 0, "cacheRead": 0}}
    latest_go: tuple[int, str] | None = None
    count = 0
    for message_id, db_ts, raw in rows:
        try:
            obj = json.loads(raw)
            msg_time = (obj.get("time") or {}).get("created")
            ts_ms = msg_time if isinstance(msg_time, (int, float)) else db_ts
            ts = float(ts_ms) / 1000 if ts_ms and float(ts_ms) > 10_000_000_000 else float(ts_ms or 0)
            if ts < week_epoch or ts > now_epoch:
                continue
            token = obj.get("tokens") or {}
            cache = token.get("cache") or {}
            metrics = {"input": int(token.get("input") or 0),
                       "output": int(token.get("output") or 0) + int(token.get("reasoning") or 0),
                       "cacheRead": int(cache.get("read") or 0) + int(cache.get("write") or 0)}
            for period in ("today", "week"):
                if period == "week" or ts >= today_epoch:
                    for key, value in metrics.items():
                        totals[period][key] += value
            model = obj.get("model") or {}
            model_id = str(model.get("id") or "")
            if model.get("providerID") == "opencode-go" and model_id:
                latest_go = (ts, model_id)
            count += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    result = {}
    for period, values in totals.items():
        result[period] = sum(values.values())
    result.update(totals["today"])
    result["weekBreakdown"] = totals["week"]
    return {"data": result, "goModel": latest_go[1] if latest_go else None, "messages": count}, "ok"


def opencode_go_limits() -> tuple[list[dict[str, Any]], str]:
    """Read Go limits through the authenticated OpenCode local API client."""
    missing = [{"label": label, "usedPercent": None, "resetLabel": "额度 N/A"}
               for label in ("5 小时", "每周", "每月")]

    # Prefer the account-level OpenCode Go usage endpoint when an API key is available.
    key = opencode_api_key()
    if key:
        try:
            request = urllib.request.Request(
                "https://opencode.ai/zen/go/v1/usage",
                headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            quota = payload.get("usage") if isinstance(payload, dict) else None
            if isinstance(quota, dict):
                rows: list[dict[str, Any]] = []
                for name, label in (("rolling", "5 小时"), ("weekly", "每周"), ("monthly", "每月")):
                    item = quota.get(name)
                    if not isinstance(item, dict):
                        continue
                    used = item.get("percent")
                    try:
                        used = float(used) if used is not None else None
                        if used is not None and not 0 <= used <= 100:
                            used = None
                    except (TypeError, ValueError):
                        used = None
                    reset = item.get("resetsAt")
                    if reset is not None:
                        reset_dt = parse_date(str(reset))
                        if reset_dt is not None:
                            if reset_dt.date() == now_local().date():
                                reset_label = reset_dt.strftime("%H:%M") + " 重置"
                            else:
                                reset_label = reset_dt.strftime("%-m月%-d日 %H:%M") + " 重置"
                        else:
                            reset_label = "重置时间 N/A"
                    else:
                        reset_label = "重置时间 N/A"
                    rows.append({"label": label, "usedPercent": used, "resetLabel": reset_label,
                                 "limited": bool(item.get("limited"))})
                if rows:
                    return rows, "ok"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            pass

    binary = OPENCODE_BIN
    if not binary or (not Path(binary).is_file() and not shutil.which(binary)):
        for row in missing:
            row["resetLabel"] = "未找到 OpenCode CLI"
        return missing, "OpenCode CLI unavailable"

    # `opencode api` discovers the local service and uses its own authentication.
    # Console sign-in is already managed by OpenCode; no separate Go API key is needed.
    endpoint = "/api/experimental/usage/limits?directory=" + urllib.parse.quote(str(ROOT))
    try:
        run_env = dict(os.environ)
        run_env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + run_env.get("PATH", "")
        process = subprocess.run([binary, "api", "get", endpoint], capture_output=True,
                                 text=True, timeout=12, check=False, env=run_env)
    except (OSError, subprocess.SubprocessError):
        for row in missing:
            row["resetLabel"] = "OpenCode 本地接口不可用"
        return missing, "OpenCode local API unavailable"

    if process.returncode:
        detail = (process.stderr + " " + process.stdout).casefold()
        if "404" in detail or "not found" in detail:
            for row in missing:
                row["resetLabel"] = "当前 OpenCode 版本未提供限额接口"
            return missing, "OpenCode usage-limits route unavailable (HTTP 404)"
        if "401" in detail or "unauthorized" in detail:
            for row in missing:
                row["resetLabel"] = "OpenCode 登录状态不可读"
            return missing, "OpenCode local API unauthorized"
        for row in missing:
            row["resetLabel"] = "OpenCode 限额请求失败"
        return missing, "OpenCode usage-limits request failed"

    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        for row in missing:
            row["resetLabel"] = "OpenCode 限额响应无法解析"
        return missing, "OpenCode usage-limits response invalid"

    # Current API response is {location, data: {workspace, go, budgets}}.
    info = payload.get("data") if isinstance(payload, dict) else None
    quota = info.get("go") if isinstance(info, dict) else None
    if not isinstance(quota, dict):
        for row in missing:
            row["resetLabel"] = "当前账户未返回 Go 额度"
        return missing, "OpenCode Console returned no Go allowance"

    rows: list[dict[str, Any]] = []
    for name, label in (("rolling", "5 小时"), ("weekly", "每周"), ("monthly", "每月")):
        item = quota.get(name)
        if not isinstance(item, dict):
            continue
        used = item.get("percent")
        try:
            used = float(used) if used is not None else None
            if used is not None and not 0 <= used <= 100:
                used = None
        except (TypeError, ValueError):
            used = None
        reset = item.get("resetsAt")
        if reset is not None:
            try:
                reset_dt = datetime.fromtimestamp(float(reset) / 1000, TZ)
                if reset_dt.date() == now_local().date():
                    reset_label = reset_dt.strftime("%H:%M") + " 重置"
                else:
                    reset_label = reset_dt.strftime("%-m月%-d日 %H:%M") + " 重置"
            except (TypeError, ValueError, OSError, OverflowError):
                reset_label = "重置时间 N/A"
        else:
            reset_label = "重置时间 N/A"
        rows.append({"label": label, "usedPercent": used, "resetLabel": reset_label,
                     "limited": bool(item.get("limited"))})
    return rows or missing, "ok" if rows else "OpenCode Console returned no Go usage windows"


def decode_e1002_sht40(raw: bytes, start: int = 1) -> tuple[float, float] | None:
    if len(raw) != 14:
        return None
    slot = raw[start:start + 3]
    if len(slot) != 3:
        return None
    packed = int.from_bytes(slot, "little")
    if packed in (0, 0xFFFFFF):
        return None
    humidity_deci = packed & 0x3FF
    temperature_units = (packed >> 10) & 0x7FF
    if humidity_deci > 1000 or temperature_units > 1650:
        return None
    return (temperature_units - 400) / 10.0, humidity_deci / 10.0


async def passive_e1002_scan(seconds: float = 8.0) -> tuple[float, float] | None:
    from bleak import BleakScanner  # optional dependency; no BLE connection is opened
    readings: list[tuple[float, float]] = []

    def callback(_device: Any, advertisement: Any) -> None:
        payload = advertisement.manufacturer_data.get(0x2446)
        if payload:
            value = decode_e1002_sht40(bytes(payload), start=1)
            if value:
                readings.append(value)

    scanner = BleakScanner(callback)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()
    return readings[-1] if readings else None


def sample_environment(db: sqlite3.Connection) -> dict[str, Any]:
    status = "BLE scanner unavailable"
    try:
        reading = asyncio.run(passive_e1002_scan(float(os.environ.get("RESEARCH_DESK_BLE_SCAN_SECONDS", "8"))))
        if reading:
            db.execute("INSERT OR REPLACE INTO sensor_readings(id,temperature_c,humidity_pct,observed_at) VALUES(1,?,?,?)",
                       (reading[0], reading[1], to_utc_iso(now_local())))
            db.commit()
            status = "ok"
        else:
            status = "no E1002 SHT40 advertisement"
    except ImportError:
        status = "bleak not installed"
    except Exception as exc:
        # Do not return exception text: BLE stacks may include device addresses.
        status = "BLE scan failed: " + type(exc).__name__
    row = db.execute("SELECT temperature_c,humidity_pct,observed_at FROM sensor_readings WHERE id=1").fetchone()
    if row:
        return {"temperatureC": float(row[0]), "humidityPct": float(row[1]), "observedAt": row[2]}, status
    return {"temperatureC": None, "humidityPct": None, "observedAt": None}, status


def cached_environment(db: sqlite3.Connection) -> tuple[dict[str, Any], str]:
    row = db.execute("SELECT temperature_c,humidity_pct,observed_at FROM sensor_readings WHERE id=1").fetchone()
    if row:
        return {"temperatureC": float(row[0]), "humidityPct": float(row[1]), "observedAt": row[2]}, "cached"
    return {"temperatureC": None, "humidityPct": None, "observedAt": None}, "no sensor reading"


RSS_SYNC_STATE = STATE_DIR / "rss_last_sync"


def _rss_sync_needed(now: datetime) -> bool:
    try:
        return RSS_SYNC_STATE.read_text(encoding="utf-8").strip() != now.date().isoformat()
    except OSError:
        return True


def _mark_rss_synced(now: datetime) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    RSS_SYNC_STATE.write_text(now.date().isoformat() + "\n", encoding="utf-8")
    try:
        RSS_SYNC_STATE.chmod(0o600)
    except OSError:
        pass


def collect(sample_sensor: bool = True, sync_rss: bool | None = None) -> dict[str, Any]:
    now = now_local()
    db = db_connect()
    try:
        if sync_rss is None:
            sync_rss = _rss_sync_needed(now)
        if sync_rss:
            try:
                rss_status = sync_zotero(db)
            except (OSError, sqlite3.Error, ValueError) as exc:
                rss_status = {"status": "stale", "feeds": 0, "cached": 0, "fetched": 0,
                              "failed": 0, "error": type(exc).__name__}
            else:
                _mark_rss_synced(now)
        else:
            rss_status = {"status": "cached", "feeds": 0, "cached": 0, "fetched": 0, "failed": 0}
        codex_status = index_codex_files(db)
        codex_usage = sum_codex(db, now)
        codex_quotas, codex_quota_status = codex_rate_limits()
        codex_account, codex_account_status = codex_account_usage(now)
        if not codex_quotas:
            codex_quotas = [{"label": "额度", "usedPercent": None, "resetLabel": "Codex 限额不可读"}]
        opencode, opencode_status = opencode_metrics(now)
        opencode_account, opencode_account_status = opencode_account_usage(now)
        go_quotas, go_status = opencode_go_limits()
        environment, sensor_status = sample_environment(db) if sample_sensor else cached_environment(db)
        rss_items = today_articles(db, now)
        payload = {
            "updatedAt": now.isoformat(timespec="seconds"),
            "timezone": str(TZ),
            "environment": environment,
            "rss": {"date": now.date().isoformat(), "basis": RSS_BASIS, "items": rss_items},
            "tokens": {
                "codex": {
                    "today": codex_account.get("today"),
                    "total": codex_account.get("total"),
                    "week": codex_account.get("week"),
                    "peakDaily": codex_account.get("peakDaily"),
                    "currentStreak": codex_account.get("currentStreak"),
                    "longestStreak": codex_account.get("longestStreak"),
                    "quotas": codex_quotas,
                },
                "opencode": opencode_account,
                "go": {"model": opencode.get("goModel"), "quotas": go_quotas},
            },
            "sources": {
                "zotero": rss_status,
                "codex": {**codex_status, "quota": codex_quota_status,
                          "accountUsage": codex_account_status},
                "opencode": {"status": opencode_status, "accountUsage": opencode_account_status,
                             "assistantMessages7d": opencode.get("messages", 0), "goQuota": go_status},
                "environment": sensor_status,
            },
        }
        return payload
    finally:
        db.close()


def write_payload(payload: dict[str, Any]) -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o644)
    except OSError:
        pass
    tmp.replace(DATA_FILE)


def main() -> int:
    try:
        payload = collect()
        write_payload(payload)
    except Exception as exc:
        print(f"refresh failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    sources = payload["sources"]
    print("Research Desk data refreshed")
    print(f"Zotero: {len(payload['rss']['items'])} today / {sources['zotero'].get('feeds', 0)} feeds / {sources['zotero'].get('failed', 0)} failed")
    print(f"Codex: {payload['tokens']['codex']['today']} today / {payload['tokens']['codex']['total']} total; "
          f"account usage {sources['codex']['accountUsage']}; quota {sources['codex']['quota']}")
    print(f"OpenCode: {payload['tokens']['opencode']['today']} today / {payload['tokens']['opencode']['total']} total; "
          f"account usage {sources['opencode']['accountUsage']}; Go quota {sources['opencode']['goQuota']}")
    env = payload["environment"]
    if env["temperatureC"] is None:
        print(f"E1002 SHT40: N/A ({sources['environment']})")
    else:
        print(f"E1002 SHT40: {env['temperatureC']:.1f} C / {env['humidityPct']:.1f}% RH ({sources['environment']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
