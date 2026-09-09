import aiosqlite
from pathlib import Path

DB_PATH = Path("data/forwarder.db")

async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            ref TEXT PRIMARY KEY,
            title TEXT,
            entity_id INTEGER,
            entity_type TEXT,
            username TEXT
        );
        CREATE TABLE IF NOT EXISTS destinations (
            ref TEXT PRIMARY KEY,
            title TEXT,
            entity_id INTEGER,
            entity_type TEXT,
            username TEXT
        );
        CREATE TABLE IF NOT EXISTS processed (
            source_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY(source_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS replacements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            replacement TEXT NOT NULL,
            is_regex INTEGER NOT NULL DEFAULT 0,
            target TEXT NOT NULL DEFAULT 'caption'
        );
        """)
        await db.commit()

async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        await db.commit()

async def list_entities(table):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT ref,title,entity_id,entity_type,username FROM {table} ORDER BY ref")
        return await cur.fetchall()

async def upsert_entity(table, ref, title, entity_id, entity_type, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT INTO {table}(ref,title,entity_id,entity_type,username) VALUES(?,?,?,?,?) "
            f"ON CONFLICT(ref) DO UPDATE SET title=excluded.title, entity_id=excluded.entity_id, "
            f"entity_type=excluded.entity_type, username=excluded.username",
            (ref, title, entity_id, entity_type, username)
        )
        await db.commit()

async def delete_entity(table, ref):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM {table} WHERE ref=?", (ref,))
        await db.commit()

async def get_replacements():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,pattern,replacement,is_regex,target FROM replacements ORDER BY id")
        return await cur.fetchall()

async def add_replacement(pattern, replacement, is_regex=False, target="caption"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO replacements(pattern,replacement,is_regex,target) VALUES(?,?,?,?)",
            (pattern, replacement, int(is_regex), target)
        )
        await db.commit()

async def delete_replacement(rid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM replacements WHERE id=?", (rid,))
        await db.commit()

async def clear_replacements():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM replacements")
        await db.commit()

async def already_processed(source_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM processed WHERE source_id=? AND message_id=?",
            (source_id, message_id)
        )
        return await cur.fetchone() is not None

async def mark_processed(source_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO processed(source_id,message_id) VALUES(?,?)",
            (source_id, message_id)
        )
        await db.commit()
