import asyncio
import importlib
import os

from aiohttp import web

from pyrogram import idle
from pytgcalls.exceptions import NoActiveGroupCall

import config
from RishuMusic import LOGGER, app, userbot
from RishuMusic.core.call import shree
from RishuMusic.misc import sudo
from RishuMusic.plugins import ALL_MODULES
from RishuMusic.utils.database import get_banned_users, get_gbanned
from config import BANNED_USERS

async def _health(request):
    return web.json_response({"status": "ok", "service": "RishuMusic"})


async def _start_render_health_server():
    """Expose a small HTTP health endpoint for Render Web Services."""
    port = os.getenv("PORT")
    if not port:
        return None

    application = web.Application()
    application.router.add_get("/", _health)
    application.router.add_get("/health", _health)

    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(port))
    await site.start()
    LOGGER(__name__).info("Render health endpoint listening on port %s.", port)
    return runner


async def init():
    health_runner = await _start_render_health_server()
    if (
        not config.STRING1
        and not config.STRING2
        and not config.STRING3
        and not config.STRING4
        and not config.STRING5
    ):
        LOGGER(__name__).error("Assistant client variables not defined, exiting...")
        exit()
    await sudo()
    try:
        users = await get_gbanned()
        for user_id in users:
            BANNED_USERS.add(user_id)
        users = await get_banned_users()
        for user_id in users:
            BANNED_USERS.add(user_id)
    except:
        pass
    await app.start()
    for all_module in ALL_MODULES:
        importlib.import_module("RishuMusic.plugins" + all_module)
    LOGGER("RishuMusic.plugins").info("Successfully Imported Modules...")
    await userbot.start()
    await shree.start()
    try:
        await shree.stream_call("https://te.legra.ph/file/29f784eb49d230ab62e9e.mp4")
    except NoActiveGroupCall:
        # The logger group's voice chat does not need to be open at startup.
        # Music commands can be used after an admin starts a voice chat.
        LOGGER("RishuMusic").warning(
            "No active voice chat in LOGGER_ID; bot will stay online."
        )
    except Exception as exc:
        LOGGER("RishuMusic").warning(
            "Startup voice-chat check skipped (%s); bot will stay online.",
            type(exc).__name__,
        )

    await shree.decorators()
    await idle()
    await app.stop()
    if health_runner:
        await health_runner.cleanup()
    LOGGER("RishuMusic").info("Stopping ShreeX Music Bot...")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init())
