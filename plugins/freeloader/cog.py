import datetime
import os
import sqlite3
import sys

import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

DB_NAME = os.path.join(os.path.dirname(__file__), "freeloader_monitor.db")
PLUGIN_DIR = os.path.dirname(__file__)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def normalize_username(username):
    return " ".join(str(username).strip().split())


class FreeloaderMonitor(commands.Cog):
    SETTINGS_SCHEMA = [
        {"key": "position_name", "type": "str", "label": "Freeloader rank name", "default": "Freeloader"},
        {"key": "days_in_faction_min", "type": "int", "label": "Minimum days in faction", "default": 30},
        {"key": "idle_days", "type": "int", "label": "Idle days before review", "default": 7},
        {"key": "exempt_ids", "type": "int_list", "label": "Exempt Torn IDs", "default": []},
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.daily_freeloader_check.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))

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
        root = load_root_settings()
        channel_id = int(root.get("FREELOADER_CHANNEL_ID") or root.get("SPAM_CHANNEL_ID") or 0)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            print("[Freeloader] Channel not found.")
            return False
        role_mentions = []
        if channel.guild:
            for role_name in root.get("ROLES_TO_TAG") or []:
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
            idle_days = int(self.settings.get("idle_days") or 7)
            min_days = int(self.settings.get("days_in_faction_min") or 30)
            position_name = str(self.settings.get("position_name") or "Freeloader")
            exempt = set(self.settings.get("exempt_ids") or [])
            threshold_ts = now_ts - (idle_days * 24 * 60 * 60)
            for member in roster.all_members():
                user_id = member.get("id") or member.get("user_id")
                try:
                    user_id = int(user_id)
                except (TypeError, ValueError):
                    continue
                if user_id in exempt:
                    continue
                if int(member.get("days_in_faction") or 0) < min_days:
                    continue
                username = member.get("name", "Unknown")
                position = str(member.get("position") or "").strip()
                is_in_oc = bool(member.get("is_in_oc", False))
                last_completed_at = roster.last_completed(user_id)
                if position.lower() == position_name.lower() and is_in_oc:
                    if not self.already_notified(user_id, "restore_from_freeloader", last_completed_at):
                        msg = (
                            f"✅ **Freeloader Status Review**\n"
                            f"`{username}` [{user_id}] is ranked **{position_name}** but has joined an OC."
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
                            f"Review for **{position_name}**."
                        )
                        if await self.post_message(msg):
                            self.record_notification(user_id, username, "move_to_freeloader", last_completed_at)
        except Exception as e:
            print(f"[Freeloader] Daily check failed: {e}")

    @daily_freeloader_check.before_loop
    async def before_daily_freeloader_check(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()


async def setup(bot: commands.Bot):
    await bot.add_cog(FreeloaderMonitor(bot))
