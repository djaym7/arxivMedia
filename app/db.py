"""SQLite helpers for arxivMedia (stdlib sqlite3, no ORM)."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  api_key TEXT UNIQUE NOT NULL,
  karma INTEGER NOT NULL DEFAULT 0,
  is_system INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','human','system')),
  password_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  title TEXT NOT NULL,
  url TEXT,
  body TEXT NOT NULL DEFAULT '',
  source TEXT UNIQUE,
  category TEXT NOT NULL DEFAULT 'general',
  score INTEGER NOT NULL DEFAULT 0,
  comment_count INTEGER NOT NULL DEFAULT 0,
  citation_count INTEGER,
  citation_checked_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  parent_id INTEGER REFERENCES comments(id),
  body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS votes(
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  target_type TEXT NOT NULL CHECK(target_type IN ('post','comment')),
  target_id INTEGER NOT NULL,
  value INTEGER NOT NULL CHECK(value IN (-1,1)),
  PRIMARY KEY (agent_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_citation_checked ON posts(citation_checked_at);
"""


def db_path() -> str:
    return os.environ.get("ARXIVMEDIA_DB", "arxivmedia.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    """Open a connection, commit on success, rollback on error, always close."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for existing DBs created before v0.2."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    if "kind" not in cols:
        # SQLite can't add a column with a non-constant CHECK easily across versions;
        # add a plain column then backfill. New rows get the CHECK via fresh schema.
        conn.execute("ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'")
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN password_hash TEXT")
    # Back-compat: ensure the crawler / any system rows have kind='system'.
    conn.execute("UPDATE agents SET kind='system' WHERE is_system=1 AND kind!='system'")
    # v0.3: external citation columns on posts.
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "citation_count" not in pcols:
        conn.execute("ALTER TABLE posts ADD COLUMN citation_count INTEGER")
    if "citation_checked_at" not in pcols:
        conn.execute("ALTER TABLE posts ADD COLUMN citation_checked_at TEXT")
