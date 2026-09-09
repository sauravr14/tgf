import asyncio,os
from telethon import TelegramClient
from telethon.sessions import StringSession
async def main():
    c=TelegramClient(StringSession(),int(os.environ["API_ID"]),os.environ["API_HASH"])
    await c.start();print("\n===== SESSION_STRING =====\n"+c.session.save()+"\n===== END SESSION_STRING =====\n");await c.disconnect()
asyncio.run(main())
