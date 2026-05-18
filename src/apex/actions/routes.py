"""FastAPI action routes for Pushover action links."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from apex.actions import handlers

router = APIRouter(prefix="/action", tags=["actions"])


def _html(title: str, body: str, color: str = "#1a1a2e") -> HTMLResponse:
    content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APEX — {title}</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: {color};
           color: #eee; display: flex; align-items: center;
           justify-content: center; height: 100vh; margin: 0; }}
    .card {{ background: #16213e; padding: 2rem; border-radius: 12px;
             max-width: 420px; text-align: center; }}
    h2 {{ color: #e94560; margin-top: 0; }}
    p {{ line-height: 1.6; }}
    .symbol {{ font-size: 1.4rem; font-weight: bold; color: #fff; }}
    .detail {{ font-size: 0.9rem; color: #aaa; }}
  </style>
</head>
<body>
  <div class="card">{body}</div>
</body>
</html>"""
    return HTMLResponse(content=content)


# ------------------------------------------------------------------ alerts

@router.get("/alerts/{alert_uid}/enter", response_class=HTMLResponse)
async def action_enter(
    alert_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_enter(alert_uid, token)
    return _html(result["title"], result["html"])


@router.get("/alerts/{alert_uid}/skip", response_class=HTMLResponse)
async def action_skip(
    alert_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_skip(alert_uid, token)
    return _html(result["title"], result["html"])


@router.get("/alerts/{alert_uid}/snooze-15", response_class=HTMLResponse)
async def action_snooze(
    alert_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_snooze(alert_uid, token)
    return _html(result["title"], result["html"])


# ------------------------------------------------------------------ trades

@router.get("/trades/{trade_uid}/win", response_class=HTMLResponse)
async def action_win(
    trade_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_trade_outcome(trade_uid, token, "WIN")
    return _html(result["title"], result["html"])


@router.get("/trades/{trade_uid}/loss", response_class=HTMLResponse)
async def action_loss(
    trade_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_trade_outcome(trade_uid, token, "LOSS")
    return _html(result["title"], result["html"])


@router.get("/trades/{trade_uid}/breakeven", response_class=HTMLResponse)
async def action_breakeven(
    trade_uid: str,
    token: str = Query(...),
) -> HTMLResponse:
    result = await handlers.handle_trade_outcome(trade_uid, token, "BREAKEVEN")
    return _html(result["title"], result["html"])
