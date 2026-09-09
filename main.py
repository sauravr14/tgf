import asyncio, logging, os, re, sqlite3
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError, RPCError

load_dotenv()
API_ID=int(os.environ["API_ID"]); API_HASH=os.environ["API_HASH"]
SESSION_STRING=os.environ["SESSION_STRING"].strip()
BOT_TOKEN=os.environ["BOT_TOKEN"].strip(); OWNER_ID=int(os.environ["OWNER_ID"])
DB_PATH=os.environ.get("DB_PATH","/data/forwarder.sqlite3")
logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO").upper(),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("forwarder")

db=sqlite3.connect(DB_PATH,check_same_thread=False); db.row_factory=sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT UNIQUE NOT NULL,enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS destinations(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT UNIQUE NOT NULL,enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS processed(source TEXT,message_id INTEGER,PRIMARY KEY(source,message_id));
CREATE TABLE IF NOT EXISTS replacements(id INTEGER PRIMARY KEY AUTOINCREMENT,old_text TEXT,new_text TEXT,mode TEXT DEFAULT 'plain',enabled INTEGER DEFAULT 1);
""")
defaults={"enabled":"1","mode":"media","caption":"1","caption_prefix":"","caption_suffix":"",
"remove_mentions":"0","filename":"1","filename_prefix":"","filename_suffix":"","delay":"2",
"photos":"1","videos":"1","documents":"1","audio":"1","voice":"1","stickers":"0","animations":"1","text":"0",
"backfill":"0","backfill_limit":"100"}
for k,v in defaults.items(): db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(k,v))
db.commit()

def s(k): return db.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()["value"]
def setv(k,v): db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,str(v))); db.commit()
def on(k): return s(k)=="1"
def add(table,v): db.execute(f"INSERT OR IGNORE INTO {table}(value) VALUES(?)",(v,)); db.commit()
def remove(table,v): db.execute(f"DELETE FROM {table} WHERE value=?",(v,)); db.commit()
def vals(table): return [r["value"] for r in db.execute(f"SELECT value FROM {table} WHERE enabled=1 ORDER BY id")]
def processed(src,mid): return db.execute("SELECT 1 FROM processed WHERE source=? AND message_id=?",(src,mid)).fetchone() is not None
def mark(src,mid):
    try: db.execute("INSERT INTO processed VALUES(?,?)",(src,mid)); db.commit(); return True
    except sqlite3.IntegrityError: return False

def rules(): return db.execute("SELECT * FROM replacements WHERE enabled=1 ORDER BY id").fetchall()
def transform(text):
    if text is None: return None
    out=text
    for r in rules():
        try: out=re.sub(r["old_text"],r["new_text"],out) if r["mode"]=="regex" else out.replace(r["old_text"],r["new_text"])
        except re.error: log.exception("Invalid regex rule %s",r["id"])
    if on("remove_mentions"): out=re.sub(r"(?<!\w)@[A-Za-z0-9_]{3,32}\b","",out)
    if s("caption_prefix"): out=s("caption_prefix")+out
    if s("caption_suffix"): out+=s("caption_suffix")
    return out.strip()

def filename(msg):
    if not msg.document:return None
    for a in msg.document.attributes:
        if isinstance(a,DocumentAttributeFilename): return a.file_name
    return None

def new_filename(name):
    if not name or not on("filename"): return name
    out=name
    for r in rules():
        try: out=re.sub(r["old_text"],r["new_text"],out) if r["mode"]=="regex" else out.replace(r["old_text"],r["new_text"])
        except re.error: pass
    return s("filename_prefix")+out+s("filename_suffix")

def allowed(m):
    if m.photo:return on("photos")
    if m.video:return on("videos")
    if m.audio:return on("audio")
    if m.voice:return on("voice")
    if m.sticker:return on("stickers")
    if m.gif:return on("animations")
    if m.document:return on("documents")
    return on("text") and bool(m.text)

def normalize(x):
    x=x.strip()
    if x.startswith("https://t.me/"):
        tail=x.rstrip("/").split("/")[-1]
        if not tail.startswith("+") and tail!="joinchat": return "@"+tail.lstrip("@")
    return x

async def resolve(client,ref):
    ref=normalize(ref)
    if re.fullmatch(r"-100\d+",ref):
        wanted=int(ref)
        async for d in client.iter_dialogs():
            if getattr(d.entity,"id",None)==wanted:return d.entity
        try:return await client.get_entity(wanted)
        except Exception as e:
            raise ValueError(f"Cannot resolve {ref}. The logged-in account must be a member of the channel. Open it in Telegram, then /reload and try /testid again.") from e
    return await client.get_entity(ref)

def name(e): return getattr(e,"title",None) or getattr(e,"username",None) or str(getattr(e,"id",e))

bot=Bot(BOT_TOKEN); dp=Dispatcher(); userbot=None
src_cache={}; dst_cache={}

def menu():
    b=InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📊 Status",callback_data="status"),InlineKeyboardButton(text="⚙️ Settings",callback_data="settings"))
    b.row(InlineKeyboardButton(text="📥 Sources",callback_data="sources"),InlineKeyboardButton(text="📤 Destinations",callback_data="dests"))
    b.row(InlineKeyboardButton(text="✏️ Text Rules",callback_data="rules"),InlineKeyboardButton(text="🎛 Media",callback_data="media"))
    b.row(InlineKeyboardButton(text="❓ Help",callback_data="help")); return b.as_markup()

def settings_menu():
    b=InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 ON/OFF",callback_data="toggle"),InlineKeyboardButton(text="📎 Mode",callback_data="mode"))
    b.row(InlineKeyboardButton(text="📝 Caption",callback_data="caption"),InlineKeyboardButton(text="📁 Filename",callback_data="filename"))
    b.row(InlineKeyboardButton(text="🎛 Media",callback_data="media"),InlineKeyboardButton(text="⏱ Delay",callback_data="delayhelp"))
    b.row(InlineKeyboardButton(text="⬅️ Back",callback_data="home")); return b.as_markup()

def media_menu():
    b=InlineKeyboardBuilder()
    for k,l in [("photos","Photos"),("videos","Videos"),("documents","Documents"),("audio","Audio"),("voice","Voice"),("stickers","Stickers"),("animations","Animations"),("text","Text")]:
        b.button(text=("✅ " if on(k) else "❌ ")+l,callback_data="flip:"+k)
    b.adjust(2); b.row(InlineKeyboardButton(text="⬅️ Settings",callback_data="settings")); return b.as_markup()

def authorized(m): return m.from_user and m.from_user.id==OWNER_ID
async def guard(m):
    if not authorized(m): await m.answer("⛔ Not authorized."); return False
    return True

HELP="""<b>Advanced Forwarder Commands</b>

<b>Channels</b>
/addsource @channel | -100ID | t.me/link
/delsource reference
/sources
/adddest @channel | -100ID | t.me/link
/deldest reference
/dests
/testid -1001234567890
/reload

<b>Forwarding</b>
/on /off
/mode media|forward
/delay 2

<b>Caption editing</b>
/caption on|off
/caption_prefix text
/caption_suffix text
/remove_mentions on|off
/addreplace old | new
/addregex pattern | replacement
/rules
/delrule ID
/clearrules

<b>Filename editing</b>
/filename on|off
/filename_prefix text
/filename_suffix text

<b>Media</b>
/toggle photo|video|document|audio|voice|sticker|animation|text

<b>History</b>
/backfill on|off
/backfill_limit 100
/backfill_now

<b>Other</b>
/status
/start
/help"""

@dp.message(CommandStart())
async def start(m):
    if await guard(m): await m.answer("🚀 <b>Advanced Telegram Forwarder</b>",reply_markup=menu())
@dp.message(Command("help"))
async def helpc(m):
    if await guard(m): await m.answer(HELP,reply_markup=menu())
@dp.message(Command("status"))
async def statusc(m):
    if await guard(m):
        await m.answer(f"Bot: {'ON' if on('enabled') else 'OFF'}\nMode: {s('mode')}\nSources: {len(vals('sources'))}\nDestinations: {len(vals('destinations'))}\nDelay: {s('delay')}s\nBackfill: {'ON' if on('backfill') else 'OFF'}")
@dp.message(Command("on"))
async def onc(m):
    if await guard(m): setv("enabled",1); await m.answer("✅ Forwarding ON")
@dp.message(Command("off"))
async def offc(m):
    if await guard(m): setv("enabled",0); await m.answer("⏸ Forwarding OFF")
@dp.message(Command("mode"))
async def modec(m):
    if not await guard(m):return
    a=(m.text or "").split(maxsplit=1)
    if len(a)==2 and a[1] in ("media","forward"):setv("mode",a[1]);await m.answer("✅ Mode: "+a[1])
    else:await m.answer("Usage: /mode media OR /mode forward")
@dp.message(Command("delay"))
async def delayc(m):
    if not await guard(m):return
    try:setv("delay",max(0,float((m.text or "").split(maxsplit=1)[1])));await m.answer("✅ Delay: "+s("delay")+"s")
    except:await m.answer("Usage: /delay 2")

@dp.message(Command("addsource"))
async def adds(m):
    if await guard(m):
        try:v=(m.text or "").split(maxsplit=1)[1];add("sources",v);await m.answer("✅ Source added: "+v)
        except:await m.answer("Usage: /addsource @channel OR -100ID")
@dp.message(Command("delsource"))
async def dels(m):
    if await guard(m):
        try:v=(m.text or "").split(maxsplit=1)[1];remove("sources",v);src_cache.pop(v,None);await m.answer("✅ Source removed")
        except:await m.answer("Usage: /delsource reference")
@dp.message(Command("sources"))
async def sourcec(m):
    if await guard(m):await m.answer("📥 Sources:\n"+("\n".join(vals("sources")) or "None"))
@dp.message(Command("adddest"))
async def addd(m):
    if await guard(m):
        try:v=(m.text or "").split(maxsplit=1)[1];add("destinations",v);await m.answer("✅ Destination added: "+v)
        except:await m.answer("Usage: /adddest @channel OR -100ID")
@dp.message(Command("deldest"))
async def deld(m):
    if await guard(m):
        try:v=(m.text or "").split(maxsplit=1)[1];remove("destinations",v);dst_cache.pop(v,None);await m.answer("✅ Destination removed")
        except:await m.answer("Usage: /deldest reference")
@dp.message(Command("dests"))
async def destc(m):
    if await guard(m):await m.answer("📤 Destinations:\n"+("\n".join(vals("destinations")) or "None"))
@dp.message(Command("testid"))
async def testid(m):
    if not await guard(m):return
    try:
        e=await resolve(userbot,(m.text or "").split(maxsplit=1)[1]);await m.answer(f"✅ Resolved: <b>{name(e)}</b>\nID: <code>{e.id}</code>")
    except Exception as e:await m.answer("❌ "+str(e))
@dp.message(Command("reload"))
async def reloadc(m):
    if await guard(m):src_cache.clear();dst_cache.clear();await m.answer("🔄 Entity cache cleared.")

async def toggle_cmd(m,key):
    if not await guard(m):return
    try:
        a=(m.text or "").split(maxsplit=1)[1].lower()
        if a not in ("on","off"):raise ValueError
        setv(key,int(a=="on"));await m.answer(f"✅ {key}: {a}")
    except:await m.answer(f"Usage: /{key} on|off")
@dp.message(Command("caption"))
async def captionc(m):await toggle_cmd(m,"caption")
@dp.message(Command("filename"))
async def filenamec(m):await toggle_cmd(m,"filename")
@dp.message(Command("remove_mentions"))
async def mentionc(m):await toggle_cmd(m,"remove_mentions")
@dp.message(Command("backfill"))
async def backfillc(m):await toggle_cmd(m,"backfill")
@dp.message(Command("backfill_limit"))
async def blimit(m):
    if await guard(m):
        try:setv("backfill_limit",max(1,int((m.text or "").split(maxsplit=1)[1])));await m.answer("✅ Backfill limit: "+s("backfill_limit"))
        except:await m.answer("Usage: /backfill_limit 100")
@dp.message(Command("caption_prefix"))
async def cpp(m):
    if await guard(m):setv("caption_prefix",(m.text or "").split(maxsplit=1)[1] if " " in (m.text or "") else "");await m.answer("✅ Caption prefix updated")
@dp.message(Command("caption_suffix"))
async def cps(m):
    if await guard(m):setv("caption_suffix",(m.text or "").split(maxsplit=1)[1] if " " in (m.text or "") else "");await m.answer("✅ Caption suffix updated")
@dp.message(Command("filename_prefix"))
async def fpp(m):
    if await guard(m):setv("filename_prefix",(m.text or "").split(maxsplit=1)[1] if " " in (m.text or "") else "");await m.answer("✅ Filename prefix updated")
@dp.message(Command("filename_suffix"))
async def fps(m):
    if await guard(m):setv("filename_suffix",(m.text or "").split(maxsplit=1)[1] if " " in (m.text or "") else "");await m.answer("✅ Filename suffix updated")
@dp.message(Command("addreplace"))
async def addr(m):
    if not await guard(m):return
    try:a=(m.text or "").split(maxsplit=1)[1];old,new=[x.strip() for x in a.split("|",1)];db.execute("INSERT INTO replacements(old_text,new_text,mode) VALUES(?,?,?)",(old,new,"plain"));db.commit();await m.answer("✅ Replacement added")
    except:await m.answer("Usage: /addreplace old | new")
@dp.message(Command("addregex"))
async def addrx(m):
    if not await guard(m):return
    try:a=(m.text or "").split(maxsplit=1)[1];old,new=[x.strip() for x in a.split("|",1)];re.compile(old);db.execute("INSERT INTO replacements(old_text,new_text,mode) VALUES(?,?,?)",(old,new,"regex"));db.commit();await m.answer("✅ Regex rule added")
    except Exception as e:await m.answer("Usage: /addregex pattern | replacement\n"+str(e))
@dp.message(Command("rules"))
async def rulesc(m):
    if await guard(m):
        rows=rules();await m.answer("✏️ Rules:\n"+("\n".join(f"{r['id']}: [{r['mode']}] {r['old_text']} → {r['new_text']}" for r in rows) or "None"))
@dp.message(Command("delrule"))
async def delrule(m):
    if await guard(m):
        try:i=int((m.text or "").split(maxsplit=1)[1]);db.execute("DELETE FROM replacements WHERE id=?",(i,));db.commit();await m.answer("✅ Deleted")
        except:await m.answer("Usage: /delrule ID")
@dp.message(Command("clearrules"))
async def clear_rules(m):
    if await guard(m):db.execute("DELETE FROM replacements");db.commit();await m.answer("🧹 Rules cleared")
@dp.message(Command("toggle"))
async def togglemedia(m):
    if not await guard(m):return
    mp={"photo":"photos","video":"videos","document":"documents","audio":"audio","voice":"voice","sticker":"stickers","animation":"animations","text":"text"}
    try:k=mp[(m.text or "").split(maxsplit=1)[1].lower()];setv(k,int(not on(k)));await m.answer(f"✅ {k}: {'ON' if on(k) else 'OFF'}")
    except:await m.answer("Usage: /toggle photo|video|document|audio|voice|sticker|animation|text")
@dp.message(Command("backfill_now"))
async def bnow(m):
    if await guard(m):asyncio.create_task(backfill());await m.answer("🚀 Backfill started in background.")

async def backfill():
    try:
        for ref in vals("sources"):
            src=src_cache.get(ref) or await resolve(userbot,ref);src_cache[ref]=src
            dsts=[]
            for d in vals("destinations"):
                e=dst_cache.get(d) or await resolve(userbot,d);dst_cache[d]=e;dsts.append(e)
            async for msg in userbot.iter_messages(src,limit=int(s("backfill_limit")),reverse=True):
                await process(ref,msg,dsts)
    except Exception:log.exception("Backfill failed")

async def process(ref,msg,dsts):
    if not on("enabled") or not allowed(msg) or processed(ref,msg.id):return
    try:
        caption=transform(msg.text) if on("caption") else msg.text
        for d in dsts:
            if s("mode")=="forward":
                await userbot.forward_messages(d,msg)
            elif msg.document and on("filename") and filename(msg)!=new_filename(filename(msg)):
                td=Path("/tmp/tgforward");td.mkdir(exist_ok=True)
                p=Path(await userbot.download_media(msg,file=str(td)))
                np=p.with_name(new_filename(filename(msg)));p.rename(np)
                try:await userbot.send_file(d,str(np),caption=caption,force_document=True)
                finally:
                    try:np.unlink()
                    except:pass
            elif msg.media:
                await userbot.send_file(d,msg.media,caption=caption,supports_streaming=bool(msg.video))
            elif msg.text and on("text"):await userbot.send_message(d,caption)
            await asyncio.sleep(float(s("delay")))
        mark(ref,msg.id);log.info("Copied %s:%s",ref,msg.id)
    except FloodWaitError as e:
        log.warning("Flood wait %s seconds",e.seconds);await asyncio.sleep(e.seconds+2);await process(ref,msg,dsts)
    except RPCError as e:log.error("Telegram error %s:%s: %s",ref,msg.id,e)
    except Exception:log.exception("Copy failed %s:%s",ref,msg.id)

@dp.callback_query()
async def callbacks(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return await c.answer("Unauthorized",show_alert=True)
    x=c.data
    if x=="home":txt="🚀 <b>Advanced Forwarder</b>";kb=menu()
    elif x=="status":txt=f"Bot: {'ON' if on('enabled') else 'OFF'}\nMode: {s('mode')}\nSources: {len(vals('sources'))}\nDestinations: {len(vals('destinations'))}\nDelay: {s('delay')}s";kb=menu()
    elif x=="settings":txt="⚙️ Settings";kb=settings_menu()
    elif x=="toggle":setv("enabled",int(not on("enabled")));txt="⚙️ Settings";kb=settings_menu()
    elif x=="mode":setv("mode","forward" if s("mode")=="media" else "media");txt="Mode: "+s("mode");kb=settings_menu()
    elif x=="media":txt="🎛 Media filters";kb=media_menu()
    elif x.startswith("flip:"):setv(x[5:],int(not on(x[5:])));txt="🎛 Media filters";kb=media_menu()
    elif x=="sources":txt="📥 Sources:\n"+("\n".join(vals("sources")) or "None");kb=menu()
    elif x=="dests":txt="📤 Destinations:\n"+("\n".join(vals("destinations")) or "None");kb=menu()
    elif x=="rules":rows=rules();txt="✏️ Rules:\n"+("\n".join(f"{r['id']}: {r['old_text']} → {r['new_text']}" for r in rows) or "None");kb=menu()
    elif x=="caption":txt="📝 Caption editing is "+("ON" if on("caption") else "OFF")+"\nUse /addreplace or /addregex.";kb=settings_menu()
    elif x=="filename":txt="📁 Filename editing is "+("ON" if on("filename") else "OFF")+"\nUse /addreplace or /addregex.";kb=settings_menu()
    elif x=="delayhelp":txt="Use /delay SECONDS";kb=settings_menu()
    else:txt=HELP;kb=menu()
    await c.message.edit_text(txt,reply_markup=kb);await c.answer()

async def userbot_main():
    global userbot
    userbot=TelegramClient(StringSession(SESSION_STRING),API_ID,API_HASH);await userbot.start()
    me=await userbot.get_me();log.info("Logged in as %s id=%s",getattr(me,"username",None),me.id)
    try:
        for r in vals("sources"):src_cache[r]=await resolve(userbot,r)
        for r in vals("destinations"):dst_cache[r]=await resolve(userbot,r)
    except Exception as e:log.warning("Entity resolution: %s",e)
    if on("backfill"):await backfill()
    @userbot.on(events.NewMessage)
    async def handler(event):
        for ref,e in list(src_cache.items()):
            if getattr(e,"id",None)==getattr(event.chat,"id",None):
                await process(ref,event.message,list(dst_cache.values()));break
    await userbot.run_until_disconnected()

async def main():
    if not SESSION_STRING:raise RuntimeError("SESSION_STRING is empty")
    await asyncio.gather(userbot_main(),dp.start_polling(bot))

if __name__=="__main__":asyncio.run(main())
