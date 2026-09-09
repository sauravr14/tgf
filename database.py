import asyncio
import sqlite3
from pathlib import Path

class ProcessedStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.commit()

    async def exists(self, message_id):
        return await asyncio.to_thread(self._exists_sync, message_id)

    def _exists_sync(self, message_id):
        with self._conn() as con:
            row = con.execute(
                "SELECT 1 FROM processed_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            return row is not None

    async def add(self, message_id):
        await asyncio.to_thread(self._add_sync, message_id)

    def _add_sync(self, message_id):
        with self._conn() as con:
            con.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id) VALUES(?)",
                (message_id,),
            )
            con.commit()
