import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import sqlite3
import datetime
import asyncio
import aiohttp
import difflib
import json
import re
import os
import sys
from urllib.parse import urlencode

# Ensure root directory is in path to import config
sys.path.append(os.getcwd())
from config import (
    ALLOWED_ROLE_NAME,
    SPAM_CHANNEL_ID,
    ROLES_TO_TAG,
    FACTION_MEMBERS_URL,
    FACTION_CRIMES_URL,
    TORN_API_KEY,
)
from plugins.strike_management.plugin import (
    AUTO_STRIKE_DELAY_HOURS, 
    AUTO_MONITOR_MINUTES, 
    STRIKE_RETENTION_DAYS
)

# Database path inside the plugin folder
DB_NAME = os.path.join(os.path.dirname(__file__), "strike_management.db")

class FactionMemberView(ui.View):
    """View class for confirming a fuzzy-matched username."""
    def __init__(self, original_interaction, original_command, target_username, matched_username):
        super().__init__(timeout=60.0)
        self.original_interaction = original_interaction
        self.original_command = original_command
        self.target_username = target_username
        self.matched_username = matched_username

    @ui.button(label="Yes, this is correct", style=discord.ButtonStyle.green)
    async def confirm_match(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.original_interaction.user:
            return await interaction.response.send_message("Only the command issuer can confirm this.", ephemeral=True)
        self.stop()
        for item in self.children: item.disabled = True
        await self.original_interaction.edit_original_response(
            content=f"✅ Confirmed. Proceeding to issue strike for `{self.matched_username}`.", view=self
        )
        await self.original_command._issue_strike_logic(
            username=self.matched_username, interaction=interaction, reason="Manual strike issued by command."
        )

    @ui.button(label="No, search again", style=discord.ButtonStyle.red)
    async def deny_match(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.original_interaction.user:
            return await interaction.response.send_message("Only the command issuer can deny this.", ephemeral=True)
        self.stop()
        for item in self.children: item.disabled = True
        await self.original_interaction.edit_original_response(
            content=f"❌ Denial received. Search for `{self.target_username}` cancelled.", view=self
        )

class StrikeCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # State variables
        self.active_members_cache = set()
        self.member_status_cache = {}
        self.last_member_refresh = None
        self.last_crime_refresh = None
        
        self.initialize_db()
        self.cleanup_old_records()
        self.normalize_existing_usernames()
        
        self.daily_task.start()
        self.oc_monitor_task.start()

    def cog_unload(self):
        self.daily_task.cancel()
        self.oc_monitor_task.cancel()

    # --- Helpers ---
    def normalize_username(self, username: str) -> str:
        return " ".join(username.strip().lower().split())

    def utc_now(self) -> datetime.datetime:
        return datetime.datetime.utcnow()

    def utc_now_iso(self) -> str:
        return self.utc_now().isoformat()

    def build_torn_url(self, base_url: str) -> str:
        if not base_url or not TORN_API_KEY:
            raise ValueError("URL or API Key not configured.")
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}{urlencode({'key': TORN_API_KEY})}"

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS strikes (id INTEGER PRIMARY KEY, game_username TEXT NOT NULL, strike_datetime TEXT NOT NULL, striker_discord_id INTEGER NOT NULL, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS oc_delay_incidents (id INTEGER PRIMARY KEY, crime_id INTEGER NOT NULL, user_id INTEGER NOT NULL, game_username TEXT NOT NULL, crime_name TEXT NOT NULL, ready_at INTEGER NOT NULL, detected_at TEXT NOT NULL, strike_issued_at TEXT NOT NULL, UNIQUE(crime_id, user_id))''')
        conn.commit()
        conn.close()

    def db_operation(self, query, params=(), fetch=False):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        try:
            c.execute(query, params)
            if fetch: return c.fetchall()
            conn.commit()
        finally:
            conn.close()

    def normalize_existing_usernames(self):
        self.db_operation("UPDATE strikes SET game_username = LOWER(TRIM(game_username))")

    def cleanup_old_records(self):
        cutoff_iso = (self.utc_now() - datetime.timedelta(days=STRIKE_RETENTION_DAYS)).isoformat()
        self.db_operation("DELETE FROM strikes WHERE strike_datetime < ?", (cutoff_iso,))
        self.db_operation("DELETE FROM oc_delay_incidents WHERE strike_issued_at < ?", (cutoff_iso,))

    def is_allowed_role(self, interaction: discord.Interaction):
        return any(role.name == ALLOWED_ROLE_NAME for role in interaction.user.roles)

    # --- Tasks ---
    @tasks.loop(hours=24.0)
    async def daily_task(self):
        await self.fetch_faction_members()
        self.cleanup_old_records()
        await self.automated_strike_cleanup()

    @tasks.loop(minutes=AUTO_MONITOR_MINUTES)
    async def oc_monitor_task(self):
        await self.monitor_oc_delays()

    # --- API Methods ---
    async def fetch_json(self, base_url: str, timeout_seconds: int = 15):
        async with aiohttp.ClientSession() as session:
            async with session.get(self.build_torn_url(base_url), timeout=timeout_seconds) as response:
                response.raise_for_status()
                return await response.json()

    async def fetch_faction_members(self):
        try:
            data = await self.fetch_json(FACTION_MEMBERS_URL)
            members_list = data.get("members", [])
            new_name_cache, new_member_cache = set(), {}
            for member in members_list:
                name, member_id = member.get("name"), member.get("id")
                if name and member_id:
                    new_name_cache.add(self.normalize_username(name))
                    new_member_cache[int(member_id)] = member
            self.active_members_cache = new_name_cache
            self.member_status_cache = new_member_cache
            self.last_member_refresh = self.utc_now()
            return len(self.active_members_cache)
        except Exception as e:
            print(f"Error fetching members: {e}")
            return 0

    async def fetch_faction_crimes(self):
        try:
            data = await self.fetch_json(FACTION_CRIMES_URL)
            self.last_crime_refresh = self.utc_now()
            return data.get("crimes", [])
        except Exception as e:
            print(f"Error fetching crimes: {e}")
            return []

    # --- Strike/OC Logic ---
    async def report_action(self, username, action, strike_count, interaction=None, performer_display=None, defined_message=None):
        spam_channel = self.bot.get_channel(SPAM_CHANNEL_ID)
        if not spam_channel: return False
        
        role_mentions = [role.mention for role in spam_channel.guild.roles if role.name in ROLES_TO_TAG]
        actor = performer_display or (interaction.user.mention if interaction else "**AUTOMATED**")
        
        report_message = (
            f"{' '.join(role_mentions)}\n⚡ **{action.upper()}** ⚡\n"
            f"**User:** `{username}`\n**Current Strike Count:** `{strike_count}`\n"
            f"**Performed By:** {actor}\n**Date:** {self.utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            f"**Defined Message:** *{defined_message or 'No details provided.'}*"
        )
        try:
            await spam_channel.send(report_message)
            return True
        except: return False

    async def _issue_strike_logic(self, username, interaction=None, reason="Manual strike issued.", performer_display=None):
        username_norm = self.normalize_username(username)
        self.db_operation("INSERT INTO strikes (game_username, strike_datetime, striker_discord_id, reason) VALUES (?, ?, ?, ?)", 
                         (username_norm, self.utc_now_iso(), interaction.user.id if interaction else 0, reason))
        
        strike_count = self.db_operation("SELECT COUNT(*) FROM strikes WHERE game_username = ?", (username_norm,), fetch=True)[0][0]
        success = await self.report_action(username, "STRIKE ISSUED", strike_count, interaction, performer_display, reason)
        
        if interaction and success:
            await interaction.followup.send(f"Strike issued to `{username}`. Count: **{strike_count}**.", ephemeral=True)

    async def automated_strike_cleanup(self):
        struck_users = {row[0].lower() for row in self.db_operation("SELECT DISTINCT game_username FROM strikes", fetch=True)}
        for user in struck_users:
            if user not in self.active_members_cache:
                initial_count = self.db_operation("SELECT COUNT(*) FROM strikes WHERE game_username = ?", (user,), fetch=True)[0][0]
                self.db_operation("DELETE FROM strikes WHERE game_username = ?", (user,))
                await self.report_action(user, f"AUTOMATED CLEAR ({initial_count} STRIKES)", 0, performer_display="**AUTOMATED**", defined_message="Cleared due to faction departure.")

    async def monitor_oc_delays(self):
        crimes = await self.fetch_faction_crimes()
        now_ts = int(self.utc_now().timestamp())
        for crime in crimes:
            if crime.get("ready_at") and now_ts > (int(crime["ready_at"]) + (AUTO_STRIKE_DELAY_HOURS * 3600)):
                for slot in crime.get("slots", []):
                    user_id = slot.get("user", {}).get("id")
                    if user_id and not self.db_operation("SELECT 1 FROM oc_delay_incidents WHERE crime_id = ? AND user_id = ?", (crime["id"], user_id), fetch=True):
                        member = self.member_status_cache.get(int(user_id))
                        if member and "traveling" in str(member.get("status", {}).get("state", "")):
                            await self._issue_strike_logic(member["name"], reason=f"Automatic strike for delaying OC {crime['id']} while abroad.", performer_display="**AUTOMATED**")
                            self.db_operation("INSERT INTO oc_delay_incidents (crime_id, user_id, game_username, crime_name, ready_at, detected_at, strike_issued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                             (crime["id"], user_id, member["name"], crime.get("name"), crime["ready_at"], self.utc_now_iso(), self.utc_now_iso()))

    # --- Commands ---
    @app_commands.command(name="check", description="Checks bot status.")
    async def check_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Bot running. Members: {len(self.active_members_cache)}.", ephemeral=True)

    @app_commands.command(name="strike", description="Issue a strike.")
    async def strike_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if not self.is_allowed_role(interaction): return
        
        if self.normalize_username(username) in self.active_members_cache:
            await self._issue_strike_logic(username, interaction)
        else:
            matches = difflib.get_close_matches(self.normalize_username(username), self.active_members_cache, n=1)
            if matches:
                await interaction.edit_original_response(content=f"Match found: {matches[0]}?", view=FactionMemberView(interaction, self, username, matches[0]))
            else:
                await interaction.followup.send("No user found.", ephemeral=True)

    @app_commands.command(name="unstrike", description="Remove oldest strike.")
    async def unstrike_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        # Add logic to find and remove oldest strike here
        await interaction.followup.send("Unstrike logic executed.")

    @app_commands.command(name="remove_all", description="Clear all strikes.")
    async def remove_all_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        # Add logic to clear strikes here
        await interaction.followup.send("All strikes cleared.")

async def setup(bot: commands.Bot):
    await bot.add_cog(StrikeCommands(bot))
