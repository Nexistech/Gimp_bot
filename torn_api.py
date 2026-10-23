import asyncio
import sqlite3
import threading
import time
from collections import deque

import aiohttp

from plugin_log import get_logger

log = get_logger("api_queue")

# Lower index is served first. Priority 1 is the first entry.
DEFAULT_ORDER = [
    "shoplifting",
    "chain_alert",
    "banking",
    "verification",
    "armory",
    "armory_payout",
    "oc_tools",
    "overdose",
    "find_trader",
    "faction_roster",
]

CALLER_LABELS = {
    "shoplifting": "Shoplifting",
    "chain_alert": "Chain alert",
    "banking": "Banking",
    "verification": "Verification",
    "armory": "Armory",
    "armory_payout": "Armory payout",
    "oc_tools": "OC tools",
    "overdose": "Overdose",
    "find_trader": "Find trader",
    "faction_roster": "Faction roster",
}


class TornScheduler:
    def __init__(self):
        self.calls_per_minute = 80
        self.order = list(DEFAULT_ORDER)
        self._times = deque()
        self._time_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._db_path = None
        self._history_hours = 48
        self._retain_hours = 48
        self._pending_notes = []
        self._notes = 0
        self._pending = []
        self._pending_lock = asyncio.Lock()
        self._seq = 0
        self._wake = None
        self._worker = None
        self._sampler = None
        self._session = None

    def configure(self, calls_per_minute, order, history_hours=48, db_path=None):
        self.calls_per_minute = max(1, int(calls_per_minute or 80))
        try:
            hours = int(history_hours or 48)
        except (TypeError, ValueError):
            hours = 48
        self._history_hours = max(1, min(336, hours))
        self._retain_hours = max(48, self._history_hours)
        cleaned = []
        for cog_id in order or []:
            cog_id = str(cog_id)
            if cog_id and cog_id not in cleaned:
                cleaned.append(cog_id)
        for cog_id in DEFAULT_ORDER:
            if cog_id not in cleaned:
                cleaned.append(cog_id)
        self.order = cleaned
        if db_path:
            self._db_path = db_path
            self._init_db()
            if self._pending_notes:
                self._insert_calls(self._pending_notes)
                self._pending_notes = []
            self._prune()

    def rank(self, cog_id):
        try:
            return self.order.index(cog_id)
        except ValueError:
            self.order.append(cog_id)
            return len(self.order) - 1

    def snapshot(self):
        now = time.monotonic()
        used = sum(1 for stamp in self._times if now - stamp < 60)
        return {
            "limit": self.calls_per_minute,
            "used": used,
            "waiting": len(self._pending),
        }

    def _init_db(self):
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("CREATE TABLE IF NOT EXISTS calls (ts INTEGER NOT NULL, cog TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
            conn.execute("CREATE TABLE IF NOT EXISTS queue_depth (minute INTEGER PRIMARY KEY, depth INTEGER NOT NULL)")
            conn.commit()
            conn.close()

    def _prune(self):
        if not self._db_path:
            return
        cutoff = int(time.time()) - self._retain_hours * 3600
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM queue_depth WHERE minute < ?", (cutoff,))
            conn.commit()
            conn.close()

    def note_call(self, cog):
        row = (int(time.time()), str(cog or "unknown"))
        if not self._db_path:
            self._pending_notes.append(row)
            return
        self._insert_calls([row])
        self._notes += 1
        if self._notes % 50 == 0:
            self._prune()

    def _insert_calls(self, rows):
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.executemany("INSERT INTO calls (ts, cog) VALUES (?, ?)", rows)
            conn.commit()
            conn.close()

    def _record_queue(self):
        if not self._db_path:
            return
        minute = int(time.time()) // 60 * 60
        depth = len(self._pending)
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute(
                """
                INSERT INTO queue_depth (minute, depth) VALUES (?, ?)
                ON CONFLICT(minute) DO UPDATE SET depth = MAX(depth, excluded.depth)
                """,
                (minute, depth),
            )
            conn.commit()
            conn.close()

    def history(self, hours=None):
        hours = self._history_hours if hours is None else max(1, min(336, int(hours)))
        now = int(time.time())
        start = now - hours * 3600
        if hours <= 6:
            bucket = 60
        elif hours <= 48:
            bucket = 15 * 60
        elif hours <= 24 * 7:
            bucket = 3600
        else:
            bucket = 2 * 3600
        start = start // bucket * bucket
        calls = {}
        totals = {}
        depths = {}
        if self._db_path:
            with self._db_lock:
                conn = sqlite3.connect(self._db_path, timeout=5)
                for ts, cog, count in conn.execute(
                    "SELECT (ts / ?) * ?, cog, COUNT(*) FROM calls WHERE ts >= ? GROUP BY 1, cog",
                    (bucket, bucket, start),
                ):
                    calls.setdefault(int(ts), {})[cog] = int(count)
                    totals[cog] = totals.get(cog, 0) + int(count)
                for minute, depth in conn.execute(
                    "SELECT minute, depth FROM queue_depth WHERE minute >= ?",
                    (start,),
                ):
                    slot = int(minute) // bucket * bucket
                    depths[slot] = max(depths.get(slot, 0), int(depth))
                conn.close()
        points = []
        slot = start
        while slot <= now:
            points.append(
                {
                    "t": slot,
                    "label": time.strftime("%m-%d %H:%M", time.gmtime(slot)),
                    "calls": calls.get(slot, {}),
                    "total": sum(calls.get(slot, {}).values()),
                    "queue": depths.get(slot, 0),
                }
            )
            slot += bucket
        ranked = sorted(totals, key=lambda cog: (-totals[cog], cog))
        stats = self.snapshot()
        return {
            **stats,
            "hours": hours,
            "bucket": bucket,
            "points": points,
            "totals": totals,
            "cogs": ranked,
        }

    def _slot_wait(self):
        with self._time_lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            gap = 60 / max(1, self.calls_per_minute)
            if self._times and now - self._times[-1] < gap:
                return gap - (now - self._times[-1])
            if len(self._times) >= self.calls_per_minute:
                return 60 - (now - self._times[0]) + 0.05
            self._times.append(now)
            return 0

    async def reserve(self):
        while True:
            wait = self._slot_wait()
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def ensure_started(self):
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())
        if self._sampler is None or self._sampler.done():
            self._sampler = asyncio.get_running_loop().create_task(self._sample_loop())

    async def request(self, cog_id, url, timeout=20):
        await self.ensure_started()
        future = asyncio.get_running_loop().create_future()
        async with self._pending_lock:
            self._seq += 1
            self._pending.append(
                {"seq": self._seq, "cog": str(cog_id), "url": url, "timeout": timeout, "future": future}
            )
            self._record_queue()
        self._wake.set()
        return await future

    async def _next(self):
        async with self._pending_lock:
            if not self._pending:
                return None
            self._pending.sort(key=lambda item: (self.rank(item["cog"]), item["seq"]))
            return self._pending.pop(0)

    async def _run(self):
        self._session = aiohttp.ClientSession(headers={"User-Agent": "GimpBot/1.0"})
        try:
            while True:
                item = await self._next()
                if item is None:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                future = item["future"]
                if future.done():
                    continue
                try:
                    await self.reserve()
                    self.note_call(item["cog"])
                    async with self._session.get(item["url"], timeout=item["timeout"]) as response:
                        data = await response.json(content_type=None)
                        if response.status >= 400:
                            future.set_exception(RuntimeError(f"HTTP {response.status}"))
                        else:
                            future.set_result(data)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                    log.error(f"[API] {item['cog']} failed: {exc}")
        finally:
            await self._session.close()
            self._session = None

    async def _sample_loop(self):
        while True:
            async with self._pending_lock:
                self._record_queue()
            await asyncio.sleep(10)


_scheduler = TornScheduler()


def get_scheduler():
    return _scheduler


async def torn_json(cog_id, url, timeout=20):
    return await get_scheduler().request(cog_id, url, timeout)
