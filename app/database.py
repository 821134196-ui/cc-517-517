"""SQLite 持久层：营位、投诉链、事件、接单、现场记录、处置/申诉/复核、音量照片、活动豁免。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'standard',
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    quiet_start TEXT NOT NULL DEFAULT '22:00',
    quiet_end   TEXT NOT NULL DEFAULT '07:00'
);

CREATE TABLE IF NOT EXISTS adjacency (
    site_a      INTEGER NOT NULL REFERENCES sites(id),
    site_b      INTEGER NOT NULL REFERENCES sites(id),
    PRIMARY KEY (site_a, site_b)
);

CREATE TABLE IF NOT EXISTS guests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    site_id     INTEGER REFERENCES sites(id)
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reported_site_id  INTEGER NOT NULL REFERENCES sites(id),
    first_at          TEXT NOT NULL,
    quiet             INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open',
        -- open / assigned / resolved / appealed / closed / withdrawn / false_alarm
    exempt            INTEGER NOT NULL DEFAULT 0,
    assigned_to       TEXT,
    assigned_at       TEXT,
    disposition       TEXT,
    resolution_note   TEXT,
    resolved_at       TEXT,
    appeal_note       TEXT,
    appealed_at       TEXT,
    reviewer          TEXT,
    review_note       TEXT,
    reviewed_at       TEXT
);

CREATE TABLE IF NOT EXISTS complaints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    complainant_id INTEGER REFERENCES guests(id),
    site_id     INTEGER REFERENCES sites(id),
    anonymous   INTEGER NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS assignments_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    officer     TEXT NOT NULL,
    claimed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    officer     TEXT NOT NULL,
    db_reading  REAL NOT NULL,
    quiet       INTEGER NOT NULL,
    over_limit  INTEGER NOT NULL,
    reading_at  TEXT NOT NULL,
    arrive_at   TEXT NOT NULL,
    duration_s  INTEGER,
    outcome     TEXT NOT NULL,
    photo_path  TEXT,
    device_ok   INTEGER NOT NULL DEFAULT 1,
    device_note TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    reviewer    TEXT NOT NULL,
    result      TEXT NOT NULL,   -- upheld / downgraded_false_alarm / reopen
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    site_id     INTEGER REFERENCES sites(id),  -- NULL = 全营地范围
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS move_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    from_site   INTEGER NOT NULL REFERENCES sites(id),
    to_site     INTEGER NOT NULL REFERENCES sites(id),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'proposed'  -- proposed / accepted / rejected
);

CREATE TABLE IF NOT EXISTS messages_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class Database:
    """每个连接一把锁；接单竞争用 BEGIN IMMEDIATE 事务 + 条件 UPDATE 兜底。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._tls, "conn"):
            c = self._new_conn()
            self._tls.conn = c
            with self._lock:
                self._all_conns.append(c)
        return self._tls.conn

    def init(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()
            if hasattr(self._tls, "conn"):
                del self._tls.conn

    # ---------- 基础查询 ----------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def begin(self):
        """立即写锁事务（用于接单竞争）。调用方必须最终调用 commit() 或 rollback()。"""
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self._lock.release()
            raise
        return self.conn

    def commit(self) -> None:
        self.conn.commit()
        self._lock.release()

    def rollback(self) -> None:
        self.conn.rollback()
        self._lock.release()


def neighbors(db: Database, site_id: int) -> list[int]:
    rows = db.query(
        """SELECT site_a AS s FROM adjacency WHERE site_b = ?
           UNION SELECT site_b AS s FROM adjacency WHERE site_a = ?
           ORDER BY s""",
        (site_id, site_id),
    )
    return [r["s"] for r in rows]


def site_map(db: Database) -> dict[int, sqlite3.Row]:
    return {r["id"]: r for r in db.query("SELECT * FROM sites ORDER BY id")}


def export_chain(db: Database, event_id: int) -> dict:
    """调试/测试用：导出一个事件的完整投诉链（不含身份脱敏）。"""
    ev = db.one("SELECT * FROM events WHERE id = ?", (event_id,))
    complaints = db.query("SELECT * FROM complaints WHERE event_id = ? ORDER BY id", (event_id,))
    records = db.query("SELECT * FROM site_records WHERE event_id = ? ORDER BY id", (event_id,))
    reviews = db.query("SELECT * FROM reviews WHERE event_id = ? ORDER BY id", (event_id,))
    return {
        "event": dict(ev) if ev else None,
        "complaints": [dict(c) for c in complaints],
        "records": [dict(r) for r in records],
        "reviews": [dict(r) for r in reviews],
    }
