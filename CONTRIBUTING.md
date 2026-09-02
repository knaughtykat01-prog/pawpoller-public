# Contributing to PawPoller

Thanks for your interest in contributing!

## Development Setup

```bash
git clone https://github.com/your-username/PawPoller.git
cd PawPoller
pip install -r requirements.txt
cp .env.example .env  # Edit with your credentials
python main.py        # Desktop mode
# or
python server.py      # Headless/server mode
```

## Adding a New Platform

Each platform follows a consistent pattern:

1. **Client** (`clients/{xx}/client.py`) — HTTP client for the platform's API
2. **Poller** (`polling/{xx}_poller.py`) — Poll cycle orchestration
3. **Database** (`database/{xx}_queries.py` + `{xx}_schema.sql`) — DB schema + queries
4. **Routes** (`routes/{xx}_api.py`) — Dashboard API endpoints
5. **Poster** (`posting/platforms/{xx}.py`) — Upload/edit logic (optional)

Start by reading an existing platform (e.g., `polling/ws_poller.py` for a
simple one, `polling/sf_poller.py` for a more complex one with chaptered
posting). Imports look like `from clients.{xx}.client import {Class}`.

## Code Style

- Python: no strict formatter enforced, but keep consistent with existing code
- JS: plain vanilla JS, no frameworks, no build step
- CSS: CSS custom properties (tokens.css), BEM-ish naming
- Comments: only when the WHY is non-obvious

## Pull Requests

1. Fork and create a feature branch
2. Keep PRs focused — one feature or fix per PR
3. Update CHANGELOG.md with your changes
4. Run `python -m pytest tests/` before submitting
5. JS changes: verify with `node -c frontend/js/*.js`

## Architecture Overview

See [`docs/SETUP.md`](docs/SETUP.md) for the architecture overview; the source is heavily commented throughout.

Key points:
- `main.py` = desktop mode (pywebview + pystray + per-platform pollers)
- `server.py` = headless mode (unified poll orchestrator)
- `dashboard.py` = FastAPI app (shared between both modes)
- All frontend is plain HTML/JS/CSS served as static files
- SQLite database, no ORM
- Settings in `data/settings.json` (gitignored)
