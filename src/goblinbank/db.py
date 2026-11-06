"""Per-goblin SQLite store. Signed amounts: positive = money IN, negative = money OUT."""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS plaid_items(item_id TEXT PRIMARY KEY, institution TEXT, token_enc TEXT NOT NULL, cursor TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, institution TEXT, name TEXT, kind TEXT,  -- kind: cash|credit|investment|loan
  balance REAL, currency TEXT DEFAULT 'USD', updated TEXT);
CREATE TABLE IF NOT EXISTS transactions(
  id TEXT PRIMARY KEY, account_id TEXT, date TEXT, name TEXT, amount REAL, currency TEXT DEFAULT 'USD',
  category TEXT, pending INTEGER DEFAULT 0, source TEXT);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date DESC);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USD',
  cycle TEXT NOT NULL, next_date TEXT NOT NULL, category TEXT DEFAULT '', url TEXT DEFAULT '', notes TEXT DEFAULT '',
  source TEXT DEFAULT 'manual', plaid_stream_id TEXT UNIQUE, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS snapshots(ts TEXT, account_id TEXT, balance REAL);
CREATE TABLE IF NOT EXISTS events(  -- the money-movement log that feeds the Atom/RSS feed
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, amount REAL, account_id TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts DESC);
"""


class DB:
    def __init__(self, path: str | None = None):
        path = path or os.environ.get("GOBLIN_DB", "/data/goblin.db")
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None
