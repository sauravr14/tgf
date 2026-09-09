import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel, DocumentAttributeFilename
from telethon.errors import FloodWaitError, RPCError

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

import database as db

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("tg-forwarder")

userbot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

SOURCE_ENTITIES = {}       # normalized entity id -> entity
DEST_ENTITIES = {}         # normalized entity id -> entity
SOURCE_REFS = {}           # ref -> entity
DEST_REFS = {}             # ref -> entity
RELOAD_LOCK = asyncio.Lock()
SEND_LOCK = asyncio.Lock()

def owner_only(message):
    return message.from_user and message.from_user.id == OWNER_ID

def clean_ref(ref):
    ref = ref.strip()
    if ref.startswith("https://t.me/") or ref.startswith("http://t.me/"):
        ref = ref.split("t.me/", 1)[1].strip("/")
        if "/" in ref:
            ref = ref.split("/", 1)[0]
    if ref.startswith("t.me/"):
        ref = ref[5:].strip("/")
    return ref

def entity_type(e):
    if isinstance(e, Channel):
        return "channel" if getattr(e, "broadcast", False) else "supergroup"
    if isinstance(e, Chat):
        return "group"
    if isinstance(e, User):
        return "user"
    return type(e).__name__.lower()

def entity_id(e):
    # For channels/supergroups this yields the -100... peer id.
    return int(e.id) if isinstance(e, (Channel, Chat, User)) else None

def display_name(e):
    if isinstance(e, Channel):
        return e.title or str(e.id)
    if isinstance(e, Chat):
        return e.title or str(e.id)
    if isinstance(e, User):
        return " ".join(x for x in [e.first_name, e.last_name] if x) or e.username or str(e.id)
    return str(e)

def username_of(e):
    return getattr(e, "username", None)

async def resolve_from_dialogs(ref):
    """Resolve a reference using the logged-in account's actual dialogs first.

    This is deliberately strict: a username that resolves to a User is not
    accepted as a source/destination channel.
    """
    wanted = clean_ref(ref)
    wanted_lower = wanted.lstrip("@").lower()

    # Numeric refs: compare against Telethon peer ids and internal ids.
    numeric = None
    try:
        numeric = int(wanted)
    except ValueError:
        pass

    # First inspect dialogs. This is the reliable path for private channels
    # because it uses entities already known to the logged-in account.
    async for dialog in userbot.iter_dialogs():
        e = dialog.entity
        eid = entity_id(e)
        uname = (username_of(e) or "").lower()
        title = (display_name(e) or "").lower()

        if numeric is not None:
            if eid == numeric or eid == abs(numeric):
                return e
            # User may have pasted the positive channel id from Telethon.
            if isinstance(e, Channel) and (eid == -100 * 10**(len(str(e.id))) + e.id):
                pass

        if wanted_lower in {uname, title, str(eid).lower()}:
            return e
        if uname and uname == wanted_lower.lstrip("@"):
            return e

    # Only after dialog scan do we try Telegram username resolution.
    # Then validate that the result is a Channel/Chat rather than a User.
    candidate = wanted
    if candidate and not candidate.startswith("@") and not candidate.lstrip("-").isdigit():
        candidate = "@" + candidate

    try:
        e = await userbot.get_entity(candidate)
    except Exception as ex:
        raise ValueError(f'Could not resolve "{ref}" through this Telegram account: {ex}') from ex

    if isinstance(e, User):
        raise ValueError(
            f'"{ref}" resolved to USER "{display_name(e)}" (ID {e.id}), not a channel/group. '
            f'Open/join the intended channel with the logged-in account, then run /reload.'
        )
    return e

async def resolve_all():
    async with RELOAD_LOCK:
        SOURCE_ENTITIES.clear()
        DEST_ENTITIES.clear()
        SOURCE_REFS.clear()
        DEST_REFS.clear()

        sources = await db.list_entities("sources")
        dests = await db.list_entities("destinations")

        for ref, old_title, old_id, old_type, old_username in sources:
            try:
                e = await resolve_from_dialogs(ref)
                if isinstance(e, User):
                    raise ValueError("resolved to User")
                eid = entity_id(e)
                SOURCE_ENTITIES[eid] = e
                SOURCE_REFS[ref] = e
                await db.upsert_entity("sources", ref, display_name(e), eid, entity_type(e), username_of(e))
                log.info("SOURCE READY | ref=%s | type=%s | title=%s | id=%s | username=@%s",
                         ref, entity_type(e), display_name(e), eid, username_of(e) or "-")
            except Exception as ex:
                log.error("SOURCE FAILED | ref=%s | %s", ref, ex)

        for ref, old_title, old_id, old_type, old_username in dests:
            try:
                e = await resolve_from_dialogs(ref)
                if isinstance(e, User):
                    raise ValueError("resolved to User")
                eid = entity_id(e)
                DEST_ENTITIES[eid] = e
                DEST_REFS[ref] = e
                await db.upsert_entity("destinations", ref, display_name(e), eid, entity_type(e), username_of(e))
                log.info("DEST READY | ref=%s | type=%s | title=%s | id=%s | username=@%s",
                         ref, entity_type(e), display_name(e), eid, username_of(e) or "-")
            except Exception as ex:
                log.error("DEST FAILED | ref=%s | %s", ref, ex)

        log.info("RESOLUTION SUMMARY | sources=%d destinations=%d",
                 len(SOURCE_ENTITIES), len(DEST_ENTITIES))

async def transform_caption(text):
    text = text or ""
    replacements = await db.get_replacements()
    for _, pattern, replacement, is_regex, target in replacements:
        if target != "caption":
            continue
        try:
            if is_regex:
                text = re.sub(pattern, replacement, text)
            else:
                text = text.replace(pattern, replacement)
        except re.error as ex:
            log.error("Invalid regex rule #%s: %s", _, ex)

    if await db.get_setting("remove_mentions", "0") == "1":
        text = re.sub(r"(?<!\w)@[A-Za-z0-9_]{4,}", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
    prefix = await db.get_setting("caption_prefix", "")
    suffix = await db.get_setting("caption_suffix", "")
    return f"{prefix}{text}{suffix}"

async def transform_filename(name):
    if not name:
        return name
    replacements = await db.get_replacements()
    for _, pattern, replacement, is_regex, target in replacements:
        if target != "filename":
            continue
        try:
            name = re.sub(pattern, replacement, name) if is_regex else name.replace(pattern, replacement)
        except re.error as ex:
            log.error("Invalid filename regex rule #%s: %s", _, ex)
    prefix = await db.get_setting("filename_prefix", "")
    suffix = await db.get_setting("filename_suffix", "")
    p = Path(name)
    return f"{prefix}{p.stem}{suffix}{p.suffix}"

async def get_original_filename(message):
    if not message.document:
        return None
    for attr in message.document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None

async def allowed_message(message):
    if not message.media:
        return False
    mode = await db.get_setting("media_filter", "all")
    if mode == "documents" and not message.document:
        return False
    if mode == "videos" and not message.video:
        return False
    if mode == "photos" and not message.photo:
        return False
    return True

async def deliver(message, mark=True):
    if not SOURCE_ENTITIES:
        log.warning("DELIVERY SKIPPED | no resolved sources")
        return
    if not DEST_ENTITIES:
        log.warning("DELIVERY SKIPPED | no resolved destinations")
        return

    source_id = int(message.chat_id)
    if mark and await db.already_processed(source_id, message.id):
        log.info("DUPLICATE SKIP | source=%s message=%s", source_id, message.id)
        return

    if not await allowed_message(message):
        log.info("FILTER SKIP | source=%s message=%s", source_id, message.id)
        if mark:
            await db.mark_processed(source_id, message.id)
        return

    mode = await db.get_setting("mode", "forward")
    delay = float(await db.get_setting("delay", "11"))

    if mode == "forward":
        # Native forwarding preserves Telegram's forward semantics but cannot
        # change captions/filenames.
        for dest in list(DEST_ENTITIES.values()):
            async with SEND_LOCK:
                try:
                    await userbot.forward_messages(dest, message)
                    log.info("FORWARDED | %s/%s -> %s/%s",
                             source_id, message.id, entity_id(dest), display_name(dest))
                except FloodWaitError as ex:
                    log.warning("FLOOD WAIT | %ss", ex.seconds)
                    await asyncio.sleep(ex.seconds)
                    await userbot.forward_messages(dest, message)
                except RPCError:
                    log.exception("FORWARD FAILED | source=%s message=%s dest=%s",
                                  source_id, message.id, entity_id(dest))
            if delay > 0:
                await asyncio.sleep(delay)
    else:
        # Media/reupload mode permits caption and filename transformations.
        caption = await transform_caption(message.raw_text or "")
        filename = await transform_filename(await get_original_filename(message))
        tmpdir = Path("tmp")
        tmpdir.mkdir(exist_ok=True)
        path = None
        try:
            path = await userbot.download_media(message, file=str(tmpdir))
            if not path:
                log.warning("DOWNLOAD FAILED | source=%s message=%s", source_id, message.id)
                return
            send_file = path
            if filename:
                new_path = Path(path).with_name(filename)
                Path(path).rename(new_path)
                send_file = str(new_path)
            for dest in list(DEST_ENTITIES.values()):
                async with SEND_LOCK:
                    try:
                        await userbot.send_file(dest, send_file, caption=caption or None)
                        log.info("REUPLOADED | %s/%s -> %s/%s",
                                 source_id, message.id, entity_id(dest), display_name(dest))
                    except FloodWaitError as ex:
                        log.warning("FLOOD WAIT | %ss", ex.seconds)
                        await asyncio.sleep(ex.seconds)
                        await userbot.send_file(dest, send_file, caption=caption or None)
                    except RPCError:
                        log.exception("REUPLOAD FAILED | source=%s message=%s dest=%s",
                                      source_id, message.id, entity_id(dest))
                if delay > 0:
                    await asyncio.sleep(delay)
        finally:
            if path:
                try:
                    p = Path(path)
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

    if mark:
        await db.mark_processed(source_id, message.id)

@userbot.on(events.NewMessage())
async def on_new_message(event):
    try:
        cid = int(event.chat_id)
        log.info("EVENT | chat_id=%s message_id=%s", cid, event.id)

        if cid not in SOURCE_ENTITIES:
            return

        src = SOURCE_ENTITIES[cid]
        log.info("SOURCE MATCH | title=%s id=%s message=%s media=%s",
                 display_name(src), cid, event.id, bool(event.message.media))

        if not event.message.media:
            log.info("TEXT-ONLY SKIP | source=%s message=%s", cid, event.id)
            return

        asyncio.create_task(deliver(event.message))
    except Exception:
        log.exception("EVENT HANDLER ERROR")

def panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Reload / Resolve", callback_data="reload")],
        [InlineKeyboardButton(text="📥 Sources", callback_data="sources"),
         InlineKeyboardButton(text="📤 Destinations", callback_data="dests")],
        [InlineKeyboardButton(text="⚙️ Status", callback_data="status"),
         InlineKeyboardButton(text="🧪 Debug", callback_data="debug")]
    ])

async def safe_edit(callback, text, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as ex:
        if "message is not modified" not in str(ex).lower():
            raise
    finally:
        await callback.answer()

def fmt_entity_rows(rows, resolved):
    if not rows:
        return "None"
    out = []
    for ref, title, eid, etype, username in rows:
        e = resolved.get(eid)
        if e:
            out.append(f"• <b>{display_name(e)}</b> — <code>{eid}</code> — {entity_type(e)}")
        else:
            out.append(f"• <code>{ref}</code> — ❌ unresolved")
    return "\n".join(out)

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not owner_only(message):
        return
    await message.answer(
        "🚀 <b>Telegram Forwarder</b>\n\n"
        "Use the buttons or /help.\n"
        "This build strictly validates that sources/destinations are channels/groups.",
        parse_mode="HTML", reply_markup=panel()
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not owner_only(message):
        return
    await message.answer(
        "<b>Commands</b>\n"
        "/status /debug /reload\n"
        "/sources /dests\n"
        "/addsource REF\n/delsource REF\n"
        "/adddest REF\n/deldest REF\n"
        "/testid REF\n"
        "/mode forward|media\n"
        "/delay SECONDS\n"
        "/caption_prefix TEXT\n/caption_suffix TEXT\n"
        "/filename_prefix TEXT\n/filename_suffix TEXT\n"
        "/remove_mentions on|off\n"
        "/addreplace OLD =&gt; NEW\n"
        "/addregex REGEX =&gt; REPLACEMENT\n"
        "/rules /clearrules\n"
        "/backfill LIMIT\n"
        "/test REF\n",
        parse_mode="HTML"
    )

@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not owner_only(message): return
    mode = await db.get_setting("mode", "forward")
    delay = await db.get_setting("delay", "11")
    backfill = await db.get_setting("backfill", "on")
    await message.answer(
        f"Bot: ON\nMode: {mode}\nSources: {len(SOURCE_ENTITIES)} resolved\n"
        f"Destinations: {len(DEST_ENTITIES)} resolved\nDelay: {delay}s\nBackfill: {backfill}"
    )

@dp.message(Command("debug"))
async def debug_cmd(message: Message):
    if not owner_only(message): return
    srows = await db.list_entities("sources")
    drows = await db.list_entities("destinations")
    text = ["🔎 <b>DEBUG</b>", f"Userbot connected: {userbot.is_connected()}",
            f"Resolved sources: {len(SOURCE_ENTITIES)}",
            f"Resolved destinations: {len(DEST_ENTITIES)}", "", "<b>Sources</b>"]
    for ref, title, eid, etype, username in srows:
        e = SOURCE_REFS.get(ref)
        text.append(f"• {ref} → {entity_type(e) if e else 'UNRESOLVED'} | "
                    f"{display_name(e) if e else title or '-'} | ID {entity_id(e) if e else eid}")
    text += ["", "<b>Destinations</b>"]
    for ref, title, eid, etype, username in drows:
        e = DEST_REFS.get(ref)
        text.append(f"• {ref} → {entity_type(e) if e else 'UNRESOLVED'} | "
                    f"{display_name(e) if e else title or '-'} | ID {entity_id(e) if e else eid}")
    await message.answer("\n".join(text), parse_mode="HTML")

@dp.message(Command("reload"))
async def reload_cmd(message: Message):
    if not owner_only(message): return
    await resolve_all()
    await message.answer(
        f"🔄 Reload complete.\nSources resolved: {len(SOURCE_ENTITIES)}\n"
        f"Destinations resolved: {len(DEST_ENTITIES)}"
    )

@dp.message(Command("sources"))
async def sources_cmd(message: Message):
    if not owner_only(message): return
    rows = await db.list_entities("sources")
    await message.answer("📥 <b>Sources</b>\n" + fmt_entity_rows(rows, SOURCE_ENTITIES), parse_mode="HTML")

@dp.message(Command("dests"))
async def dests_cmd(message: Message):
    if not owner_only(message): return
    rows = await db.list_entities("destinations")
    await message.answer("📤 <b>Destinations</b>\n" + fmt_entity_rows(rows, DEST_ENTITIES), parse_mode="HTML")

async def add_entity(message, table, arg):
    ref = clean_ref(arg)
    try:
        e = await resolve_from_dialogs(ref)
        if isinstance(e, User):
            raise ValueError("resolved to a User, not a channel/group")
        await db.upsert_entity(table, ref, display_name(e), entity_id(e), entity_type(e), username_of(e))
        await resolve_all()
        await message.answer(
            f"✅ Added {entity_type(e)}\n<b>{display_name(e)}</b>\nID: <code>{entity_id(e)}</code>",
            parse_mode="HTML"
        )
    except Exception as ex:
        await message.answer(f"❌ {ex}")

@dp.message(Command("addsource"))
async def addsource_cmd(message: Message):
    if not owner_only(message): return
    arg = message.text.partition(" ")[2].strip()
    if not arg:
        await message.answer("Usage: /addsource @channel")
        return
    await add_entity(message, "sources", arg)

@dp.message(Command("adddest"))
async def adddest_cmd(message: Message):
    if not owner_only(message): return
    arg = message.text.partition(" ")[2].strip()
    if not arg:
        await message.answer("Usage: /adddest @channel")
        return
    await add_entity(message, "destinations", arg)

@dp.message(Command("delsource"))
async def delsource_cmd(message: Message):
    if not owner_only(message): return
    arg = message.text.partition(" ")[2].strip()
    await db.delete_entity("sources", clean_ref(arg))
    await resolve_all()
    await message.answer("✅ Source removed and resolver refreshed.")

@dp.message(Command("deldest"))
async def deldest_cmd(message: Message):
    if not owner_only(message): return
    arg = message.text.partition(" ")[2].strip()
    await db.delete_entity("destinations", clean_ref(arg))
    await resolve_all()
    await message.answer("✅ Destination removed and resolver refreshed.")

@dp.message(Command("testid"))
async def testid_cmd(message: Message):
    if not owner_only(message): return
    ref = message.text.partition(" ")[2].strip()
    if not ref:
        await message.answer("Usage: /testid @channel")
        return
    try:
        e = await resolve_from_dialogs(ref)
        await message.answer(
            f"✅ <b>Resolved</b>\n"
            f"Name: <b>{display_name(e)}</b>\n"
            f"Type: <b>{entity_type(e)}</b>\n"
            f"ID: <code>{entity_id(e)}</code>\n"
            f"Username: <code>@{username_of(e) or '-'}</code>",
            parse_mode="HTML"
        )
    except Exception as ex:
        await message.answer(f"❌ {ex}")

@dp.message(Command("mode"))
async def mode_cmd(message: Message):
    if not owner_only(message): return
    mode = message.text.partition(" ")[2].strip().lower()
    if mode not in {"forward", "media"}:
        await message.answer("Usage: /mode forward or /mode media")
        return
    await db.set_setting("mode", mode)
    await message.answer(f"✅ Mode: {mode}")

@dp.message(Command("delay"))
async def delay_cmd(message: Message):
    if not owner_only(message): return
    try:
        value = max(0, float(message.text.partition(" ")[2].strip()))
        await db.set_setting("delay", value)
        await message.answer(f"✅ Delay: {value}s")
    except Exception:
        await message.answer("Usage: /delay 11")

@dp.message(Command("caption_prefix"))
async def caption_prefix(message: Message):
    if not owner_only(message): return
    await db.set_setting("caption_prefix", message.text.partition(" ")[2])
    await message.answer("✅ Caption prefix updated.")

@dp.message(Command("caption_suffix"))
async def caption_suffix(message: Message):
    if not owner_only(message): return
    await db.set_setting("caption_suffix", message.text.partition(" ")[2])
    await message.answer("✅ Caption suffix updated.")

@dp.message(Command("filename_prefix"))
async def filename_prefix(message: Message):
    if not owner_only(message): return
    await db.set_setting("filename_prefix", message.text.partition(" ")[2])
    await message.answer("✅ Filename prefix updated.")

@dp.message(Command("filename_suffix"))
async def filename_suffix(message: Message):
    if not owner_only(message): return
    await db.set_setting("filename_suffix", message.text.partition(" ")[2])
    await message.answer("✅ Filename suffix updated.")

@dp.message(Command("remove_mentions"))
async def remove_mentions(message: Message):
    if not owner_only(message): return
    v = message.text.partition(" ")[2].strip().lower()
    if v not in {"on", "off"}:
        await message.answer("Usage: /remove_mentions on|off")
        return
    await db.set_setting("remove_mentions", "1" if v == "on" else "0")
    await message.answer(f"✅ Remove mentions: {v}")

async def add_rule(message, is_regex):
    arg = message.text.partition(" ")[2]
    if "=>" not in arg:
        await message.answer("Usage: /addreplace OLD => NEW")
        return
    left, right = arg.split("=>", 1)
    await db.add_replacement(left.strip(), right.strip(), is_regex, "caption")
    await message.answer("✅ Rule added.")

@dp.message(Command("addreplace"))
async def addreplace(message: Message):
    if not owner_only(message): return
    await add_rule(message, False)

@dp.message(Command("addregex"))
async def addregex(message: Message):
    if not owner_only(message): return
    await add_rule(message, True)

@dp.message(Command("rules"))
async def rules_cmd(message: Message):
    if not owner_only(message): return
    rows = await db.get_replacements()
    if not rows:
        await message.answer("No rules.")
        return
    await message.answer("\n".join(
        f"{rid}. {'REGEX' if rx else 'TEXT'}: {pat} => {rep}" for rid, pat, rep, rx, target in rows
    ))

@dp.message(Command("clearrules"))
async def clearrules(message: Message):
    if not owner_only(message): return
    await db.clear_replacements()
    await message.answer("✅ All replacement rules cleared.")

@dp.message(Command("test"))
async def test_cmd(message: Message):
    if not owner_only(message): return
    ref = message.text.partition(" ")[2].strip()
    if not ref:
        await message.answer("Usage: /test @source")
        return
    try:
        e = await resolve_from_dialogs(ref)
        msgs = await userbot.get_messages(e, limit=1)
        msg = msgs[0] if msgs else None
        if not msg:
            await message.answer("No message found.")
            return
        if not msg.media:
            await message.answer(f"Latest message is text-only (ID {msg.id}).")
            return
        await deliver(msg, mark=False)
        await message.answer(f"🧪 Test delivery attempted for {display_name(e)} message {msg.id}.")
    except Exception as ex:
        log.exception("TEST FAILED")
        await message.answer(f"❌ Test failed: {ex}")

@dp.callback_query(F.data == "reload")
async def cb_reload(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed", show_alert=True); return
    await resolve_all()
    await safe_edit(c, f"🔄 Reloaded.\nSources: {len(SOURCE_ENTITIES)}\nDestinations: {len(DEST_ENTITIES)}",
                    panel())

@dp.callback_query(F.data == "sources")
async def cb_sources(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed", show_alert=True); return
    rows = await db.list_entities("sources")
    await safe_edit(c, "📥 <b>Sources</b>\n" + fmt_entity_rows(rows, SOURCE_ENTITIES), panel())

@dp.callback_query(F.data == "dests")
async def cb_dests(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed", show_alert=True); return
    rows = await db.list_entities("destinations")
    await safe_edit(c, "📤 <b>Destinations</b>\n" + fmt_entity_rows(rows, DEST_ENTITIES), panel())

@dp.callback_query(F.data == "status")
async def cb_status(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed", show_alert=True); return
    mode = await db.get_setting("mode", "forward")
    delay = await db.get_setting("delay", "11")
    await safe_edit(c, f"🤖 <b>ON</b>\nMode: {mode}\nSources: {len(SOURCE_ENTITIES)}\nDestinations: {len(DEST_ENTITIES)}\nDelay: {delay}s", panel())

@dp.callback_query(F.data == "debug")
async def cb_debug(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("Not allowed", show_alert=True); return
    s = "\n".join(f"{k}: {display_name(v)} ({entity_type(v)})" for k,v in SOURCE_ENTITIES.items()) or "none"
    d = "\n".join(f"{k}: {display_name(v)} ({entity_type(v)})" for k,v in DEST_ENTITIES.items()) or "none"
    await safe_edit(c, f"🔎 <b>DEBUG</b>\n\n<b>Sources</b>\n{s}\n\n<b>Destinations</b>\n{d}", panel())

async def main():
    await db.init_db()
    await db.set_setting("mode", await db.get_setting("mode", "forward"))
    await db.set_setting("delay", await db.get_setting("delay", "11"))
    await userbot.connect()

    if not await userbot.is_user_authorized():
        raise RuntimeError("Telethon session is not authorized. Generate a fresh session string.")

    me = await userbot.get_me()
    log.info("USERBOT READY | %s | id=%s", display_name(me), me.id)

    await resolve_all()

    if not SOURCE_ENTITIES:
        log.warning("NO RESOLVED SOURCES. Use /addsource with a channel the logged-in account can access.")
    if not DEST_ENTITIES:
        log.warning("NO RESOLVED DESTINATIONS. Use /adddest with a channel/group the logged-in account can post to.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
