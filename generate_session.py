import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]

    print("\nTelegram user session generator")
    print("Your phone number, login code, and 2FA password are entered interactively.")
    print("The resulting StringSession is printed once. Keep it secret.\n")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    session = client.session.save()

    print("\n===== SESSION_STRING =====")
    print(session)
    print("===== END SESSION_STRING =====\n")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
