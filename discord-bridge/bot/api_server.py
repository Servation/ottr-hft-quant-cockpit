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
        return web.json_response({"metrics": summary, "num_points": len(points)})
    except Exception as e:
        # Don't leak internals; the snapshot degrades gracefully without metrics.
        logger.error(f"Error computing performance: {e}")
        return web.json_response(
            {"status": "error", "reason": "Internal server error"}, status=500
        )


async def start_api_server(bot, port=8001):
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/api/directive", handle_directive)
    app.router.add_get("/api/performance", handle_performance)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Discord Bot API Server running on port {port}")
    return runner
