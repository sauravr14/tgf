import asyncio
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

from database import ProcessedStore

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

SOURCE_CHANNEL = os.environ["SOURCE_CHANNEL"].strip()
DESTINATION_CHANNEL = os.environ["DESTINATION_CHANNEL"].strip()

BACKFILL_ON_START = os.environ.get("BACKFILL_ON_START", "false").lower() == "true"
INCLUDE_PHOTOS = os.environ.get("INCLUDE_PHOTOS", "true").lower() == "true"
INCLUDE_VIDEOS = os.environ.get("INCLUDE_VIDEOS", "true").lower() == "true"
INCLUDE_DOCUMENTS = os.environ.get("INCLUDE_DOCUMENTS", "true").lower() == "true"
INCLUDE_AUDIO = os.environ.get("INCLUDE_AUDIO", "true").lower() == "true"
INCLUDE_VOICE = os.environ.get("INCLUDE_VOICE", "true").lower() == "true"
INCLUDE_STICKERS = os.environ.get("INCLUDE_STICKERS", "false").lower() == "true"

DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2"))
STATUS_EVERY = int(os.environ.get("STATUS_EVERY", "100"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tg-file-forwarder")

store = ProcessedStore(os.environ.get("DB_PATH", "/data/processed.sqlite3"))

def is_supported_media(message):
    media = message.media
    if not media:
        return False

    # Telethon message helpers
    if message.photo:
        return INCLUDE_PHOTOS
    if message.video:
        return INCLUDE_VIDEOS
    if message.audio:
        return INCLUDE_AUDIO
    if message.voice:
        return INCLUDE_VOICE
    if message.document:
        # Documents also include many file types; stickers are documents too.
        if message.sticker:
            return INCLUDE_STICKERS
        return INCLUDE_DOCUMENTS

    return False

async def copy_message(client, message, destination):
    if not is_supported_media(message):
        return False

    # Sending message.media causes Telegram/Telethon to obtain the media and
    # upload it to the destination. We intentionally do not attempt to bypass
    # Telegram content protection.
    caption = message.text or None

    await client.send_file(
        destination,
        message.media,
        caption=caption,
        supports_streaming=bool(message.video),
    )
    return True

async def process_message(client, message, destination):
    if await store.exists(message.id):
        return False

    try:
        copied = await copy_message(client, message, destination)
        if copied:
            await store.add(message.id)
            log.info("Copied source message %s", message.id)
            if DELAY_SECONDS > 0:
                await asyncio.sleep(DELAY_SECONDS)
            return True

    except FloodWaitError as e:
        log.warning("FloodWait: sleeping %s seconds", e.seconds)
        await asyncio.sleep(e.seconds + 2)
        return await process_message(client, message, destination)

    except RPCError as e:
        # Protected/no-forward content and other Telegram restrictions are
        # deliberately not bypassed.
        log.error("Telegram error for message %s: %s", message.id, e)

    except Exception:
        log.exception("Failed to copy message %s", message.id)

    return False

async def backfill(client, source, destination):
    log.info("Backfill enabled. Scanning source history...")
    count = 0
    async for message in client.iter_messages(source, reverse=True):
        if await process_message(client, message, destination):
            count += 1
            if count % STATUS_EVERY == 0:
                log.info("Backfill copied %s files", count)
    log.info("Backfill complete. Copied %s files.", count)

async def main():
    if not SESSION_STRING:
        raise RuntimeError(
            "SESSION_STRING is empty. Generate a Telethon StringSession with "
            "`docker compose run --rm forwarder python generate_session.py`."
        )

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await client.start()
    me = await client.get_me()
    source = await client.get_entity(SOURCE_CHANNEL)
    destination = await client.get_entity(DESTINATION_CHANNEL)

    log.info("Logged in as: %s (id=%s)", getattr(me, "username", None), me.id)
    log.info("Source: %s", getattr(source, "title", SOURCE_CHANNEL))
    log.info("Destination: %s", getattr(destination, "title", DESTINATION_CHANNEL))

    if BACKFILL_ON_START:
        await backfill(client, source, destination)

    @client.on(events.NewMessage(chats=source))
    async def handler(event):
        await process_message(client, event.message, destination)

    log.info("Forwarder is running. Waiting for new files...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
