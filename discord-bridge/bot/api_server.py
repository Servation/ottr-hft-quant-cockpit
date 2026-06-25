import hmac
import logging
import os

from aiohttp import web

from bot.security import RateLimiter

logger = logging.getLogger("api_server")

# Per-IP rate limit for the directive endpoint (DoS / spam protection).
_directive_rate = RateLimiter(int(os.getenv("API_RATE_LIMIT", "30")), 60.0)


def _authorized(request) -> tuple:
    """
    Validate the shared API key on a state-changing request.

    Returns (ok, status, reason). Fail-closed: if the server has no OTTR_API_KEY
    configured, no caller can be authorized (503), because an unauthenticated
    directive endpoint is exactly the spoofing/EoP hole from threat_model.md.
    """
    expected = os.getenv("OTTR_API_KEY", "")
    if not expected:
        logger.error(
            "OTTR_API_KEY is not configured; refusing /api/directive (fail-closed). "
            "Set OTTR_API_KEY on the discord-bridge and agent-gateway services."
        )
        return (False, 503, "Server auth not configured")

    provided = request.headers.get("X-API-Key", "")
    # Constant-time comparison to avoid leaking the key via timing.
    if not hmac.compare_digest(provided, expected):
        logger.warning("Unauthorized /api/directive call (missing or invalid X-API-Key).")
        return (False, 401, "Unauthorized")

    return (True, 200, "")


async def handle_directive(request):
    # --- Rate limit (per client IP) -----------------------------------------
    client_ip = getattr(request, "remote", None) or "unknown"
    if not _directive_rate.allow(client_ip):
        logger.warning("Rate limit exceeded for /api/directive from %s", client_ip)
        return web.json_response({"status": "error", "reason": "Rate limit exceeded"}, status=429)

    # --- Authentication gate -------------------------------------------------
    ok, status, reason = _authorized(request)
    if not ok:
        return web.json_response({"status": "error", "reason": reason}, status=status)

    try:
        data = await request.json()
        directive = data.get("message", "")
        if not directive:
            return web.json_response({"status": "error", "reason": "No message provided"}, status=400)

        bot = request.app["bot"]
        channel = bot._trading_floor_channel

        if not channel:
            logger.error("Channel not found for directive.")
            return web.json_response({"status": "error", "reason": "Channel not found"}, status=500)

        await channel.send(f"**[CEO DIRECTIVE from Dashboard]**: {directive}")

        return web.json_response({"status": "ok"})
    except Exception as e:
        # Do not leak internals to the caller (see threat_model.md). Log privately.
        logger.error(f"Error handling directive: {e}")
        return web.json_response({"status": "error", "reason": "Internal server error"}, status=500)


async def handle_performance(request):
    """Read-only: performance metrics computed over the equity curve.

    Portfolio-derived data only (no secrets), so it follows the same posture as
    the other read endpoints (unauthenticated; the bridge isn't publicly exposed
    and CORS/localhost binding mitigate). The gateway proxies this into
    /portfolio/snapshot so the dashboard can show return-vs-HODL / Sharpe /
    drawdown. The metric *logic* lives here (single source of truth), not in the
    gateway.
    """
    try:
        from bot.equity import load_curve
        from bot import metrics

        rows = load_curve()
        points = [
            (r["ts"], r["total_value"])
            for r in rows
            if isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("total_value"), (int, float))
        ]
        btc = [
            r.get("btc_price")
            for r in rows
            if isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("total_value"), (int, float))
        ]
        summary = metrics.summarize(
            points, btc_prices=btc if any(b for b in btc) else None
        )

        # Tier 3: surface the live risk-control state (read-only, no secrets) so the
        # dashboard can show whether enforcement is on, whether the drawdown breaker is
        # halted, and the current drawdown vs the configured limit.
        from bot import risk_state, settings
        rl = settings.get("risk_limits", {})
        rstate = risk_state.load()
        cur_dd = None
        values = [p[1] for p in points]
        if len(values) >= 2:
            peak = max(values)
            cur_dd = (peak - values[-1]) / peak if peak > 0 else None
        risk_block = {
            "enabled": bool(rl.get("enabled", False)),
            "halted": bool(rstate.get("halted", False)),
            "halted_since": rstate.get("halted_since"),
            "stop_loss_pct": rl.get("stop_loss_pct"),
            "max_drawdown_halt_pct": rl.get("max_drawdown_halt_pct"),
            "current_drawdown": cur_dd,
        }
        return web.json_response(
            {"metrics": summary, "num_points": len(points), "risk": risk_block}
        )
    except Exception as e:
        # Don't leak internals; the snapshot degrades gracefully without metrics.
        logger.error(f"Error computing performance: {e}")
        return web.json_response(
            {"status": "error", "reason": "Internal server error"}, status=500
        )


async def handle_health(request):
    """Read-only component health for the operator dashboard (Tier 4 / O2). No secrets;
    each sub-check degrades to its own status and the endpoint never 500s on one failure."""
    import time as _t
    components = {}

    # LLM backend (LM Studio): reachable + ping latency.
    try:
        from bot.agents import agent_llm
        t0 = _t.monotonic()
        ok = await agent_llm.check_health()
        components["llm"] = {"status": "OK" if ok else "DOWN",
                             "latency_ms": round((_t.monotonic() - t0) * 1000)}
    except Exception:
        components["llm"] = {"status": "DOWN"}

    # Price feed: age of the last successful quote vs the cache TTL.
    try:
        from bot.price_feed import price_feed
        ts = getattr(price_feed, "_cache_timestamp", 0.0) or 0.0
        ttl = getattr(price_feed, "_cache_ttl", 60)
        if ts <= 0:
            components["price_feed"] = {"status": "UNKNOWN", "last_quote_age_sec": None}
        else:
            age = _t.time() - ts
            components["price_feed"] = {"status": "OK" if age < ttl * 5 else "STALE",
                                        "last_quote_age_sec": round(age)}
    except Exception:
        components["price_feed"] = {"status": "DOWN"}

    # Scheduler: running + the next meeting.
    try:
        from bot.scheduler import meeting_scheduler
        sched = getattr(meeting_scheduler, "_scheduler", None)
        running = bool(sched and getattr(sched, "running", False))
        next_type, next_time = meeting_scheduler.get_next_meeting_info()
        components["scheduler"] = {"status": "OK" if running else "DOWN",
                                   "next_meeting": next_time, "next_type": next_type}
    except Exception:
        components["scheduler"] = {"status": "DOWN"}

    # Portfolio: the in-memory state is readable.
    try:
        from bot.portfolio import portfolio
        holdings = portfolio._state.get("holdings", {})
        components["portfolio"] = {
            "status": "OK",
            "positions": sum(1 for h in holdings.values() if h.get("quantity", 0) > 0),
        }
    except Exception:
        components["portfolio"] = {"status": "DOWN"}

    statuses = [c.get("status") for c in components.values()]
    bad = [s for s in statuses if s in ("DOWN", "STALE")]
    if not bad:
        overall = "OK"
    elif all(s == "DOWN" for s in statuses):
        overall = "DOWN"
    else:
        overall = "DEGRADED"
    return web.json_response({"status": overall, "components": components})


async def start_api_server(bot, port=8001):
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/api/directive", handle_directive)
    app.router.add_get("/api/performance", handle_performance)
    app.router.add_get("/api/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Discord Bot API Server running on port {port}")
    return runner
