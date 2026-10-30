# Project notes

The current implementation and data-source status are documented in [README.md](README.md). Preserve the confirmed V6 compact, 800×480 layout in `research_desk.html`.

Key files:

- `refresh_data.py`: local collectors for Zotero RSS, Codex, OpenCode, and the passive E1002 BLE temperature/humidity advertisement.
- `server.py`: loopback preview and 10-minute data refresh service.
- `data.json`: current real data snapshot, generated locally.
- `demo-data.json`: synthetic data, loaded only with `?demo=1`.
- `render_v6.py`: renders actual 800×480 screen PNGs and checks pagination/overflow.
- `README.md`: setup, data definitions, and known source limitations.
