import asyncio
import datetime
import os
import sqlite3
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_settings, schema_defaults
try:
    from config import TORN_API_KEY, FACTION_MEMBERS_URL, FACTION_CRIMES_URL
except ImportError:
    TORN_API_KEY = ""
    FACTION_MEMBERS_URL = "https://api.torn.com/v2/faction/members"
    FACTION_CRIMES_URL = "https://api.torn.com/v2/faction/crimes"

DB_NAME = os.path.join(os.path.dirname(__file__), "faction_roster.db")
MEMBER_REFRESH_MINUTES = 10
CRIME_REFRESH_MINUTES = 10
MAX_COMPLETED_PAGES = 8


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def normalize_username(username):
    return " ".join(str(username).strip().lower().split())


class FactionRoster(commands.Cog):
    """Single Torn puller. Other plugins read this cache/DB instead of calling Torn."""

    SETTINGS_SCHEMA = [
        {"key": "member_refresh_minutes", "type": "int", "label": "Member refresh minutes", "default": 10},
        {"key": "max_completed_pages", "type": "int", "label": "Max completed-crime pages", "default": 8},
    ]

    def __init__(self, bot):
        self.bot = bot
        self.members_by_id = {}
        self.members_by_name = {}
        self.active_crimes = []
        self.completed_crimes = []
        self.last_completed_by_user = {}
        self.last_member_refresh = None
        self.last_crime_refresh = None
        self.settings = load_settings(os.path.dirname(__file__), schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.load_cache_from_db()
        self.refresh_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(os.path.dirname(__file__), schema_defaults(self.SETTINGS_SCHEMA))
        minutes = max(1, int(self.settings.get("member_refresh_minutes") or 10))
        self.refresh_task.change_interval(minutes=minutes)

    def cog_unload(self):
        self.refresh_task.cancel()

    def db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize_db(self):
        conn = self.db()
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL,
                position TEXT,
                days_in_faction INTEGER,
                is_in_oc INTEGER,
                status_state TEXT,
                status_description TEXT,
                last_action_status TEXT,
                last_action_at INTEGER,
                last_completed_crime_at INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        cols = {row[1] for row in c.execute("PRAGMA table_info(members)")}
        if "last_completed_crime_at" not in cols:
            c.execute("ALTER TABLE members ADD COLUMN last_completed_crime_at INTEGER")
        conn.commit()
        conn.close()

    def load_cache_from_db(self):
        conn = self.db()
        rows = conn.execute("SELECT * FROM members").fetchall()
        conn.close()
        by_id = {}
        by_name = {}
        last_completed = {}
        for row in rows:
            data = dict(row)
            data["id"] = data["user_id"]
            data["is_in_oc"] = bool(data.get("is_in_oc"))
            data["raw"] = {
                "id": data["user_id"],
                "name": data["name"],
                "position": data.get("position"),
                "days_in_faction": data.get("days_in_faction"),
                "is_in_oc": data["is_in_oc"],
                "status": {
                    "state": data.get("status_state"),
                    "description": data.get("status_description"),
                },
            }
            by_id[data["user_id"]] = data
            by_name[data["name_norm"]] = data
            if data.get("last_completed_crime_at"):
                last_completed[data["user_id"]] = data["last_completed_crime_at"]
        self.members_by_id = by_id
        self.members_by_name = by_name
        self.last_completed_by_user = last_completed

    def build_torn_url(self, base_url, extra_params=None):
        if not base_url:
            raise ValueError("Torn URL is not configured.")
        parsed = urlparse(base_url)
        existing = parse_qs(parsed.query, keep_blank_values=True)
        params = {k: v[-1] for k, v in existing.items() if v}
        if extra_params:
            params.update({k: str(v) for k, v in extra_params.items() if v is not None})
        if TORN_API_KEY:
            params["key"] = TORN_API_KEY
        return parsed._replace(query=urlencode(params)).geturl()

    async def fetch_json(self, url, timeout_seconds=20):
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout_seconds) as response:
                response.raise_for_status()
                return await response.json()

    async def fetch_paginated_crimes(self, cat, max_pages=MAX_COMPLETED_PAGES):
        offset = 0
        all_crimes = []
        pages = 0
        while pages < max_pages:
            url = self.build_torn_url(FACTION_CRIMES_URL, extra_params={"cat": cat, "offset": offset})
            data = await self.fetch_json(url)
            crimes = data.get("crimes", []) or []
            if crimes:
                all_crimes.extend(crimes)
            pages += 1
            links = (data.get("_metadata") or {}).get("links") or {}
            next_link = links.get("next")
            if not next_link:
                break
            next_params = parse_qs(urlparse(next_link).query)
            next_offset = next_params.get("offset", [None])[-1]
            try:
                next_offset_int = int(next_offset)
            except (TypeError, ValueError):
                break
            if next_offset_int == offset:
                break
            offset = next_offset_int
        return all_crimes

    def build_last_completed_map(self, crimes):
        latest = dict(self.last_completed_by_user)
        for crime in crimes:
            executed_at = crime.get("executed_at") or crime.get("ready_at")
            if not executed_at:
                continue
            try:
                executed_at_int = int(executed_at)
            except (TypeError, ValueError):
                continue
            for slot in crime.get("slots", []) or []:
                user = slot.get("user") or {}
                if user.get("id") is None:
                    continue
                try:
                    user_id = int(user["id"])
                except (TypeError, ValueError):
                    continue
                prev = latest.get(user_id)
                if prev is None or executed_at_int > prev:
                    latest[user_id] = executed_at_int
        return latest

    async def refresh_members(self):
        url = self.build_torn_url(FACTION_MEMBERS_URL)
        data = await self.fetch_json(url)
        members = data.get("members", []) or []
        by_id = {}
        by_name = {}
        now = utc_now_iso()
        conn = self.db()
        c = conn.cursor()
        seen = []
        for member in members:
            try:
                user_id = int(member.get("id"))
            except (TypeError, ValueError):
                continue
            name = member.get("name") or "Unknown"
            status = member.get("status") or {}
            last_action = member.get("last_action") or {}
            last_completed = self.last_completed_by_user.get(user_id)
            row = {
                "id": user_id,
                "user_id": user_id,
                "name": name,
                "name_norm": normalize_username(name),
                "position": str(member.get("position") or "").strip(),
                "days_in_faction": int(member.get("days_in_faction") or 0),
                "is_in_oc": bool(member.get("is_in_oc", False)),
                "status_state": str(status.get("state") or ""),
                "status_description": str(status.get("description") or ""),
                "last_action_status": str(last_action.get("status") or ""),
                "last_action_at": last_action.get("timestamp"),
                "last_completed_crime_at": last_completed,
                "raw": member,
                "updated_at": now,
            }
            by_id[user_id] = row
            by_name[row["name_norm"]] = row
            seen.append(user_id)
            c.execute(
                """
                INSERT INTO members (
                    user_id, name, name_norm, position, days_in_faction, is_in_oc,
                    status_state, status_description, last_action_status, last_action_at,
                    last_completed_crime_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name=excluded.name,
                    name_norm=excluded.name_norm,
                    position=excluded.position,
                    days_in_faction=excluded.days_in_faction,
                    is_in_oc=excluded.is_in_oc,
                    status_state=excluded.status_state,
                    status_description=excluded.status_description,
                    last_action_status=excluded.last_action_status,
                    last_action_at=excluded.last_action_at,
                    last_completed_crime_at=COALESCE(excluded.last_completed_crime_at, members.last_completed_crime_at),
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    name,
                    row["name_norm"],
                    row["position"],
                    row["days_in_faction"],
                    1 if row["is_in_oc"] else 0,
                    row["status_state"],
                    row["status_description"],
                    row["last_action_status"],
                    row["last_action_at"],
                    last_completed,
                    now,
                ),
            )
        if seen:
            placeholders = ",".join("?" * len(seen))
            c.execute(f"DELETE FROM members WHERE user_id NOT IN ({placeholders})", seen)
        conn.commit()
        conn.close()
        self.members_by_id = by_id
        self.members_by_name = by_name
        self.last_member_refresh = utc_now()
        print(f"[Roster] Refreshed {len(by_id)} members.")
        return len(by_id)

    async def refresh_crimes(self):
        available = await self.fetch_paginated_crimes("available", max_pages=5)
        completed = await self.fetch_paginated_crimes("completed", max_pages=MAX_COMPLETED_PAGES)
        self.active_crimes = available
        self.completed_crimes = completed
        self.last_completed_by_user = self.build_last_completed_map(completed)
        conn = self.db()
        for user_id, ts in self.last_completed_by_user.items():
            conn.execute(
                "UPDATE members SET last_completed_crime_at = ? WHERE user_id = ? AND (last_completed_crime_at IS NULL OR last_completed_crime_at < ?)",
                (ts, user_id, ts),
            )
            if user_id in self.members_by_id:
                self.members_by_id[user_id]["last_completed_crime_at"] = ts
        conn.commit()
        conn.close()
        self.last_crime_refresh = utc_now()
        print(f"[Roster] Crimes available={len(available)} completed={len(completed)}.")
        return available

    def all_members(self):
        return list(self.members_by_id.values())

    def get_member(self, user_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        return self.members_by_id.get(user_id)

    def get_member_by_name(self, username):
        return self.members_by_name.get(normalize_username(username))

    def known_names(self):
        return set(self.members_by_name.keys())

    async def wait_until_populated(self, timeout_seconds=180):
        """Block plugin loops until the first successful member refresh (or timeout)."""
        await self.bot.wait_until_ready()
        if self.members_by_id:
            return True
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if self.members_by_id:
                return True
            await asyncio.sleep(1)
        print("[Roster] Timed out waiting for member data.")
        return bool(self.members_by_id)

    def last_completed(self, user_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        member = self.members_by_id.get(user_id)
        if member and member.get("last_completed_crime_at"):
            return member["last_completed_crime_at"]
        return self.last_completed_by_user.get(user_id)

    @tasks.loop(minutes=MEMBER_REFRESH_MINUTES)
    async def refresh_task(self):
        try:
            await self.refresh_members()
            await self.refresh_crimes()
        except Exception as e:
            print(f"[Roster] Refresh failed: {e}")

    @refresh_task.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(FactionRoster(bot))
