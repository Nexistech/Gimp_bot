import datetime
import sqlite3
import os
import sys
import discord
from discord import app_commands
from discord.ext import commands

# Import configuration safely
sys.path.append(os.getcwd())
try:
    from config import (
        SPAM_CHANNEL_ID,
        FREELOADER_EXEMPT_IDS
    )
except ImportError:
    SPAM_CHANNEL_ID = 0
    FREELOADER_EXEMPT_IDS = []

DB_NAME = os.path.join(os.path.dirname(__file__), "freeloader_monitor.db")
OC_NUDGE_DAYS = 3  # Alert if not in OC and last completion >= 3 days ago


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def format_time_ago(last_ts):
    if not last_ts:
        return "a long time ago"
    
    now_ts = int(utc_now().timestamp())
    delta_seconds = now_ts - last_ts
    days = delta_seconds // 86400

    if days < 1:
        hours = max(1, delta_seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    elif days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = max(1, days // 365)
        return f"{years} year{'s' if years != 1 else ''} ago"


class OCNudgeMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._original_fetch = None
        self.initialize_db()

    async def cog_load(self):
        # Wait until bot is ready to ensure FreeloaderMonitor is fully loaded
        self.bot.loop.create_task(self._attach_hook())

    def cog_unload(self):
        # Restore original fetch method if unloaded
        freeloader_cog = self.bot.get_cog("FreeloaderMonitor")
        if freeloader_cog and self._original_fetch:
            freeloader_cog.fetch_all_activity_pages = self._original_fetch

    async def _attach_hook(self):
        await self.bot.wait_until_ready()
        freeloader_cog = self.bot.get_cog("FreeloaderMonitor")
        if not freeloader_cog:
            print("[OC Nudge] Could not find FreeloaderMonitor cog to attach hook.")
            return

        if hasattr(freeloader_cog, "fetch_all_activity_pages"):
            self._original_fetch = freeloader_cog.fetch_all_activity_pages

            async def wrapped_fetch(*args, **kwargs):
                data = await self._original_fetch(*args, **kwargs)
                if data:
                    try:
                        await self.process_oc_nudges(data)
                    except Exception as e:
                        print(f"[OC Nudge] Error processing intercepted API data: {e}")
                return data

            freeloader_cog.fetch_all_activity_pages = wrapped_fetch
            print("[OC Nudge] Successfully attached hook to FreeloaderMonitor.")

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
            print(f"[OC Nudge DB] Operation failed: {e}")
            return [] if fetch else None
        finally:
            conn.close()

    def initialize_db(self):
        self.db_operation("""
            CREATE TABLE IF NOT EXISTS oc_ignored_users (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)

    def get_ignored_ids(self):
        rows = self.db_operation("SELECT user_id FROM oc_ignored_users", fetch=True)
        db_ignored = {row[0] for row in rows} if rows else set()
        return db_ignored.union(set(FREELOADER_EXEMPT_IDS))

    def find_discord_member(self, guild, torn_username):
        if not guild:
            return None

        target_name = torn_username.strip().lower()

        for member in guild.members:
            if member.display_name.strip().lower() == target_name or (
                member.global_name and member.global_name.strip().lower() == target_name
            ):
                return member

        for member in guild.members:
            if target_name in member.display_name.lower():
                return member

        return None

    async def process_oc_nudges(self, activity_data):
        """Runs automatically when FreeloaderMonitor finishes fetching data."""
        channel = self.bot.get_channel(SPAM_CHANNEL_ID)
        if not channel:
            print(f"[OC Nudge] SPAM_CHANNEL_ID ({SPAM_CHANNEL_ID}) not found.")
            return

        guild = channel.guild
        members = activity_data.get("members", [])
        crimes = activity_data.get("crimes", [])
        basic = activity_data.get("basic", {})
        faction_name = basic.get("name", "Faction")

        # Map user_id to last completed crime timestamp
        latest_by_user = {}
        for crime in crimes:
            executed_at = crime.get("executed_at")
            if not executed_at:
                continue
            for slot in crime.get("slots", []):
                user = slot.get("user")
                if not user or user.get("id") is None:
                    continue
                try:
                    uid = int(user["id"])
                    exec_ts = int(executed_at)
                except (TypeError, ValueError):
                    continue
                if uid not in latest_by_user or exec_ts > latest_by_user[uid]:
                    latest_by_user[uid] = exec_ts

        ignored_ids = self.get_ignored_ids()
        now_ts = int(utc_now().timestamp())
        threshold_ts = now_ts - (OC_NUDGE_DAYS * 86400)

        for member in members:
            try:
                user_id = int(member.get("id"))
            except (TypeError, ValueError):
                continue

            if user_id in ignored_ids:
                continue

            # Skip if currently in an OC
            if bool(member.get("is_in_oc", False)):
                continue

            username = member.get("name", "Unknown")
            last_ts = latest_by_user.get(user_id)

            # Check if last completed OC was >= 3 days ago or never completed
            if last_ts is None or last_ts <= threshold_ts:
                time_ago_str = format_time_ago(last_ts)
                profile_url = f"https://www.torn.com/profiles.php?XID={user_id}"
                discord_member = self.find_discord_member(guild, username)

                embed = discord.Embed(
                    title="Member OC Join Required",
                    description=(
                        f"{faction_name} member **[{username}]({profile_url})** "
                        f"was last in an OC **{time_ago_str}** and needs to join an organized crime."
                    ),
                    color=discord.Color.orange()
                )
                embed.set_footer(text=f"Torn ID: {user_id}")

                content = discord_member.mention if discord_member else None

                try:
                    await channel.send(content=content, embed=embed)
                except discord.Forbidden:
                    print(f"[OC Nudge] Failed to send embed to channel {SPAM_CHANNEL_ID}: Forbidden.")

    # --- Slash Commands for Ignore List ---
    oc_group = app_commands.Group(name="oc_nudge", description="Manage OC Nudge exemptions")

    @oc_group.command(name="ignore", description="Exclude a user from OC nudge notifications.")
    async def ignore_user(self, interaction: discord.Interaction, user_id: int):
        self.db_operation(
            "INSERT OR REPLACE INTO oc_ignored_users (user_id, added_at) VALUES (?, ?)",
            (user_id, utc_now().isoformat())
        )
        await interaction.response.send_message(f"✅ Torn ID `{user_id}` is now ignored for OC nudges.", ephemeral=True)

    @oc_group.command(name="unignore", description="Remove a user from the OC nudge ignore list.")
    async def unignore_user(self, interaction: discord.Interaction, user_id: int):
        self.db_operation("DELETE FROM oc_ignored_users WHERE user_id = ?", (user_id,))
        await interaction.response.send_message(f"✅ Torn ID `{user_id}` removed from the ignore list.", ephemeral=True)

    @oc_group.command(name="list_ignored", description="List all users ignored for OC nudges.")
    async def list_ignored(self, interaction: discord.Interaction):
        rows = self.db_operation("SELECT user_id FROM oc_ignored_users", fetch=True)
        db_ids = [str(r[0]) for r in rows] if rows else []
        config_ids = [str(i) for i in FREELOADER_EXEMPT_IDS]

        all_ignored = list(set(db_ids + config_ids))

        if not all_ignored:
            return await interaction.response.send_message("No users are currently ignored.", ephemeral=True)

        await interaction.response.send_message(
            f"🚫 **Ignored Torn IDs:**\n" + ", ".join(all_ignored),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OCNudgeMonitor(bot))
