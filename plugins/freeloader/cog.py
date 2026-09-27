import datetime
import os
import sqlite3
import sys

import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
try:
    from config import ROLES_TO_TAG, FREELOADER_EXEMPT_IDS
except ImportError:
    ROLES_TO_TAG = []
    FREELOADER_EXEMPT_IDS = []
try:
    from config import FREELOADER_CHANNEL_ID
except ImportError:
    from config import SPAM_CHANNEL_ID as FREELOADER_CHANNEL_ID

DB_NAME = os.path.join(os.path.dirname(__file__), "freeloader_monitor.db")
FREELOADER_POSITION_NAME = "Freeloader"
FREELOADER_DAYS_IN_FACTION_MIN = 30
FREELOADER_IDLE_DAYS = 7


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def normalize_username(username):
    return " ".join(str(username).strip().split())


class FreeloaderMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.initialize_db()
        self.daily_freeloader_check.start()

    def cog_unload(self):
        self.daily_freeloader_check.cancel()

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def db_operation(self, query, params=(), fetch=False):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        try:
            c.execute(query, params)
            if fetch:
                return c.fetchall()
            conn.commit()
        except sqlite3.Error as e:
            print(f"[Freeloader DB] Operation failed: {e}")
            return [] if fetch else None
        finally:
            conn.close()

    def initialize_db(self):
        self.db_operation(
            """
            CREATE TABLE IF NOT EXISTS freeloader_notifications (
                user_id INTEGER PRIMARY KEY,
                game_username TEXT NOT NULL,
                last_completed_crime_at INTEGER,
                last_warning_type TEXT NOT NULL,
                last_warning_at TEXT NOT NULL
            )
            """
        )

    def already_notified(self, user_id, warning_type, reference_ts):
        rows = self.db_operation(
            "SELECT last_warning_type, last_completed_crime_at FROM freeloader_notifications WHERE user_id = ?",
            (user_id,),
            fetch=True,
        )
        if not rows:
            return False
        stored_type, stored_reference = rows[0]
        return stored_type == warning_type and stored_reference == reference_ts

    def record_notification(self, user_id, username, warning_type, reference_ts):
        self.db_operation(
            """
            INSERT INTO freeloader_notifications (user_id, game_username, last_completed_crime_at, last_warning_type, last_warning_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                game_username=excluded.game_username,
                last_completed_crime_at=excluded.last_completed_crime_at,
                last_warning_type=excluded.last_warning_type,
                last_warning_at=excluded.last_warning_at
            """,
            (user_id, normalize_username(username), reference_ts, warning_type, utc_now_iso()),
        )

    async def post_message(self, content):
        channel = self.bot.get_channel(FREELOADER_CHANNEL_ID)
        if channel is None:
            print("[Freeloader] Channel not found.")
            return False
        role_mentions = []
        if channel.guild:
            for role_name in ROLES_TO_TAG:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    role_mentions.append(role.mention)
        prefix = " ".join(role_mentions).strip()
        final_content = f"{prefix}\n{content}" if prefix else content
        try:
            await channel.send(final_content)
            return True
        except discord.Forbidden:
            return False

    @tasks.loop(hours=24.0)
    async def daily_freeloader_check(self):
        roster = self.get_roster()
        if not roster or not roster.members_by_id:
            print("[Freeloader] Roster empty; skipping check.")
            return
        try:
            now_ts = int(utc_now().timestamp())
            threshold_ts = now_ts - (FREELOADER_IDLE_DAYS * 24 * 60 * 60)
            for member in roster.all_members():
                user_id = member.get("id") or member.get("user_id")
                try:
                    user_id = int(user_id)
                except (TypeError, ValueError):
                    continue
                if user_id in FREELOADER_EXEMPT_IDS:
                    continue
                if int(member.get("days_in_faction") or 0) < FREELOADER_DAYS_IN_FACTION_MIN:
                    continue
                username = member.get("name", "Unknown")
                position = str(member.get("position") or "").strip()
                is_in_oc = bool(member.get("is_in_oc", False))
                last_completed_at = roster.last_completed(user_id)
                if position.lower() == FREELOADER_POSITION_NAME.lower() and is_in_oc:
                    if not self.already_notified(user_id, "restore_from_freeloader", last_completed_at):
                        msg = (
                            f"✅ **Freeloader Status Review**\n"
                            f"`{username}` [{user_id}] is ranked **{FREELOADER_POSITION_NAME}** but has joined an OC."
                        )
                        if await self.post_message(msg):
                            self.record_notification(user_id, username, "restore_from_freeloader", last_completed_at)
                    continue
                if is_in_oc:
                    continue
                if last_completed_at is None or last_completed_at <= threshold_ts:
                    if not self.already_notified(user_id, "move_to_freeloader", last_completed_at):
                        msg = (
                            f"⚠️ **Freeloader Review**\n"
                            f"`{username}` [{user_id}] has no recent OC participation. "
                            f"Review for **{FREELOADER_POSITION_NAME}**."
                        )
                        if await self.post_message(msg):
                            self.record_notification(user_id, username, "move_to_freeloader", last_completed_at)
        except Exception as e:
            print(f"[Freeloader] Daily check failed: {e}")

    @daily_freeloader_check.before_loop
    async def before_daily_freeloader_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(FreeloaderMonitor(bot))
