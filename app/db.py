"""База SQLite: звонки, сессии, попытки входа, токены Быстрой команды, настройки.

Все запросы — только с параметрами (?), никакой склейки строк с данными пользователя.
"""
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            TEXT PRIMARY KEY,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    contact       TEXT NOT NULL DEFAULT '',
    template      TEXT NOT NULL DEFAULT 'auto',
    kind          TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    source_ext    TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT 'web',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'queued',
    redo          TEXT NOT NULL DEFAULT 'all',
    progress      INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    models        TEXT NOT NULL DEFAULT '',
    short_summary TEXT NOT NULL DEFAULT '',
    notified      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS calls_created ON calls(created_at DESC);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,            -- full | pending (ждёт код второго фактора)
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    csrf       TEXT NOT NULL,
    tries      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS devices (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS attempts (
    id   INTEGER PRIMARY KEY,
    ts   INTEGER NOT NULL,
    ip   TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- password | totp | api
    ok   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_ip ON attempts(ip, kind, ts);
CREATE INDEX IF NOT EXISTS attempts_ts ON attempts(kind, ts);

CREATE TABLE IF NOT EXISTS api_tokens (
    id         INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
    finally:
        con.close()


def init():
    with db() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
    DB_PATH.chmod(0o600)


def now() -> int:
    return int(time.time())


def get_setting(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as con:
        con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def del_setting(key: str):
    with db() as con:
        con.execute("DELETE FROM settings WHERE key = ?", (key,))
