import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(TZ)


def iso(value: datetime | None = None) -> str:
    return (value or now()).astimezone(TZ).isoformat(timespec="microseconds")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def previous_slot(at: datetime, hours: list[int]) -> datetime:
    choices = [at.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    return max(x for x in choices + [x - timedelta(days=1) for x in choices] if x <= at)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, qzone_id TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, initial_since TEXT NOT NULL,
 success_until TEXT, boundary_ids TEXT NOT NULL DEFAULT '[]', last_attempt TEXT,
 last_success TEXT, access_state TEXT NOT NULL DEFAULT 'unverified',
 failures INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 pagination_verified INTEGER NOT NULL DEFAULT 0, validation_notes TEXT NOT NULL DEFAULT '',
 pending_until TEXT, last_slot TEXT
);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 window_start TEXT NOT NULL, window_end TEXT NOT NULL, started_at TEXT NOT NULL,
 ended_at TEXT, coverage TEXT NOT NULL, page_log TEXT NOT NULL DEFAULT '[]',
 boundary_ids TEXT NOT NULL DEFAULT '[]', error TEXT, new_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), external_id TEXT NOT NULL,
 fingerprint_risk INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
 published_at TEXT, collected_at TEXT NOT NULL, body TEXT NOT NULL, batch_id TEXT,
 processing_state TEXT NOT NULL DEFAULT 'pending', late_arrival INTEGER NOT NULL DEFAULT 0,
 UNIQUE(source_id, external_id)
);
CREATE TABLE IF NOT EXISTS post_versions (
 post_id TEXT NOT NULL REFERENCES posts(id), version INTEGER NOT NULL,
 body TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(post_id,version)
);
CREATE TABLE IF NOT EXISTS media (
 id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES posts(id), post_version INTEGER NOT NULL,
 position INTEGER NOT NULL, url TEXT NOT NULL, path TEXT, mime TEXT, state TEXT NOT NULL DEFAULT 'pending',
 UNIQUE(post_id,post_version,position)
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL, version INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 due_at TEXT NOT NULL, created_at TEXT NOT NULL, error TEXT,
 UNIQUE(kind,ref,version)
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES posts(id), event_key TEXT NOT NULL,
 version INTEGER NOT NULL, status TEXT NOT NULL, identity_level TEXT NOT NULL, body TEXT NOT NULL,
 model_version TEXT NOT NULL, processed_at TEXT NOT NULL, human_locked INTEGER NOT NULL DEFAULT 0,
 proposal TEXT, merged_into TEXT,
 UNIQUE(post_id,event_key)
);
CREATE TABLE IF NOT EXISTS event_sources (
 event_id TEXT NOT NULL REFERENCES events(id), post_id TEXT NOT NULL REFERENCES posts(id),
 PRIMARY KEY(event_id,post_id)
);
CREATE TABLE IF NOT EXISTS event_history (
 event_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(event_id,version)
);
CREATE TABLE IF NOT EXISTS handoffs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
 action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL, created_at TEXT NOT NULL,
 acked_at TEXT, last_error TEXT, UNIQUE(event_id,event_version,action)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, operator TEXT NOT NULL,
 action TEXT NOT NULL, before_value TEXT, after_value TEXT, reason TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
 id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, cost REAL NOT NULL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state,due_at);
CREATE INDEX IF NOT EXISTS handoffs_due ON handoffs(state,next_at,seq);
CREATE INDEX IF NOT EXISTS events_processed ON events(processed_at,id);
"""


class Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self, hours, sources=None):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            start = iso(previous_slot(now(), hours))
            for sid, name, qq in sources or [("wall", "来源一", ""), ("rights", "来源二", "")]:
                c.execute("INSERT OR IGNORE INTO sources(id,name,qzone_id,initial_since) VALUES(?,?,?,?)",
                          (sid, name, qq, start))
            # The process is a single worker; interrupted work is retried after restart.
            c.execute("UPDATE tasks SET state='pending' WHERE state='running'")
            c.execute("UPDATE batches SET coverage='incomplete',ended_at=?,error='进程中断，待补采' "
                      "WHERE coverage='running'", (iso(),))
        self.path.chmod(0o600)

    def rows(self, sql, params=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None
