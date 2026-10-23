import asyncio
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
        self._slot_lock = asyncio.Lock()
        self._pending = []
        self._pending_lock = asyncio.Lock()
        self._seq = 0
        self._wake = None
        self._worker = None
        self._session = None

    def configure(self, calls_per_minute, order):
        self.calls_per_minute = max(1, int(calls_per_minute or 80))
        cleaned = []
        for cog_id in order or []:
            cog_id = str(cog_id)
            if cog_id and cog_id not in cleaned:
                cleaned.append(cog_id)
        for cog_id in DEFAULT_ORDER:
            if cog_id not in cleaned:
                cleaned.append(cog_id)
        self.order = cleaned

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

    async def request(self, cog_id, url, timeout=20):
        await self.ensure_started()
        future = asyncio.get_running_loop().create_future()
        async with self._pending_lock:
            self._seq += 1
            self._pending.append(
                {"seq": self._seq, "cog": str(cog_id), "url": url, "timeout": timeout, "future": future}
            )
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


_scheduler = TornScheduler()


def get_scheduler():
    return _scheduler


async def torn_json(cog_id, url, timeout=20):
    return await get_scheduler().request(cog_id, url, timeout)
