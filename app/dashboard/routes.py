"""FastAPI router for the dashboard.

Routes:
  GET /             - full page render (all sections seeded; HTMX takes over)
  GET /partials/pnl
  GET /partials/positions
  GET /partials/signals
  GET /partials/regime
  GET /partials/fills
  GET /partials/risk-blocks
  GET /partials/ticker

Each partial route returns an HTML fragment scoped to one section. HTMX on
the client side does `hx-get` against these every 2 seconds and swaps the
fragment in place.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.dashboard import queries
from app.dashboard.auth import require_auth
from app.journal.db import get_session_factory

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Every dashboard route is gated by require_auth. When DASHBOARD_USER /
# DASHBOARD_PASSWORD are blank in .env, require_auth returns immediately
# (dev mode). When both are set, browsers get a native login prompt.
router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_auth)])


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"title": "trading-app"})


@router.get("/partials/pnl", response_class=HTMLResponse)
async def partial_pnl(request: Request) -> HTMLResponse:
    settings = get_settings()
    summary = await queries.get_pnl_summary(get_session_factory(), capital_inr=settings.capital_inr)
    return templates.TemplateResponse(request, "partials/pnl.html", {"s": summary})


@router.get("/partials/positions", response_class=HTMLResponse)
async def partial_positions(request: Request) -> HTMLResponse:
    rows = await queries.get_open_positions(get_session_factory())
    return templates.TemplateResponse(request, "partials/positions.html", {"rows": rows})


@router.get("/partials/signals", response_class=HTMLResponse)
async def partial_signals(request: Request) -> HTMLResponse:
    rows = await queries.get_today_signals(get_session_factory())
    return templates.TemplateResponse(request, "partials/signals.html", {"rows": rows})


@router.get("/partials/regime", response_class=HTMLResponse)
async def partial_regime(request: Request) -> HTMLResponse:
    regime = await queries.get_latest_regime(get_session_factory())
    return templates.TemplateResponse(request, "partials/regime.html", {"r": regime})


@router.get("/partials/fills", response_class=HTMLResponse)
async def partial_fills(request: Request) -> HTMLResponse:
    sf = get_session_factory()
    fills = await queries.get_recent_fills(sf)
    stats = await queries.get_slippage_stats(sf)
    return templates.TemplateResponse(
        request, "partials/fills.html", {"rows": fills, "stats": stats}
    )


@router.get("/partials/risk-blocks", response_class=HTMLResponse)
async def partial_risk_blocks(request: Request) -> HTMLResponse:
    rows = await queries.get_risk_blocks_today(get_session_factory())
    return templates.TemplateResponse(request, "partials/risk_blocks.html", {"rows": rows})


@router.get("/partials/ticker", response_class=HTMLResponse)
async def partial_ticker(request: Request) -> HTMLResponse:
    rows = await queries.get_last_ticks_per_symbol(get_session_factory())
    return templates.TemplateResponse(request, "partials/ticker.html", {"rows": rows})
