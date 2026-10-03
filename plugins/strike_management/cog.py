import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import sqlite3
import datetime
import difflib
import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

sys.path.append(os.getcwd())
try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""
FACTION_MEMBERS_URL = "https://api.torn.com/v2/faction/members"
FACTION_CRIMES_URL = "https://api.torn.com/v2/faction/crimes"
from plugin_settings import load_root_settings
from plugins.strike_management.plugin import (
    AUTO_STRIKE_DELAY_HOURS,
    AUTO_MONITOR_MINUTES,
    STRIKE_RETENTION_DAYS,
)

DB_NAME = os.path.join(os.path.dirname(__file__), "strike_management.db")
ABROAD_STATES = {"traveling", "abroad"}


class FactionMemberView(ui.View):
    def __init__(self, original_interaction, cog, target_username, matched_username):
        super().__init__(timeout=60.0)
        self.original_interaction = original_interaction
        self.cog = cog
        self.target_username = target_username
        self.matched_username = matched_username

    @ui.button(label="Yes, this is correct", style=discord.ButtonStyle.green)
    async def confirm_match(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.original_interaction.user:
            return await interaction.response.send_message(
                "Only the command issuer can confirm this.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        self.stop()
        for item in self.children:
            item.disabled = True
        await self.original_interaction.edit_original_response(
            content=f"✅ Confirmed. Proceeding to issue strike for `{self.matched_username}`.",
            view=self,
        )
        await self.cog._issue_strike_logic(
            username=self.matched_username,
            interaction=interaction,
            reason="Manual strike issued by command.",
        )

    @ui.button(label="No, search again", style=discord.ButtonStyle.red)
    async def deny_match(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.original_interaction.user:
            return await interaction.response.send_message(
                "Only the command issuer can deny this.", ephemeral=True
            )
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"❌ Denial received. Search for `{self.target_username}` cancelled.",
            view=self,
        )


class StrikeCommands(commands.Cog):
    SETTINGS_SCHEMA = []

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_members_cache = set()
        self.member_status_cache = {}
        self.last_member_refresh = None
        self.last_crime_refresh = None
        self.initialize_db()
        self.cleanup_old_records()
        self.normalize_existing_usernames()
        self.backfill_user_ids()
        self.daily_task.start()
        self.oc_monitor_task.start()

    def cog_unload(self):
        self.daily_task.cancel()
        self.oc_monitor_task.cancel()

    def normalize_username(self, username: str) -> str:
        return " ".join(username.strip().lower().split())

    def utc_now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def utc_now_iso(self) -> str:
        return self.utc_now().isoformat()

    def build_torn_url(self, base_url: str, extra_params=None) -> str:
        if not base_url or not TORN_API_KEY:
            raise ValueError("URL or API Key not configured.")
        parsed = urlparse(base_url)
        existing = parse_qs(parsed.query, keep_blank_values=True)
        params = {k: v[-1] for k, v in existing.items() if v}
        if extra_params:
            params.update({k: str(v) for k, v in extra_params.items() if v is not None})
        params["key"] = TORN_API_KEY
        return parsed._replace(query=urlencode(params)).geturl()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS strikes (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                game_username TEXT NOT NULL,
                strike_datetime TEXT NOT NULL,
                striker_discord_id INTEGER NOT NULL,
                reason TEXT
            )"""
        )
        cols = {row[1] for row in c.execute("PRAGMA table_info(strikes)")}
        if "user_id" not in cols:
            c.execute("ALTER TABLE strikes ADD COLUMN user_id INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_strikes_user_id ON strikes(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_strikes_username ON strikes(game_username)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS oc_delay_incidents (
                id INTEGER PRIMARY KEY,
                crime_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                game_username TEXT NOT NULL,
                crime_name TEXT NOT NULL,
                ready_at INTEGER NOT NULL,
                detected_at TEXT NOT NULL,
                strike_issued_at TEXT NOT NULL,
                UNIQUE(crime_id, user_id)
            )"""
        )
        conn.commit()
        conn.close()

    def db_operation(self, query, params=(), fetch=False):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        try:
            c.execute(query, params)
            if fetch:
                return c.fetchall()
            conn.commit()
        finally:
            conn.close()

    def normalize_existing_usernames(self):
        self.db_operation("UPDATE strikes SET game_username = LOWER(TRIM(game_username))")

    def resolve_user_id(self, username):
        roster = self.get_roster()
        if not roster:
            return None
        member = roster.get_member_by_name(username)
        if member:
            return int(member.get("id") or member.get("user_id"))
        return None

    def backfill_user_ids(self):
        roster = self.get_roster()
        if not roster or not roster.members_by_name:
            return
        rows = self.db_operation(
            "SELECT id, game_username FROM strikes WHERE user_id IS NULL", fetch=True
        ) or []
        for strike_id, name in rows:
            member = roster.get_member_by_name(name)
            if not member:
                continue
            uid = member.get("id") or member.get("user_id")
            self.db_operation("UPDATE strikes SET user_id = ? WHERE id = ?", (int(uid), strike_id))

    def strike_count_for(self, username=None, user_id=None):
        if user_id:
            row = self.db_operation(
                "SELECT COUNT(*) FROM strikes WHERE user_id = ?", (int(user_id),), fetch=True
            )
            if row and row[0][0]:
                return row[0][0]
        if username:
            return self.db_operation(
                "SELECT COUNT(*) FROM strikes WHERE game_username = ?",
                (self.normalize_username(username),),
                fetch=True,
            )[0][0]
        return 0

    def fetch_strike_rows(self, username=None, user_id=None):
        if user_id:
            rows = self.db_operation(
                """SELECT id, user_id, game_username, strike_datetime, striker_discord_id, reason
                   FROM strikes WHERE user_id = ? ORDER BY strike_datetime DESC""",
                (int(user_id),),
                fetch=True,
            )
            if rows:
                return rows
        if username:
            return self.db_operation(
                """SELECT id, user_id, game_username, strike_datetime, striker_discord_id, reason
                   FROM strikes WHERE game_username = ? ORDER BY strike_datetime DESC""",
                (self.normalize_username(username),),
                fetch=True,
            ) or []
        return []

    def cleanup_old_records(self):
        cutoff_iso = (self.utc_now() - datetime.timedelta(days=STRIKE_RETENTION_DAYS)).isoformat()
        self.db_operation("DELETE FROM strikes WHERE strike_datetime < ?", (cutoff_iso,))
        self.db_operation("DELETE FROM oc_delay_incidents WHERE strike_issued_at < ?", (cutoff_iso,))

    def allowed_role_name(self):
        return str(load_root_settings().get("ALLOWED_ROLE_NAME") or "Concierge")

    def is_allowed_role(self, interaction: discord.Interaction):
        allowed = self.allowed_role_name()
        return any(role.name == allowed for role in interaction.user.roles)

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def sync_member_cache_from_roster(self):
        roster = self.get_roster()
        if not roster or not roster.members_by_id:
            return False
        new_name_cache = set()
        new_member_cache = {}
        for user_id, row in roster.members_by_id.items():
            new_name_cache.add(row["name_norm"])
            raw = row.get("raw") or {
                "id": user_id,
                "name": row["name"],
                "position": row.get("position"),
                "days_in_faction": row.get("days_in_faction"),
                "is_in_oc": row.get("is_in_oc"),
                "status": {"state": row.get("status_state"), "description": row.get("status_description")},
            }
            new_member_cache[int(user_id)] = raw
        self.active_members_cache = new_name_cache
        self.member_status_cache = new_member_cache
        self.last_member_refresh = roster.last_member_refresh
        return True

    def member_is_abroad(self, member):
        state = str((member.get("status") or {}).get("state") or "").strip().lower()
        return state in ABROAD_STATES

    async def fetch_json(self, url: str, timeout_seconds: int = 15):
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout_seconds) as response:
                response.raise_for_status()
                return await response.json()

    async def fetch_faction_members(self):
        if self.sync_member_cache_from_roster():
            return len(self.active_members_cache)
        try:
            data = await self.fetch_json(self.build_torn_url(FACTION_MEMBERS_URL))
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
            print(f"[Strike] Loaded {len(new_name_cache)} members (direct API).")
            return len(self.active_members_cache)
        except Exception as e:
            print(f"[Strike] Error fetching members: {e}")
            return 0

    async def fetch_faction_crimes(self):
        roster = self.get_roster()
        if roster and roster.active_crimes:
            self.last_crime_refresh = roster.last_crime_refresh
            return roster.active_crimes
        try:
            data = await self.fetch_json(
                self.build_torn_url(FACTION_CRIMES_URL, extra_params={"cat": "available"})
            )
            self.last_crime_refresh = self.utc_now()
            crimes = data.get("crimes", []) or []
            print(f"[Strike] Loaded {len(crimes)} available crimes (direct API).")
            return crimes
        except Exception as e:
            print(f"[Strike] Error fetching crimes: {e}")
            return []

    async def report_action(
        self,
        username,
        action,
        strike_count,
        interaction=None,
        performer_display=None,
        defined_message=None,
    ):
        root = load_root_settings()
        spam_channel = self.bot.get_channel(int(root.get("SPAM_CHANNEL_ID") or 0))
        if not spam_channel:
            print("[Strike] SPAM_CHANNEL_ID not found.")
            return False
        tag_roles = set(root.get("ROLES_TO_TAG") or [])
        role_mentions = [
            role.mention for role in spam_channel.guild.roles if role.name in tag_roles
        ]
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
        except Exception as e:
            print(f"[Strike] Failed to report action: {e}")
            return False

    async def _issue_strike_logic(
        self,
        username,
        interaction=None,
        reason="Manual strike issued.",
        performer_display=None,
        user_id=None,
    ):
        username_norm = self.normalize_username(username)
        if user_id is None:
            user_id = self.resolve_user_id(username)
        self.db_operation(
            "INSERT INTO strikes (user_id, game_username, strike_datetime, striker_discord_id, reason) VALUES (?, ?, ?, ?, ?)",
            (
                int(user_id) if user_id else None,
                username_norm,
                self.utc_now_iso(),
                interaction.user.id if interaction else 0,
                reason,
            ),
        )
        strike_count = self.strike_count_for(username=username_norm, user_id=user_id)
        success = await self.report_action(
            username, "STRIKE ISSUED", strike_count, interaction, performer_display, reason
        )
        if interaction:
            try:
                await interaction.followup.send(
                    f"Strike issued to `{username}`. Count: **{strike_count}**.",
                    ephemeral=True,
                )
            except Exception as e:
                print(f"[Strike] followup failed: {e}")
        return success

    async def automated_strike_cleanup(self):
        rows = self.db_operation("SELECT DISTINCT game_username FROM strikes", fetch=True) or []
        struck_users = {row[0].lower() for row in rows}
        for user in struck_users:
            if user not in self.active_members_cache:
                initial_count = self.db_operation(
                    "SELECT COUNT(*) FROM strikes WHERE game_username = ?",
                    (user,),
                    fetch=True,
                )[0][0]
                self.db_operation("DELETE FROM strikes WHERE game_username = ?", (user,))
                await self.report_action(
                    user,
                    f"AUTOMATED CLEAR ({initial_count} STRIKES)",
                    0,
                    performer_display="**AUTOMATED**",
                    defined_message="Cleared due to faction departure.",
                )

    async def monitor_oc_delays(self):
        await self.fetch_faction_members()
        crimes = await self.fetch_faction_crimes()
        now_ts = int(self.utc_now().timestamp())
        delay_seconds = AUTO_STRIKE_DELAY_HOURS * 3600
        for crime in crimes:
            ready_at = crime.get("ready_at")
            crime_id = crime.get("id")
            if not ready_at or not crime_id:
                continue
            try:
                ready_at_int = int(ready_at)
            except (TypeError, ValueError):
                continue
            if now_ts <= ready_at_int + delay_seconds:
                continue
            status = str(crime.get("status") or "").strip().lower()
            if status != "planning":
                continue
            for slot in crime.get("slots", []) or []:
                user = slot.get("user") or {}
                user_id = user.get("id")
                if not user_id:
                    continue
                already = self.db_operation(
                    "SELECT 1 FROM oc_delay_incidents WHERE crime_id = ? AND user_id = ?",
                    (crime_id, user_id),
                    fetch=True,
                )
                if already:
                    continue
                member = self.member_status_cache.get(int(user_id))
                if not member:
                    print(f"[Strike] Delay on crime {crime_id} but member {user_id} not in cache.")
                    continue
                if not self.member_is_abroad(member):
                    continue
                reason = (
                    f"Automatic strike for delaying OC {crime_id} "
                    f"({crime.get('name', 'Unknown')}) while abroad/traveling."
                )
                await self._issue_strike_logic(
                    member.get("name", str(user_id)),
                    reason=reason,
                    performer_display="**AUTOMATED**",
                    user_id=int(user_id),
                )
                self.db_operation(
                    """INSERT OR IGNORE INTO oc_delay_incidents
                       (crime_id, user_id, game_username, crime_name, ready_at, detected_at, strike_issued_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        crime_id,
                        user_id,
                        member.get("name"),
                        crime.get("name") or "Unknown",
                        ready_at_int,
                        self.utc_now_iso(),
                        self.utc_now_iso(),
                    ),
                )

    @tasks.loop(hours=24.0)
    async def daily_task(self):
        await self.fetch_faction_members()
        self.cleanup_old_records()
        await self.automated_strike_cleanup()

    @tasks.loop(minutes=AUTO_MONITOR_MINUTES)
    async def oc_monitor_task(self):
        try:
            await self.monitor_oc_delays()
        except Exception as e:
            print(f"[Strike] OC monitor failed: {e}")

    async def _wait_for_roster(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()
        await self.fetch_faction_members()
        self.backfill_user_ids()

    @daily_task.before_loop
    async def before_daily(self):
        await self._wait_for_roster()

    @oc_monitor_task.before_loop
    async def before_oc_monitor(self):
        await self._wait_for_roster()

    @app_commands.command(name="strike", description="Issue a strike.")
    async def strike_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if not self.is_allowed_role(interaction):
            return await interaction.followup.send(
                f"You need the **{self.allowed_role_name()}** role to use this.", ephemeral=True
            )
        await self.fetch_faction_members()
        if self.normalize_username(username) in self.active_members_cache:
            await self._issue_strike_logic(username, interaction)
            return
        matches = difflib.get_close_matches(
            self.normalize_username(username), self.active_members_cache, n=1
        )
        if matches:
            view = FactionMemberView(interaction, self, username, matches[0])
            await interaction.followup.send(f"Match found: `{matches[0]}`?", view=view, ephemeral=True)
        else:
            await interaction.followup.send("No user found in the current faction roster.", ephemeral=True)

    @app_commands.command(name="unstrike", description="Remove oldest strike.")
    async def unstrike_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if not self.is_allowed_role(interaction):
            return await interaction.followup.send(
                f"You need the **{self.allowed_role_name()}** role to use this.", ephemeral=True
            )
        username_norm = self.normalize_username(username)
        user_id = self.resolve_user_id(username)
        if user_id:
            row = self.db_operation(
                "SELECT id FROM strikes WHERE user_id = ? ORDER BY strike_datetime ASC LIMIT 1",
                (int(user_id),),
                fetch=True,
            )
        else:
            row = None
        if not row:
            row = self.db_operation(
                "SELECT id FROM strikes WHERE game_username = ? ORDER BY strike_datetime ASC LIMIT 1",
                (username_norm,),
                fetch=True,
            )
        if not row:
            return await interaction.followup.send(f"No strikes found for `{username}`.", ephemeral=True)
        self.db_operation("DELETE FROM strikes WHERE id = ?", (row[0][0],))
        strike_count = self.strike_count_for(username=username_norm, user_id=user_id)
        await self.report_action(
            username,
            "STRIKE REMOVED",
            strike_count,
            interaction,
            defined_message="Oldest strike removed.",
        )
        await interaction.followup.send(
            f"Removed oldest strike from `{username}`. Remaining: **{strike_count}**.",
            ephemeral=True,
        )

    @app_commands.command(name="remove_all", description="Clear all strikes.")
    async def remove_all_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if not self.is_allowed_role(interaction):
            return await interaction.followup.send(
                f"You need the **{self.allowed_role_name()}** role to use this.", ephemeral=True
            )
        username_norm = self.normalize_username(username)
        user_id = self.resolve_user_id(username)
        count = self.strike_count_for(username=username_norm, user_id=user_id)
        if not count:
            return await interaction.followup.send(f"No strikes found for `{username}`.", ephemeral=True)
        if user_id:
            self.db_operation("DELETE FROM strikes WHERE user_id = ?", (int(user_id),))
        self.db_operation("DELETE FROM strikes WHERE game_username = ?", (username_norm,))
        await self.report_action(
            username,
            "ALL STRIKES CLEARED",
            0,
            interaction,
            defined_message=f"Cleared {count} strike(s).",
        )
        await interaction.followup.send(f"Cleared **{count}** strike(s) from `{username}`.", ephemeral=True)

    @app_commands.command(name="strikes", description="Show strike history for a faction member.")
    async def strikes_command(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        if not self.is_allowed_role(interaction):
            return await interaction.followup.send(
                f"You need the **{self.allowed_role_name()}** role to use this.", ephemeral=True
            )
        await self.fetch_faction_members()
        self.backfill_user_ids()
        user_id = None
        display_name = username
        if username.isdigit():
            user_id = int(username)
            roster = self.get_roster()
            member = roster.get_member(user_id) if roster else None
            if member:
                display_name = member.get("name", username)
        else:
            user_id = self.resolve_user_id(username)
            member = self.get_roster().get_member_by_name(username) if self.get_roster() else None
            if member:
                display_name = member.get("name", username)
                user_id = member.get("id") or member.get("user_id")
        rows = self.fetch_strike_rows(username=username, user_id=user_id)
        if not rows:
            return await interaction.followup.send(
                f"No strikes found for `{display_name}`.", ephemeral=True
            )
        lines = []
        for _id, uid, name, when, striker, reason in rows[:15]:
            who = f"<@{striker}>" if striker else "automated"
            tid = uid or "?"
            lines.append(f"• `{when}` — {reason or 'No reason'} (id `{tid}`, by {who})")
        extra = "" if len(rows) <= 15 else f"\n…and {len(rows) - 15} older strike(s)."
        await interaction.followup.send(
            f"**{display_name}** has **{len(rows)}** strike(s) in the last {STRIKE_RETENTION_DAYS} days.\n"
            + "\n".join(lines)
            + extra,
            ephemeral=True,
        )

    def web_tables(self):
        rows = self.db_operation(
            """SELECT id, user_id, game_username, strike_datetime, reason
               FROM strikes ORDER BY strike_datetime DESC LIMIT 200""",
            fetch=True,
        ) or []
        table_rows = []
        for strike_id, user_id, name, when, reason in rows:
            table_rows.append(
                {
                    "id": strike_id,
                    "When": when,
                    "Member": f"{name} ({user_id or '?'})",
                    "Reason": reason or "",
                }
            )
        add_form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="add_strike">
          <label>Torn ID</label>
          <input name="user_id" placeholder="Torn ID">
          <label>Username (used if ID is blank)</label>
          <input name="username" placeholder="In-game name">
          <label>Reason</label>
          <input name="reason" placeholder="Manual strike from web config">
          <button type="submit">Add strike</button>
        </form>
        """
        return [
            {
                "title": "Recent strikes",
                "columns": ["When", "Member", "Reason"],
                "rows": table_rows,
                "remove_action": "remove_strike",
                "add_form": add_form,
            }
        ]

    def web_action(self, data):
        action = data.get("action")
        if action == "remove_strike":
            strike_id = int(data.get("id"))
            row = self.db_operation(
                "SELECT game_username FROM strikes WHERE id = ?", (strike_id,), fetch=True
            )
            self.db_operation("DELETE FROM strikes WHERE id = ?", (strike_id,))
            name = row[0][0] if row else "unknown"
            return f"Removed strike #{strike_id} from {name}."
        if action == "add_strike":
            user_id = str(data.get("user_id") or "").strip()
            username = str(data.get("username") or "").strip()
            reason = str(data.get("reason") or "Manual strike from web config").strip()
            roster = self.get_roster()
            member = None
            if user_id.isdigit() and roster:
                member = roster.get_member(int(user_id))
            if not member and username and roster:
                member = roster.get_member_by_name(username)
            if member:
                username = member.get("name") or username
                user_id = member.get("id") or member.get("user_id")
            if not username and not user_id:
                return "Provide a Torn ID or username."
            self.db_operation(
                "INSERT INTO strikes (user_id, game_username, strike_datetime, striker_discord_id, reason) VALUES (?, ?, ?, ?, ?)",
                (
                    int(user_id) if str(user_id).isdigit() else None,
                    self.normalize_username(username or str(user_id)),
                    self.utc_now_iso(),
                    0,
                    reason,
                ),
            )
            return f"Added strike for {username or user_id}."
        return "Unknown action."


async def setup(bot: commands.Bot):
    await bot.add_cog(StrikeCommands(bot))
