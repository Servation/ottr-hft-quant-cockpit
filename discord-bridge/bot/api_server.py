import logging
from aiohttp import web

logger = logging.getLogger("api_server")

async def handle_directive(request):
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
        logger.error(f"Error handling directive: {e}")
        return web.json_response({"status": "error", "reason": str(e)}, status=500)

async def start_api_server(bot, port=8001):
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/api/directive", handle_directive)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Discord Bot API Server running on port {port}")
    return runner
