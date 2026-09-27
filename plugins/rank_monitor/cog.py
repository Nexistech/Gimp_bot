import datetime
import os
import sqlite3
import sys

import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
try:
    from config import SPAM_CHANNEL_ID, ROLES_TO_TAG
except ImportError:
    SPAM_CHANNEL_ID = 0
    ROLES_TO_TAG = []

DB_NAME = os.path.join(os.path.dirname(__file__), "rank_monitor.db")
CHECK_MINUTES = 30
MIN_DAYS_AFTER_RECRUIT = 3
POSITION_RECRUIT = "recruit"
POSITION_MEMBER = "member"
POSITION_FLUFFER = "fluffer"
POSITION_TALENT = "talent"
POSITION_FREELOADER = "freeloader"
IGNORE_POSITIONS = {
    POSITION_TALENT,
    POSITION_FREELOADER,
}


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RankMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.initialize_db()
        self.rank_check_task.start()

    def cog_unload(self):
        self.rank_check_task.cancel()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rank_notifications (
                user_id INTEGER NOT NULL,
                suggested_rank TEXT NOT NULL,
                last_position TEXT,
                last_notified_at TEXT NOT NULL,
                PRIMARY KEY (user_id, suggested_rank)
            )
            """
        )
        conn.commit()
        conn.close()

    def already_notified(self, user_id, suggested_rank, current_position):
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute(
            "SELECT last_position FROM rank_notifications WHERE user_id = ? AND suggested_rank = ?",
            (user_id, suggested_rank),
        ).fetchone()
        conn.close()
        if not row:
            return False
        return row[0] == current_position

    def record_notification(self, user_id, suggested_rank, current_position):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            INSERT INTO rank_notifications (user_id, suggested_rank, last_position, last_notified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, suggested_rank) DO UPDATE SET
                last_position=excluded.last_position,
                last_notified_at=excluded.last_notified_at
            """,
            (user_id, suggested_rank, current_position, utc_now_iso()),
        )
        conn.commit()
        conn.close()

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    async def post_message(self, content):
        channel = self.bot.get_channel(SPAM_CHANNEL_ID)
        if channel is None:
            print("[RankMonitor] SPAM_CHANNEL_ID not found.")
            return False
        mentions = []
        if channel.guild:
            for role_name in ROLES_TO_TAG:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        prefix = " ".join(mentions).strip()
        text = f"{prefix}\n{content}" if prefix else content
        try:
            await channel.send(text)
            return True
        except discord.Forbidden:
            print("[RankMonitor] Forbidden sending to spam channel.")
            return False

    def suggested_rank_for(self, member):
        position = (member.get("position") or "").strip().lower()
        days = int(member.get("days_in_faction") or 0)
        in_oc = bool(member.get("is_in_oc"))

        if position in IGNORE_POSITIONS:
            return None
        if position == POSITION_RECRUIT or days < MIN_DAYS_AFTER_RECRUIT:
            return None
        if position == POSITION_FLUFFER:
            return "Talent" if in_oc else None
        if position == POSITION_MEMBER or position == "":
            return "Talent" if in_oc else "Fluffer"
        return None

    @tasks.loop(minutes=CHECK_MINUTES)
    async def rank_check_task(self):
        roster = self.get_roster()
        if not roster:
            print("[RankMonitor] FactionRoster cog not loaded yet.")
            return
        members = roster.all_members()
        if not members:
            return
        for member in members:
            user_id = member["id"]
            username = member["name"]
            position = member.get("position") or "Unknown"
            suggested = self.suggested_rank_for(member)
            if not suggested:
                continue
            if self.already_notified(user_id, suggested, position):
                continue
            profile = f"https://www.torn.com/profiles.php?XID={user_id}"
            if suggested == "Fluffer":
                msg = (
                    f"🏷️ **Rank Review**\n"
                    f"[`{username}`]({profile}) [{user_id}] is **{position}** "
                    f"and has not joined an OC. Move to **Fluffer**."
                )
            else:
                msg = (
                    f"🏷️ **Rank Review**\n"
                    f"[`{username}`]({profile}) [{user_id}] is **{position}** "
                    f"and has joined an OC. Move to **Talent**."
                )
            if await self.post_message(msg):
                self.record_notification(user_id, suggested, position)

    @rank_check_task.before_loop
    async def before_rank_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RankMonitor(bot))
