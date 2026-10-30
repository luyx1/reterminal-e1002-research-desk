# E1002 Design

Research Desk dashboard for the E1002 e-ink display (800×480). Collects Zotero feeds, Codex/OpenCode token usage, OpenCode Go quota, and E1002 sensor data, renders dashboard pages, and uploads them to the paired display.

## Setup

See [research_desk/README.md](research_desk/README.md) for full setup, configuration, and usage.

Quick start:

```bash
cd /path/to/research_desk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

## Notes

- Private state lives in `research_desk/.state/` and is gitignored.
- Generated images/data (`*.png`, `data.json`) are gitignored.
- The E1002 device name is configured via `RESEARCH_DESK_DISPLAY_NAME` or `research_desk/.state/display_name`.