"""SQLite map between Discord and Vector objects.

Every cloned object (community, role, category, channel) gets a row in ``links``; every relayed message gets a
row in ``messages`` so replies, edits and deletes can follow it to the other side. Discord IDs are stored as text.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    kind        TEXT NOT NULL,          -- guild | role | category | channel | forum_thread
    discord_id  TEXT NOT NULL,
    vector_id   TEXT NOT NULL,
    guild_id    TEXT NOT NULL,          -- the Discord guild this belongs to
    PRIMARY KEY (kind, discord_id)
);
CREATE INDEX IF NOT EXISTS links_vector ON links (kind, vector_id);

CREATE TABLE IF NOT EXISTS messages (
    discord_id      TEXT NOT NULL,
    vector_id       TEXT NOT NULL,
    discord_channel TEXT NOT NULL,
    vector_channel  TEXT NOT NULL,
    origin          TEXT NOT NULL,      -- 'discord' or 'vector': where the message was written
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_discord ON messages (discord_id);
CREATE INDEX IF NOT EXISTS messages_vector ON messages (vector_id);

CREATE TABLE IF NOT EXISTS webhooks (
    discord_channel TEXT PRIMARY KEY,
    webhook_id      TEXT NOT NULL,
    webhook_token   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class MessageLink:
    discord_id: str
    vector_id: str
    discord_channel: str
    vector_channel: str
    origin: str


class Store:
    def __init__(self, path: str = "bridge.db"):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- links ----------------------------------------------------------------------------

    def link(self, kind: str, discord_id: object, vector_id: str, guild_id: object) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO links (kind, discord_id, vector_id, guild_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (kind, discord_id) DO UPDATE SET vector_id = excluded.vector_id, guild_id = excluded.guild_id",
                (kind, str(discord_id), vector_id, str(guild_id)),
            )
            self._db.commit()

    def unlink(self, kind: str, discord_id: object) -> None:
        with self._lock:
            self._db.execute("DELETE FROM links WHERE kind = ? AND discord_id = ?", (kind, str(discord_id)))
            self._db.commit()

    def vector_id(self, kind: str, discord_id: object) -> Optional[str]:
        row = self._db.execute("SELECT vector_id FROM links WHERE kind = ? AND discord_id = ?",
                               (kind, str(discord_id))).fetchone()
        return row[0] if row else None

    def discord_id(self, kind: str, vector_id: str) -> Optional[int]:
        row = self._db.execute("SELECT discord_id FROM links WHERE kind = ? AND vector_id = ?",
                               (kind, vector_id)).fetchone()
        return int(row[0]) if row else None

    def links(self, kind: str, guild_id: object = None) -> List[Tuple[int, str]]:
        if guild_id is None:
            rows = self._db.execute("SELECT discord_id, vector_id FROM links WHERE kind = ?", (kind,))
        else:
            rows = self._db.execute("SELECT discord_id, vector_id FROM links WHERE kind = ? AND guild_id = ?",
                                    (kind, str(guild_id)))
        return [(int(d), v) for d, v in rows.fetchall()]

    def guild_of_vector_channel(self, vector_channel: str) -> Optional[int]:
        row = self._db.execute("SELECT guild_id FROM links WHERE kind = 'channel' AND vector_id = ?",
                               (vector_channel,)).fetchone()
        return int(row[0]) if row else None

    # -- messages -------------------------------------------------------------------------

    def add_message(self, discord_id: object, vector_id: str, discord_channel: object, vector_channel: str,
                    origin: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (discord_id, vector_id, discord_channel, vector_channel, origin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(discord_id), vector_id, str(discord_channel), vector_channel, origin, int(time.time())),
            )
            self._db.commit()

    def _rows(self, column: str, value: str) -> List[MessageLink]:
        rows = self._db.execute(
            f"SELECT discord_id, vector_id, discord_channel, vector_channel, origin FROM messages WHERE {column} = ?",
            (value,),
        ).fetchall()
        return [MessageLink(*r) for r in rows]

    def by_discord(self, discord_id: object) -> List[MessageLink]:
        """A Discord message can map to several Vector messages (text + each attachment)."""
        return self._rows("discord_id", str(discord_id))

    def by_vector(self, vector_id: str) -> List[MessageLink]:
        return self._rows("vector_id", vector_id)

    def forget_discord(self, discord_id: object) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE discord_id = ?", (str(discord_id),))
            self._db.commit()

    def forget_vector(self, vector_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE vector_id = ?", (vector_id,))
            self._db.commit()

    def prune_messages(self, older_than_days: int) -> int:
        cutoff = int(time.time()) - older_than_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    # -- webhooks -------------------------------------------------------------------------

    def webhook(self, discord_channel: object) -> Optional[Tuple[int, str]]:
        row = self._db.execute("SELECT webhook_id, webhook_token FROM webhooks WHERE discord_channel = ?",
                               (str(discord_channel),)).fetchone()
        return (int(row[0]), row[1]) if row else None

    def set_webhook(self, discord_channel: object, webhook_id: object, token: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO webhooks (discord_channel, webhook_id, webhook_token) VALUES (?, ?, ?) "
                "ON CONFLICT (discord_channel) DO UPDATE SET webhook_id = excluded.webhook_id, "
                "webhook_token = excluded.webhook_token",
                (str(discord_channel), str(webhook_id), token),
            )
            self._db.commit()

    def drop_webhook(self, discord_channel: object) -> None:
        with self._lock:
            self._db.execute("DELETE FROM webhooks WHERE discord_channel = ?", (str(discord_channel),))
            self._db.commit()

    def webhook_ids(self) -> Iterator[int]:
        for (wid,) in self._db.execute("SELECT webhook_id FROM webhooks").fetchall():
            yield int(wid)
