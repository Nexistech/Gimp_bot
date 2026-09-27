import datetime
import sqlite3
import os
import sys
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp
import discord
from discord.ext import commands, tasks

# Ensure root directory is in path to import config
sys.path.append(os.getcwd())
try:
    from config import (
        SPAM_CHANNEL_ID, 
        ROLES_TO_TAG, 
        TORN_API_KEY, 
        FACTION_ACTIVITY_URL, 
        FREELOADER_EXEMPT_IDS
    )
except ImportError:
    SPAM_CHANNEL_ID = 0
    ROLES_TO_TAG = []
    TORN_API_KEY = ""
    FACTION_ACTIVITY_URL = "https://api.torn.com/v2/faction/basic,crimes,members?cat=available,completed&striptags=true"
    FREELOADER_EXEMPT_IDS = []

try:
    from config import FREELOADER_CHANNEL_ID
except ImportError:
    FREELOADER_CHANNEL_ID = SPAM_CHANNEL_ID

# Path to the specific database for this plugin
DB_NAME = os.path.join(os.path.dirname(__file__), "freeloader_monitor.db")
FREELOADER_POSITION_NAME = "Freeloader"
FREELOADER_DAYS_IN_FACTION_MIN = 30
FREELOADER_IDLE_DAYS = 7

def utc_now():
    return datetime.datetime.utcnow()

def utc_now_iso():
    return utc_now().isoformat()

def normalize_username(username):
    return " ".join(str(username).strip().split())

def build_torn_url(base_url, offset=None):
    if not base_url:
        raise ValueError("FACTION_ACTIVITY_URL is not configured.")
    parsed = urlparse(base_url)
    existing = parse_qs(parsed.query, keep_blank_values=True)
    params = {k: v[-1] for k, v in existing.items() if v}
    if offset is not None: params["offset"] = str(offset)
    if TORN_API_KEY: params["key"] = TORN_API_KEY
    query = urlencode(params)
    rebuilt = parsed._replace(query=query)
    return rebuilt.geturl()

class FreeloaderMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.initialize_db()
        self.daily_freeloader_check.start()

    def cog_unload(self):
        self.daily_freeloader_check.cancel()

    def db_operation(self, query, params=(), fetch=False):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        try:
            c.execute(query, params)
            if fetch:
                result = c.fetchall()
                conn.close()
                return result
            conn.commit()
        except sqlite3.Error as e:
            print(f"[Freeloader DB] Operation failed: {e}")
            return [] if fetch else None
        finally:
            conn.close()

    def initialize_db(self):
        self.db_operation("""
            CREATE TABLE IF NOT EXISTS freeloader_notifications (
                user_id INTEGER PRIMARY KEY,
                game_username TEXT NOT NULL,
                last_completed_crime_at INTEGER,
                last_warning_type TEXT NOT NULL,
                last_warning_at TEXT NOT NULL
            )
        """)

    async def fetch_json(self, url, timeout_seconds=20):
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout_seconds) as response:
                response.raise_for_status()
                return await response.json()

    async def fetch_all_activity_pages(self):
        offset = 0
        all_crimes = []
        members, basic = None, None
        
        while True:
            final_url = build_torn_url(FACTION_ACTIVITY_URL, offset=offset)
            data = await self.fetch_json(final_url)
            if members is None: members = data.get("members", [])
            if basic is None: basic = data.get("basic", {})
            crimes = data.get("crimes", [])
            if crimes: all_crimes.extend(crimes)
            
            metadata = data.get("_metadata", {})
            links = metadata.get("links", {})
            next_link = links.get("next")
            if not next_link: break

            parsed_next = urlparse(next_link)
            next_params = parse_qs(parsed_next.query)
            next_offset = next_params.get("offset", [None])[-1]
            if next_offset is None: break
            try:
                next_offset_int = int(next_offset)
            except (TypeError, ValueError): break
            if next_offset_int == offset: break
            offset = next_offset_int

        return {"basic": basic or {}, "members": members or [], "crimes": all_crimes}

    def build_last_completed_map(self, crimes):
        latest_by_user = {}
        for crime in crimes:
            executed_at = crime.get("executed_at")
            if not executed_at: continue
            for slot in crime.get("slots", []):
                user = slot.get("user")
                if not user or user.get("id") is None: continue
                try:
                    user_id = int(user["id"])
                    executed_at_int = int(executed_at)
                except (TypeError, ValueError): continue
                prev = latest_by_user.get(user_id)
                if prev is None or executed_at_int > prev:
                    latest_by_user[user_id] = executed_at_int
        return latest_by_user

    def already_notified(self, user_id, warning_type, reference_ts):
        rows = self.db_operation("SELECT last_warning_type, last_completed_crime_at FROM freeloader_notifications WHERE user_id = ?", (user_id,), fetch=True)
        if not rows: return False
        stored_type, stored_reference = rows[0]
        return stored_type == warning_type and stored_reference == reference_ts

    def record_notification(self, user_id, username, warning_type, reference_ts):
        self.db_operation("""
            INSERT INTO freeloader_notifications (user_id, game_username, last_completed_crime_at, last_warning_type, last_warning_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                game_username=excluded.game_username,
                last_completed_crime_at=excluded.last_completed_crime_at,
                last_warning_type=excluded.last_warning_type,
                last_warning_at=excluded.last_warning_at
        """, (user_id, normalize_username(username), reference_ts, warning_type, utc_now_iso()))

    async def post_message(self, content):
        channel = self.bot.get_channel(FREELOADER_CHANNEL_ID)
        if channel is None: return False
        role_mentions = []
        guild = channel.guild
        if guild:
            for role_name in ROLES_TO_TAG:
                role = discord.utils.get(guild.roles, name=role_name)
                if role: role_mentions.append(role.mention)
        prefix = " ".join(role_mentions).strip()
        final_content = f"{prefix}\n{content}" if prefix else content
        try:
            await channel.send(final_content)
            return True
        except discord.Forbidden:
            return False

    @tasks.loop(hours=24.0)
    async def daily_freeloader_check(self):
        try:
            activity = await self.fetch_all_activity_pages()
            members, crimes = activity.get("members", []), activity.get("crimes", [])
            last_completed_by_user = self.build_last_completed_map(crimes)
            now_ts = int(utc_now().timestamp())
            threshold_ts = now_ts - (FREELOADER_IDLE_DAYS * 24 * 60 * 60)

            for member in members:
                user_id = member.get("id")
                try: user_id = int(user_id)
                except: continue
                
                if user_id in FREELOADER_EXEMPT_IDS or int(member.get("days_in_faction", 0)) < FREELOADER_DAYS_IN_FACTION_MIN:
                    continue

                username = member.get("name", "Unknown")
                position = str(member.get("position", "")).strip()
                is_in_oc = bool(member.get("is_in_oc", False))
                last_completed_at = last_completed_by_user.get(user_id)

                if position.lower() == FREELOADER_POSITION_NAME.lower() and is_in_oc:
                    if not self.already_notified(user_id, "restore_from_freeloader", last_completed_at):
                        msg = f"✅ **Freeloader Status Review**\n`{username}` [{user_id}] is ranked **{FREELOADER_POSITION_NAME}** but has joined an OC."
                        if await self.post_message(msg):
                            self.record_notification(user_id, username, "restore_from_freeloader", last_completed_at)
                    continue

                if is_in_oc: continue

                if last_completed_at is None or last_completed_at <= threshold_ts:
                    if not self.already_notified(user_id, "move_to_freeloader", last_completed_at):
                        msg = f"⚠️ **Freeloader Review**\n`{username}` [{user_id}] has no recent OC participation. Review for **{FREELOADER_POSITION_NAME}**."
                        if await self.post_message(msg):
                            self.record_notification(user_id, username, "move_to_freeloader", last_completed_at)
        except Exception as e:
            print(f"[Freeloader] Daily check failed: {e}")

    @daily_freeloader_check.before_loop
    async def before_daily_freeloader_check(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(FreeloaderMonitor(bot))
