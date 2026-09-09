# Telegram Advanced Forwarder — Fixed Entity Resolution

This build fixes the key diagnostic problem where a Telegram username can resolve to a **User** while the intended source is a **Channel**.

## Important

Use this only for Telegram content and destinations that you are authorized to access/process. It does not bypass protected content or Telegram permissions.

## What changed

- Dialog-first entity resolution.
- `/testid` now reports **Type**, **ID**, and **Username**.
- A `User` can never silently become a source/destination.
- Startup logs show every resolved source/destination.
- `/debug` shows resolver state.
- `/reload` refreshes all entities.
- `/addsource` and `/adddest` resolve immediately.
- New-message handler logs every incoming Telegram event.
- Source matching uses the actual Telethon peer ID.
- `/test @source` runs the delivery pipeline against the latest media message.
- Handles `message is not modified` in the control UI.
- SQLite duplicate protection.
- Multiple sources/destinations.
- Forward mode and media/reupload mode.
- Caption and filename prefix/suffix.
- Caption text and regex replacement rules.
- Mention removal.
- Media filters can be extended safely.

## Deploy on Ubuntu VPS

```bash
mkdir -p /home/ubuntu/tgforward-fixed
cd /home/ubuntu/tgforward-fixed
```

Copy the ZIP contents here, then:

```bash
cp .env.example .env
nano .env
```

Fill:

```text
API_ID=...
API_HASH=...
SESSION_STRING=...
BOT_TOKEN=...
OWNER_ID=...
```

Then:

```bash
docker compose build --no-cache
docker compose up -d
docker logs -f tg-advanced-forwarder-fixed
```

## First diagnostic sequence

After the container starts:

```text
/start
/reload
/debug
/testid @YourChannel
/sources
/dests
```

A valid channel should look like:

```text
Name: AE Storage
Type: channel
ID: -100xxxxxxxxxx
Username: @AEStorage
```

If `/testid @AEStorage` says:

```text
Type: user
```

the logged-in Telethon account is not resolving that reference to the intended channel. Open/join the intended channel with that Telegram account, then run:

```text
/reload
```

For a private channel, the account must actually have access to it.

## Test actual delivery

```text
/test @AEStorage
```

Then inspect:

```bash
docker logs --tail 200 tg-advanced-forwarder-fixed
```

You should see:

```text
SOURCE READY
EVENT
SOURCE MATCH
FORWARDED
```

or, in media mode:

```text
REUPLOADED
```

## Modes

### Native forward

```text
/mode forward
```

Preserves Telegram forwarding semantics. Caption/filename transformations do not apply.

### Media/reupload

```text
/mode media
```

Downloads and reuploads media, allowing caption and filename transformations.

## Caption

```text
/caption_prefix [text]
/caption_suffix [text]
/remove_mentions on
```

Rules:

```text
/addreplace old => new
/addregex pattern => replacement
/rules
/clearrules
```

## Filename

```text
/filename_prefix [text]
/filename_suffix [text]
```

## Delay

```text
/delay 11
```

Flood waits are respected rather than bypassed.

## Security

Do not paste your Bot Token, API Hash, or Telethon StringSession into chat or public repositories. If a live StringSession has already been exposed, terminate that Telegram session and generate a new one.
