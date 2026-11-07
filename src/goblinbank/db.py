"""Per-goblin SQLite store. Signed amounts: positive = money IN, negative = money OUT."""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS plaid_items(item_id TEXT PRIMARY KEY, institution TEXT, token_enc TEXT NOT NULL, cursor TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, item_id TEXT, source TEXT NOT NULL, institution TEXT, name TEXT, kind TEXT,  -- kind: cash|credit|investment|loan
  balance REAL, currency TEXT DEFAULT 'USD', updated TEXT);
CREATE TABLE IF NOT EXISTS transactions(
  id TEXT PRIMARY KEY, account_id TEXT, date TEXT, name TEXT, amount REAL, currency TEXT DEFAULT 'USD',
  category TEXT, pending INTEGER DEFAULT 0, source TEXT);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date DESC);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USD',
  cycle TEXT NOT NULL, next_date TEXT NOT NULL, category TEXT DEFAULT '', url TEXT DEFAULT '', notes TEXT DEFAULT '',
  source TEXT DEFAULT 'manual', plaid_stream_id TEXT UNIQUE, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS holdings(account_id TEXT, symbol TEXT, name TEXT, quantity REAL, price REAL, value REAL, PRIMARY KEY(account_id, symbol));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT, secret INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS plaid_usage(day TEXT, endpoint TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY(day, endpoint));
CREATE TABLE IF NOT EXISTS categories(name TEXT PRIMARY KEY, kind TEXT DEFAULT 'expense', builtin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS category_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL UNIQUE, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(category TEXT PRIMARY KEY, monthly_amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS savings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT DEFAULT 'savings', institution TEXT DEFAULT '', balance REAL DEFAULT 0,
  apy REAL DEFAULT 0, opened TEXT, maturity TEXT, term_months INTEGER, account_id TEXT, goal REAL, notes TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT DEFAULT '', author TEXT DEFAULT 'me', pinned INTEGER DEFAULT 0, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS providers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL,
  key_enc TEXT DEFAULT '', tools INTEGER DEFAULT 1, allow_cloud INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, amount REAL, every INTEGER, unit TEXT, account_id TEXT, reason TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS dismissed(key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, provider_id INTEGER, summary TEXT DEFAULT '', created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, trace TEXT DEFAULT '', compacted INTEGER DEFAULT 0, created TEXT);
CREATE INDEX IF NOT EXISTS cm_chat ON chat_messages(chat_id, id);
CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, target REAL NOT NULL, deadline TEXT, priority INTEGER DEFAULT 2, monthly REAL DEFAULT 0,
  note TEXT DEFAULT '', status TEXT DEFAULT 'active', created TEXT, closed TEXT);
CREATE TABLE IF NOT EXISTS allocations(id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER NOT NULL, place TEXT NOT NULL DEFAULT '', amount REAL NOT NULL, UNIQUE(goal_id, place));
CREATE TABLE IF NOT EXISTS incomes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL, every INTEGER DEFAULT 1, unit TEXT DEFAULT 'month', next_date TEXT NOT NULL, account_id TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS chat_links(chat_id INTEGER NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL, PRIMARY KEY(chat_id, kind, ref_id));
CREATE TABLE IF NOT EXISTS api_tokens(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, created TEXT, last_used TEXT, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshots(ts TEXT, account_id TEXT, balance REAL);
CREATE TABLE IF NOT EXISTS events(  -- the money-movement log that feeds the Atom/RSS feed
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, amount REAL, account_id TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts DESC);
"""


COLUMNS = {  # added after the first release: (table, column) -> DDL fragment
    ("subscriptions", "every"): "INTEGER DEFAULT 1", ("subscriptions", "unit"): "TEXT DEFAULT 'month'",
    ("subscriptions", "account_id"): "TEXT", ("subscriptions", "payment_method"): "TEXT DEFAULT ''",
    ("subscriptions", "payer"): "TEXT DEFAULT 'me'", ("subscriptions", "payer_name"): "TEXT DEFAULT ''",
    ("subscriptions", "notify_days"): "INTEGER DEFAULT 3", ("subscriptions", "end_date"): "TEXT", ("subscriptions", "created"): "TEXT", ("subscriptions", "match_key"): "TEXT", ("incomes", "match_key"): "TEXT",
    ("transactions", "plaid_category"): "TEXT", ("transactions", "category_locked"): "INTEGER DEFAULT 0", ("transactions", "notes"): "TEXT DEFAULT ''",
    ("accounts", "min_balance"): "REAL", ("accounts", "subtype"): "TEXT", ("accounts", "savings_pool"): "INTEGER", ("providers", "context_tokens"): "INTEGER DEFAULT 8192",
}
DEFAULT_CATEGORIES = [("Income", "income"), ("Food", "expense"), ("Groceries", "expense"), ("Clothes", "expense"), ("Fun", "expense"), ("Games", "expense"),
                      ("Gas", "expense"), ("Transport", "expense"), ("Bills", "expense"), ("Housing", "expense"), ("Health", "expense"), ("Shopping", "expense"),
                      ("Subscriptions", "expense"), ("Travel", "expense"), ("Savings", "transfer"), ("Investing", "transfer"), ("Transfer", "transfer"), ("Misc", "expense")]


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
            for (table, col), ddl in COLUMNS.items():
                if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            c.executemany("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,1)", DEFAULT_CATEGORIES)

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
