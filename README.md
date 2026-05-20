# trading-app

Indian intraday ORB (Opening Range Breakout) trading app on NSE with an LLM co-pilot.
Paper-first, with the same code path swapping in a live broker once the 2-week paper run clears the go/no-go gates.

## Phase status

| Phase | Scope                                          | Status        |
|-------|------------------------------------------------|---------------|
| 0     | Scaffold, config, DB models, /health           | **complete**  |
| 1     | Angel One auth + WebSocket feed + 1-min bars   | pending       |
| 2     | ORB signal generation                          | pending       |
| 3     | Risk layer + PaperBroker + execution engine    | pending       |
| 4     | LLM co-pilot (regime, pre-trade, EOD)          | pending       |
| 5     | Dashboard (Jinja + HTMX + SSE)                 | pending       |
| 6     | EOD report, kill switch, WS reconnect, holidays| pending       |
| 7     | 2-week paper run + go/no-go check              | pending       |
| 8     | Phased live: ₹10k → ₹25k → ₹50k                | pending       |

## Requirements

- Linux (cloud VM in `ap-south-1` recommended for NSE latency)
- Python 3.11 or 3.12
- [`uv`](https://docs.astral.sh/uv/) for env + package management
- An Angel One account with SmartAPI enabled — see [Section 11 below](#first-morning-checklist)
- A Google Gemini API key (Google AI Studio — `gemini-2.5-flash-lite` for the per-trade tier, `gemini-2.5-pro` for the 15-min regime tier)

## Setup

```bash
# One-time: install uv (no apt/sudo required)
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL   # or source the shell rc so ~/.local/bin is on PATH

make install              # uv sync (creates .venv, installs deps)
cp .env.example .env      # then fill in secrets
make test                 # 4 smoke tests should pass
make run                  # boots FastAPI on :8000
curl localhost:8000/health
```

## Commands

| Command         | What it does                                                |
|-----------------|-------------------------------------------------------------|
| `make install`  | Create venv, install runtime + dev deps                     |
| `make run`      | Start FastAPI orchestrator + dashboard on `:8000`           |
| `make test`     | Run pytest                                                  |
| `make lint`     | ruff check + format check                                   |
| `make format`   | ruff format and fix                                         |
| `make typecheck`| `mypy --strict` on `app/`                                   |
| `make kill`     | Kill switch — squares off positions and halts loop (Phase 6)|
| `make report`   | Generate EOD report (Phase 6)                               |

## Project layout

See the [original brief](docs/brief.md) — only Phase 0 paths exist in the tree today.
Subsequent phases add their own modules in place.

## First-morning checklist

Once Phases 1–3 are in:

1. **The night before**: confirm Angel One credentials work by logging in to <https://www.angelone.in/> manually.
2. **08:50 IST**: SSH to the VM, `cd trading-app`.
3. **09:00 IST**: `python -m app.scripts.auth` — refreshes the SmartAPI feed/JWT tokens for the day.
4. **09:05 IST**: `make run` — boots the orchestrator. Dashboard at `http://<vm-ip>:8000/`.
5. **09:15 IST**: market opens — verify ticks are flowing on the dashboard.
6. **09:30 IST**: opening range is locked; signals can now fire.
7. **15:15 IST**: auto-square-off runs.
8. **15:30 IST**: `make report` (also runs automatically) writes `reports/YYYY-MM-DD.md`.

## Disaster recovery

If the kill switch fails (`make kill` errors, process is hung, network is down):

1. **Log in to <https://trade.angelone.in/>** directly.
2. Open the **Positions** tab.
3. Square off each open intraday position manually (one click per position).
4. Cancel any pending orders from the **Orders** tab.
5. Once flat, kill the process: `pkill -9 -f "uvicorn app.main"` and `pkill -9 -f "python -m app"`.
6. Investigate logs at `logs/app-<date>.log` before restarting.

## Configuration

All settings live in `.env`. Defaults are in `app/config.py`. Risk caps and the
broker mode (`paper` / `live`) **never** read from a database — they're read at
startup and a config change requires a restart, by design.

## Testing

```bash
make test         # pytest -v
make typecheck    # mypy --strict on app/
make lint         # ruff
```

Tests use a per-test temporary SQLite DB (see `tests/conftest.py`).
