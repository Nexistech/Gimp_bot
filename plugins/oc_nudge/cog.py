import datetime
import os
import sqlite3
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
try:
    from config import SPAM_CHANNEL_ID, FREELOADER_EXEMPT_IDS
except ImportError:
    SPAM_CHANNEL_ID = 0
    FREELOADER_EXEMPT_IDS = []

DB_NAME = os.path.join(os.path.dirname(__file__), "oc_nudge.db")
OC_NUDGE_DAYS = 3
CHECK_MINUTES = 60


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def format_time_ago(last_ts):
    if not last_ts:
        return "a long time ago"
    delta_seconds = int(utc_now().timestamp()) - last_ts
    days = delta_seconds // 86400
    if days < 1:
        hours = max(1, delta_seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = max(1, days // 365)
    return f"{years} year{'s' if years != 1 else ''} ago"


class OCNudgeMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.initialize_db()
        self.nudge_task.start()

    def cog_unload(self):
        self.nudge_task.cancel()

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
            print(f"[OC Nudge DB] Operation failed: {e}")
            return [] if fetch else None
        finally:
            conn.close()

    def initialize_db(self):
        self.db_operation(
            """
            CREATE TABLE IF NOT EXISTS oc_ignored_users (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            )
            """
        )
        self.db_operation(
            """
            CREATE TABLE IF NOT EXISTS oc_nudge_sent (
                user_id INTEGER PRIMARY KEY,
                last_completed_crime_at INTEGER,
                last_sent_at TEXT NOT NULL
            )
            """
        )

    def get_ignored_ids(self):
        rows = self.db_operation("SELECT user_id FROM oc_ignored_users", fetch=True)
        db_ignored = {row[0] for row in rows} if rows else set()
        return db_ignored.union(set(FREELOADER_EXEMPT_IDS))

    def already_nudged(self, user_id, last_ts):
        rows = self.db_operation(
            "SELECT last_completed_crime_at FROM oc_nudge_sent WHERE user_id = ?",
            (user_id,),
            fetch=True,
        )
        if not rows:
            return False
        return rows[0][0] == last_ts

    def record_nudge(self, user_id, last_ts):
        self.db_operation(
            """
            INSERT INTO oc_nudge_sent (user_id, last_completed_crime_at, last_sent_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_completed_crime_at=excluded.last_completed_crime_at,
                last_sent_at=excluded.last_sent_at
            """,
            (user_id, last_ts, utc_now().isoformat()),
        )

    def _member_names(self, member):
        names = []
        for raw in (
            getattr(member, "display_name", None),
            getattr(member, "name", None),
            getattr(member, "nick", None),
            getattr(member, "global_name", None),
        ):
            if raw:
                names.append(str(raw).strip().lower())
        return names

    def find_discord_member(self, guild, torn_username):
        if not guild:
            return None
        target_name = (torn_username or "").strip().lower()
        if not target_name:
            return None
        for member in guild.members:
            if target_name in self._member_names(member):
                return member
        for member in guild.members:
            display = (getattr(member, "display_name", None) or "").lower()
            if target_name in display:
                return member
        return None

    @tasks.loop(minutes=CHECK_MINUTES)
    async def nudge_task(self):
        try:
            await self._run_nudges()
        except Exception as e:
            print(f"[OC Nudge] Task failed: {e}")

    async def _run_nudges(self):
        roster = self.get_roster()
        if not roster or not roster.members_by_id:
            print("[OC Nudge] Roster empty; skipping.")
            return
        channel = self.bot.get_channel(SPAM_CHANNEL_ID)
        if not channel:
            print(f"[OC Nudge] SPAM_CHANNEL_ID ({SPAM_CHANNEL_ID}) not found.")
            return
        ignored_ids = self.get_ignored_ids()
        now_ts = int(utc_now().timestamp())
        threshold_ts = now_ts - (OC_NUDGE_DAYS * 86400)
        for member in roster.all_members():
            try:
                user_id = int(member.get("id") or member.get("user_id"))
            except (TypeError, ValueError):
                continue
            if user_id in ignored_ids:
                continue
            if bool(member.get("is_in_oc", False)):
                continue
            last_ts = roster.last_completed(user_id)
            if last_ts is not None and last_ts > threshold_ts:
                continue
            if self.already_nudged(user_id, last_ts):
                continue
            username = member.get("name", "Unknown")
            time_ago_str = format_time_ago(last_ts)
            profile_url = f"https://www.torn.com/profiles.php?XID={user_id}"
            discord_member = self.find_discord_member(channel.guild, username)
            embed = discord.Embed(
                title="Member OC Join Required",
                description=(
                    f"Faction member **[{username}]({profile_url})** "
                    f"was last in an OC **{time_ago_str}** and needs to join an organized crime."
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"Torn ID: {user_id}")
            content = discord_member.mention if discord_member else None
            try:
                await channel.send(content=content, embed=embed)
                self.record_nudge(user_id, last_ts)
            except discord.Forbidden:
                print(f"[OC Nudge] Forbidden sending to {SPAM_CHANNEL_ID}.")
                return

    @nudge_task.before_loop
    async def before_nudge(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()

    oc_group = app_commands.Group(name="oc_nudge", description="Manage OC Nudge exemptions")

    @oc_group.command(name="ignore", description="Exclude a user from OC nudge notifications.")
    async def ignore_user(self, interaction: discord.Interaction, user_id: int):
        self.db_operation(
            "INSERT OR REPLACE INTO oc_ignored_users (user_id, added_at) VALUES (?, ?)",
            (user_id, utc_now().isoformat()),
        )
        await interaction.response.send_message(
            f"✅ Torn ID `{user_id}` is now ignored for OC nudges.", ephemeral=True
        )

    @oc_group.command(name="unignore", description="Remove a user from the OC nudge ignore list.")
    async def unignore_user(self, interaction: discord.Interaction, user_id: int):
        self.db_operation("DELETE FROM oc_ignored_users WHERE user_id = ?", (user_id,))
        await interaction.response.send_message(
            f"✅ Torn ID `{user_id}` removed from the ignore list.", ephemeral=True
        )

    @oc_group.command(name="list_ignored", description="List all users ignored for OC nudges.")
    async def list_ignored(self, interaction: discord.Interaction):
        rows = self.db_operation("SELECT user_id FROM oc_ignored_users", fetch=True)
        db_ids = [str(r[0]) for r in rows] if rows else []
        config_ids = [str(i) for i in FREELOADER_EXEMPT_IDS]
        all_ignored = sorted(set(db_ids + config_ids))
        if not all_ignored:
            return await interaction.response.send_message("No users are currently ignored.", ephemeral=True)
        await interaction.response.send_message(
            "🚫 **Ignored Torn IDs:**\n" + ", ".join(all_ignored),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OCNudgeMonitor(bot))
