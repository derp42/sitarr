from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import Guidance, Item


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS items (
                    key TEXT PRIMARY KEY, server TEXT NOT NULL, remote_id TEXT NOT NULL,
                    title TEXT NOT NULL, year INTEGER, kind TEXT NOT NULL, snapshot TEXT NOT NULL,
                    guidance TEXT, status TEXT NOT NULL DEFAULT 'pending', reason TEXT,
                    checked REAL, seen REAL NOT NULL, applied TEXT
                );
                CREATE INDEX IF NOT EXISTS item_status ON items(status, checked);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, value TEXT, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY, item_key TEXT NOT NULL, title TEXT NOT NULL,
                    server TEXT NOT NULL, remote_id TEXT NOT NULL, before_state TEXT NOT NULL,
                    after_state TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, finished REAL,
                    progress INTEGER DEFAULT 0, total INTEGER DEFAULT 0, message TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS policy_backups (
                    id INTEGER PRIMARY KEY, server TEXT NOT NULL, user_id TEXT NOT NULL,
                    policy TEXT NOT NULL, created REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def upsert(self, item: Item):
        with self.connect() as c:
            c.execute(
                """INSERT INTO items(key,server,remote_id,title,year,kind,snapshot,seen)
                         VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                         title=excluded.title,year=excluded.year,kind=excluded.kind,
                         snapshot=excluded.snapshot,seen=excluded.seen""",
                (
                    item.key,
                    item.server,
                    item.id,
                    item.title,
                    item.year,
                    item.kind,
                    json.dumps(item.data()),
                    time.time(),
                ),
            )

    def get(self, key):
        with self.connect() as c:
            r = c.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
            return dict(r) if r else None

    def items(self, q="", status="", limit=100, offset=0):
        sql = "SELECT * FROM items WHERE (title LIKE ? OR ?='') AND (status=? OR ?='')"
        args = (f"%{q}%", q, status, status)
        with self.connect() as c:
            total = c.execute("SELECT count(*) FROM (" + sql + ")", args).fetchone()[0]
            rows = c.execute(sql + " ORDER BY title LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for k in ["snapshot", "guidance", "applied"]:
                d[k] = json.loads(d[k]) if d[k] else None
            result.append(d)
        return {"items": result, "total": total}

    def guidance(self, key, guidance: Guidance | None, reason=""):
        with self.connect() as c:
            c.execute(
                "UPDATE items SET guidance=?,status=?,reason=?,checked=? WHERE key=?",
                (
                    json.dumps(guidance.data()) if guidance else None,
                    "ready" if guidance else "unreviewed",
                    reason,
                    time.time(),
                    key,
                ),
            )

    def mark(self, key, status, reason="", applied=None):
        with self.connect() as c:
            if applied is not None:
                c.execute(
                    "UPDATE items SET status=?,reason=?,applied=? WHERE key=?",
                    (status, reason, json.dumps(applied), key),
                )
            else:
                c.execute("UPDATE items SET status=?,reason=? WHERE key=?", (status, reason, key))

    def cached(self, key):
        with self.connect() as c:
            r = c.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return (True, json.loads(r[0])) if r else (False, None)

    def cache(self, key, value, days):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                (key, json.dumps(value), time.time() + days * 86400),
            )

    def queue(self, kind, payload=None):
        with self.connect() as c:
            existing = c.execute(
                "SELECT id FROM jobs WHERE kind=? AND payload=? AND status IN ('queued','running')",
                (kind, json.dumps(payload or {}, sort_keys=True)),
            ).fetchone()
            if existing:
                return existing[0]
            return c.execute(
                "INSERT INTO jobs(kind,payload,status,created) VALUES(?,?,'queued',?)",
                (kind, json.dumps(payload or {}, sort_keys=True), time.time()),
            ).lastrowid

    def jobs(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 25")]

    def job(self, job_id, **fields):
        allowed = {"status", "progress", "total", "message", "error", "finished"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid job update")
        with self.connect() as c:
            c.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), job_id),
            )

    def stats(self):
        with self.connect() as c:
            counts = dict(c.execute("SELECT status,count(*) FROM items GROUP BY status").fetchall())
            changes = c.execute("SELECT count(*) FROM changes WHERE status='applied'").fetchone()[0]
        return {"counts": counts, "total": sum(counts.values()), "changes": changes}

    def ledger(self, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,))]
