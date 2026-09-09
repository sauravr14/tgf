# TG Advanced Forwarder

Telegram userbot + BotFather control bot.

## Features
- Multiple source and destination channels.
- Public usernames, t.me links, and numeric -100 IDs.
- Numeric ID resolution primes Telethon dialogs first.
- Media or Telegram-forward mode.
- Caption prefix/suffix.
- Plain text replacements.
- Regex replacements.
- Remove @mentions.
- Filename prefix/suffix and replacements; changed filenames are downloaded/re-uploaded.
- Media type filters.
- Delay/rate-limit handling.
- SQLite duplicate protection.
- Optional historical backfill.
- Owner-only BotFather control panel.

## Setup

```bash
cp .env.example .env
nano .env
docker compose build
docker compose run --rm forwarder python generate_session.py
```

Copy the printed StringSession into `.env`, then:

```bash
docker compose up -d
docker logs -f tg-advanced-forwarder
```

## Bot commands

```text
/start
/status

/addsource @channel
/addsource -1001234567890
/delsource reference
/sources

/adddest @channel
/adddest -1001234567890
/deldest reference
/dests

/testid -1001234567890
/reload

/on
/off
/mode media
/mode forward
/delay 2

/caption on
/caption_prefix text
/caption_suffix text
/remove_mentions on

/addreplace @something | @admin
/addregex @\w+ | @admin
/rules
/delrule 1
/clearrules

/filename on
/filename_prefix NEW_
/filename_suffix _FINAL

/toggle photo
/toggle video
/toggle document
/toggle audio
/toggle voice
/toggle sticker
/toggle animation
/toggle text

/backfill on
/backfill_limit 100
/backfill_now
```

## Text editing example

Source caption:

`Movie Name | @something | 1080p`

Command:

```text
/addreplace @something | @admin
```

Result:

`Movie Name | @admin | 1080p`

Regex example:

```text
/addregex @\w+ | @admin
```

This applies to captions and filenames.

## Numeric -100 IDs

The bot supports them. The logged-in Telegram user must be able to access the channel. For private channels, membership is required.

If `/testid -100...` fails, open the channel with the same Telegram account, then run:

```text
/reload
/testid -100...
```

Using `@username` is still the simplest option for public channels.

## Modes

`media` downloads/re-uploads media and permits caption/filename changes.

`forward` uses Telegram forwarding and therefore does not apply the editing rules.

## Security

Keep `.env` and StringSession private. The control bot only accepts commands from `OWNER_ID`.

If a StringSession is exposed, terminate that Telegram session and generate a new one.

This project does not bypass Telegram protected-content restrictions.
