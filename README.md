# Telegram File Forwarder

A Dockerized Telethon userbot that copies media/files from a source Telegram channel to a destination channel.

## Important

Your Telegram account must be able to access the source channel and must have permission to post in the destination channel.

This project does **not** bypass Telegram content protection. If a source channel has protected/no-forwards content, Telegram may reject copying/downloading that content.

## 1. Create the project

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
nano .env
```

Fill in:

- `API_ID`
- `API_HASH`
- `SOURCE_CHANNEL`
- `DESTINATION_CHANNEL`

Do not put your normal Telegram password in `.env`.

## 2. Generate a user session

Build the image:

```bash
docker compose build
```

Then run the interactive session generator:

```bash
docker compose run --rm forwarder python generate_session.py
```

Enter your Telegram phone number, login code, and 2FA password if Telegram asks for it.

It prints:

```text
===== SESSION_STRING =====
...
===== END SESSION_STRING =====
```

Copy the whole session string into `.env`:

```env
SESSION_STRING=PASTE_THE_VALUE_HERE
```

The StringSession is effectively a login credential. Keep it secret.

## 3. Start the forwarder

```bash
docker compose up -d
```

Check:

```bash
docker ps
docker logs -f tg-file-forwarder
```

You should see:

```text
Forwarder is running. Waiting for new files...
```

## Historical files

If you want the bot to scan the source channel's existing history, set:

```env
BACKFILL_ON_START=true
```

Then:

```bash
docker compose down
docker compose up -d
```

This can copy a large number of files. For a large channel, expect it to take time and potentially hit Telegram rate limits.

After the initial backfill, set it back to:

```env
BACKFILL_ON_START=false
```

The SQLite database in `./data/processed.sqlite3` prevents already-copied source message IDs from being copied again.

## Supported media

By default:

- Photos
- Videos
- Documents/files
- Audio
- Voice messages

Stickers are disabled by default and can be enabled with:

```env
INCLUDE_STICKERS=true
```

Text-only messages are ignored.

## If the source channel is private

The Telegram account used to generate the StringSession must already be a member of the source channel and able to read its posts.

## If destination is private

You can use the numeric Telegram peer ID if your account can resolve it, for example:

```env
DESTINATION_CHANNEL=-1001234567890
```

For public channels, `@channelusername` is usually easiest.

## Updating the bot

After changing source/destination or code:

```bash
docker compose up -d --build
```

## Security

Never commit `.env` or the StringSession to GitHub.

If a StringSession is exposed, treat it as compromised and revoke the Telegram session from Telegram's active sessions.
