from __future__ import annotations

import json
import logging
import time
from threading import Event, Lock, Thread

from .config import Config, ProtectedUser
from .db import Store
from .models import Guidance, Item, managed_state, proposal
from .servers import Jellyfin, ServerError, connect
from .sources import Sources, SourceUnavailable

LOG = logging.getLogger("sitarr")


class Engine:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.stop_event = Event()
        self.thread = None
        self.mutation_lock = Lock()

    def server(self, server_id):
        server = next((s for s in self.config.settings.servers if s.id == server_id and s.enabled), None)
        if not server:
            raise ValueError("Server is missing or disabled")
        return connect(server)

    def start(self):
        with self.store.connect() as c:
            c.execute(
                "UPDATE jobs SET status='failed',error='Service restarted; submit this job again' "
                "WHERE status='running'"
            )
            c.execute(
                "UPDATE changes SET status='uncertain',error='Service interrupted during write; inspect remote state' WHERE status='planned'"
            )
        self.thread = Thread(target=self.worker, name="sitarr-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def worker(self):
        next_sync = time.time() + 60
        while not self.stop_event.is_set():
            with self.store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("""SELECT * FROM jobs WHERE status='queued'
                    ORDER BY (kind IN ('apply','protect','undo') OR
                      (kind='resolve' AND json_extract(payload,'$.key') IS NOT NULL)) DESC,id LIMIT 1""").fetchone()
                if row:
                    c.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            if row:
                job = dict(row)
                try:
                    payload = json.loads(job["payload"])
                    kind = job["kind"]
                    if kind in ("scan", "sync"):
                        self.scan(job["id"])
                    if kind in ("resolve", "sync"):
                        self.resolve(job["id"], payload)
                    elif kind == "apply":
                        self.apply(job["id"], payload)
                    elif kind == "undo":
                        self.undo(int(payload["change_id"]))
                    elif kind == "protect":
                        self.protect(payload)
                    self.store.job(job["id"], status="completed", finished=time.time())
                except Exception as exc:
                    # Never log HTTP request URLs or payloads containing tokens.
                    message = (
                        str(exc)
                        if isinstance(exc, (ServerError, SourceUnavailable, ValueError))
                        else type(exc).__name__
                    )
                    LOG.warning("Job %s failed: %s", job["id"], message)
                    self.store.job(job["id"], status="failed", error=message, finished=time.time())
            else:
                if time.time() >= next_sync:
                    if any(s.enabled for s in self.config.settings.servers):
                        self.store.queue("sync")
                    next_sync = time.time() + self.config.settings.sync_hours * 3600
                self.stop_event.wait(1)

    def scan(self, job_id):
        count = 0
        for config in self.config.settings.servers:
            if not config.enabled:
                continue
            server = connect(config)
            started = time.time()
            try:
                for item in server.all_items():
                    if self.stop_event.is_set():
                        return
                    self.store.upsert(item)
                    existing = self.store.get(item.key)
                    if (
                        self.config.settings.auto_apply
                        and existing["guidance"]
                        and existing["status"] == "applied"
                    ):
                        self.apply_one(item.key)
                    count += 1
                    if count % 25 == 0:
                        self.store.job(job_id, progress=count, message=f"Discovered {count} titles")
            finally:
                server.close()
            with self.store.connect() as c:
                c.execute(
                    "UPDATE items SET status='missing',reason='No longer present on the server' "
                    "WHERE server=? AND seen<?",
                    (config.id, started),
                )
        self.store.job(job_id, progress=count, total=count, message=f"Library scan complete: {count} titles")

    def resolve(self, job_id, payload):
        settings = self.config.settings.model_copy(deep=True)
        if payload.get("key"):
            rows = [self.store.get(payload["key"])]
            if rows[0] is None:
                raise ValueError("Title not found")
        else:
            with self.store.connect() as c:
                cutoff = time.time() - settings.cache_days * 86400
                rows = [
                    dict(r)
                    for r in c.execute(
                        """
                    SELECT * FROM items WHERE status!='missing' AND (checked IS NULL OR checked<?)
                    AND (guidance IS NULL OR json_extract(guidance,'$.source')!='Household override')
                    ORDER BY checked IS NOT NULL,checked,title LIMIT ?
                """,
                        (cutoff, settings.batch_size),
                    )
                ]
        sources = Sources(self.store, settings)
        self.store.job(job_id, total=len(rows), progress=0)
        try:
            for index, row in enumerate(rows):
                if self.stop_event.is_set():
                    return
                if index and not payload.get("key"):
                    with self.store.connect() as c:
                        urgent = c.execute("""SELECT id FROM jobs WHERE status='queued' AND
                            (kind IN ('apply','protect','undo') OR
                             (kind='resolve' AND json_extract(payload,'$.key') IS NOT NULL)) LIMIT 1""").fetchone()
                    if urgent:
                        self.store.queue("resolve", {"resume_after": job_id})
                        self.store.job(
                            job_id,
                            message="Bulk lookup paused for an interactive request; continuation queued",
                        )
                        return
                prior = json.loads(row["guidance"]) if row["guidance"] else None
                if prior and prior["source"] == "Household override" and not payload.get("force"):
                    continue
                item = Item(**json.loads(row["snapshot"]))
                self.store.job(job_id, progress=index, message=f"Checking {item.title}")
                try:
                    guidance = sources.lookup(item, payload.get("review_url"))
                except SourceUnavailable as exc:
                    # A network outage must not replace a valid age with unknown.
                    if not prior:
                        self.store.mark(item.key, "error", str(exc))
                    raise
                if not guidance and prior:
                    self.store.mark(
                        item.key,
                        "review",
                        "Previously rated; source could not be reconfirmed. Existing restriction retained.",
                    )
                    continue
                self.store.guidance(
                    item.key, guidance, "No exact title/year match; review required" if not guidance else ""
                )
                if settings.auto_apply:
                    self.apply_one(item.key)
                self.store.job(job_id, progress=index + 1)
        finally:
            sources.close()
        self.store.job(job_id, message="Age lookup complete")
        if not payload.get("key"):
            with self.store.connect() as c:
                remaining = c.execute(
                    "SELECT 1 FROM items WHERE checked IS NULL AND status!='missing' LIMIT 1"
                ).fetchone()
            if remaining:
                # Batches bound memory/interrupt latency, not the initial library
                # size. Backfill continues at provider pacing and yields to UI jobs.
                self.store.queue("resolve", {"resume_after": job_id})

    def apply(self, job_id, payload):
        if payload.get("key"):
            keys = [payload["key"]]
        else:
            with self.store.connect() as c:
                keys = [r[0] for r in c.execute("SELECT key FROM items WHERE status='ready'")]
        self.store.job(job_id, total=len(keys), progress=0)
        for i, key in enumerate(keys):
            if self.stop_event.is_set():
                return
            self.apply_one(key)
            self.store.job(job_id, progress=i + 1, message=f"Applied {i + 1} of {len(keys)} titles")

    def apply_one(self, key):
        row = self.store.get(key)
        if not row:
            raise ValueError("Title not found")
        guidance = Guidance(**json.loads(row["guidance"])) if row["guidance"] else None
        server = self.server(row["server"])
        try:
            item = server.get(row["remote_id"])
            with self.store.connect() as c:
                uncertain = c.execute(
                    "SELECT id,after_state FROM changes WHERE server=? AND remote_id=? AND status='uncertain'",
                    (item.server, item.id),
                ).fetchall()
                for change in uncertain:
                    if managed_state(item) == json.loads(change["after_state"]):
                        c.execute(
                            "UPDATE changes SET status='applied',error=NULL WHERE id=?", (change["id"],)
                        )
            if any(f in item.locked for f in ["CustomRating", "OfficialRating", "Tags"]):
                self.store.mark(key, "review", "Rating or tags are locked; no automatic edit")
                return
            self.write_item(server, item, guidance)
            # Series-level restrictions do not reliably protect direct episode
            # playback. Stamp episodes (and Jellyfin seasons) too, with each
            # child's own stricter restriction preserved and each write audited.
            if item.kind == "series":
                for child in server.all_items(parent=item.id):
                    if self.stop_event.is_set():
                        return
                    self.write_item(server, child, guidance, root_key=key)
            fresh = server.get(item.id)
            self.store.upsert(fresh)
            self.store.mark(key, "applied" if guidance else "unreviewed", applied=managed_state(fresh))
        finally:
            server.close()

    def write_item(self, server, item, guidance, root_key=None):
        if any(f in item.locked for f in ["CustomRating", "OfficialRating", "Tags"]):
            if not root_key:
                self.store.mark(item.key, "review", "Rating or tags are locked; no automatic edit")
            return
        after = proposal(item, guidance)
        before = managed_state(item)
        if before == after:
            return
        with self.mutation_lock:
            # Re-read to detect edits between the scan and the write.
            live = server.get(item.id)
            if managed_state(live) != before:
                raise ValueError(f"{item.title}: metadata changed during apply; retry after review")
            with self.store.connect() as c:
                change_id = c.execute(
                    """INSERT INTO changes(item_key,title,server,remote_id,
                    before_state,after_state,status,created) VALUES(?,?,?,?,?,?,'planned',?)""",
                    (
                        root_key or item.key,
                        item.title,
                        item.server,
                        item.id,
                        json.dumps(before),
                        json.dumps(after),
                        time.time(),
                    ),
                ).lastrowid
            try:
                server.write(item.id, after)
            except Exception:
                with self.store.connect() as c:
                    c.execute(
                        "UPDATE changes SET status='uncertain',error='Write failed; inspect remote state before retry' WHERE id=?",
                        (change_id,),
                    )
                raise
            with self.store.connect() as c:
                c.execute("UPDATE changes SET status='applied' WHERE id=?", (change_id,))

    def undo(self, change_id):
        with self.store.connect() as c:
            row = c.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        if not row or row["status"] not in ("applied", "uncertain"):
            raise ValueError("Change is missing or already reverted")
        server = self.server(row["server"])
        try:
            with self.mutation_lock:
                current = server.get(row["remote_id"])
                if managed_state(current) != json.loads(row["after_state"]):
                    raise ValueError("Metadata was edited after this change; refusing to overwrite it")
                server.write(row["remote_id"], json.loads(row["before_state"]))
                with self.store.connect() as c:
                    c.execute("UPDATE changes SET status='reverted',error=NULL WHERE id=?", (change_id,))
                self.store.mark(
                    row["item_key"], "review", "Change reverted; automatic lookup paused for this title"
                )
                with self.store.connect() as c:
                    # A future auto-run must not immediately reapply an undo.
                    c.execute(
                        "UPDATE items SET checked=? WHERE key=?",
                        (time.time() + 3650 * 86400, row["item_key"]),
                    )
        finally:
            server.close()

    def protect(self, payload):
        user = ProtectedUser.model_validate(payload)
        server = self.server(user.server_id)
        if not isinstance(server, Jellyfin):
            server.close()
            raise ValueError("Plex user restrictions are configured in Plex Home/sharing settings")
        try:
            policy = server.policy(user.user_id)
            with self.store.connect() as c:
                c.execute(
                    "INSERT INTO policy_backups(server,user_id,policy,created) VALUES(?,?,?,?)",
                    (user.server_id, user.user_id, json.dumps(policy), time.time()),
                )
            server.protect(user.user_id, user.max_age, user.block_unreviewed)
            settings = self.config.settings.model_copy(deep=True)
            settings.protected_users = [
                u
                for u in settings.protected_users
                if (u.server_id, u.user_id) != (user.server_id, user.user_id)
            ] + [user]
            self.config.save(settings)
        finally:
            server.close()
