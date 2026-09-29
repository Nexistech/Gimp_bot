import asyncio
import datetime
import os
import sqlite3
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, save_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "verification.db")
LOCKOUT_HOURS = 24
TORN_DISCORD = "https://www.torn.com/discord"
OFFICIAL_VERIFY_HELP = (
    "This server does **not** run Torn verification.\n"
    "Link Discord on the **official Torn Discord**: {torn_discord}\n"
    "Torn stores that link. We only read it through the public API after you verify there.\n"
    "There is no OAuth page on this bot and we never see your Torn password or API key."
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class Verification(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "enabled",
            "type": "bool",
            "label": "Enable verification actions (leave off until roles are mapped)",
            "default": False,
        },
        {
            "key": "lobby_channel_id",
            "type": "int",
            "label": "Lobby / gate channel",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "log_channel_id",
            "type": "int",
            "label": "Verification log channel",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "verified_role",
            "type": "str",
            "label": "Verified Discord role",
            "default": "Verified",
            "widget": "discord_role",
        },
        {
            "key": "unverified_role",
            "type": "str",
            "label": "Unverified / lobby Discord role",
            "default": "Unverified",
            "widget": "discord_role",
        },
        {
            "key": "exclude_role",
            "type": "str",
            "label": "Skip verification entirely if they have this role",
            "default": "",
            "widget": "discord_role",
        },
        {
            "key": "admin_role",
            "type": "str",
            "label": "Admin role that can /verify others, use force, and /verifyall",
            "default": "",
            "widget": "discord_role",
        },
        {
            "key": "dry_run",
            "type": "bool",
            "label": "Test mode: do not change roles/nicks; report what would happen",
            "default": True,
        },
        {
            "key": "set_nickname",
            "type": "bool",
            "label": "Set Discord nickname to Torn name on verify",
            "default": True,
        },
        {
            "key": "daily_check",
            "type": "bool",
            "label": "Daily: strip mapped roles from people no longer in the faction",
            "default": False,
        },
        {
            "key": "recruit_message",
            "type": "str",
            "label": "Extra message sent to Recruit rank after verify",
            "default": "Welcome to the faction. You are a Recruit for 72 hours. Ask Concierge if you need help.",
        },
        {
            "key": "torn_discord_url",
            "type": "str",
            "label": "Official Torn Discord / verify URL",
            "default": TORN_DISCORD,
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        if "rank_roles" not in self.settings or not self.settings.get("rank_roles"):
            self.settings["rank_roles"] = {}
        if isinstance(self.settings.get("rank_roles"), str):
            import json
            try:
                self.settings["rank_roles"] = json.loads(self.settings["rank_roles"] or "{}")
            except json.JSONDecodeError:
                self.settings["rank_roles"] = {}
        self.initialize_db()
        self.daily_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        if isinstance(self.settings.get("rank_roles"), str):
            import json
            try:
                self.settings["rank_roles"] = json.loads(self.settings["rank_roles"] or "{}")
            except json.JSONDecodeError:
                self.settings["rank_roles"] = {}

    def cog_unload(self):
        self.daily_task.cancel()

    def is_enabled(self):
        return bool(self.settings.get("enabled"))

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def guild(self):
        root = load_root_settings()
        guild_id = int(root.get("DEV_GUILD_ID") or 0)
        if guild_id:
            return self.bot.get_guild(guild_id)
        return self.bot.guilds[0] if self.bot.guilds else None

    def role_by_name(self, guild, name):
        if not guild or not name:
            return None
        return discord.utils.get(guild.roles, name=str(name))

    def excluded(self, member):
        name = str(self.settings.get("exclude_role") or "").strip()
        if not name:
            return False
        return any(role.name == name for role in member.roles)

    def mapped_role_names(self):
        names = set()
        verified = str(self.settings.get("verified_role") or "").strip()
        unverified = str(self.settings.get("unverified_role") or "").strip()
        if verified:
            names.add(verified)
        if unverified:
            names.add(unverified)
        rank_map = self.settings.get("rank_roles") or {}
        if isinstance(rank_map, dict):
            for value in rank_map.values():
                if isinstance(value, list):
                    names.update(str(v) for v in value if v)
                elif value:
                    names.update(part.strip() for part in str(value).split(",") if part.strip())
        return names

    def roles_for_rank(self, guild, position):
        rank_map = self.settings.get("rank_roles") or {}
        raw = rank_map.get(position) or rank_map.get(str(position).lower()) or []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        roles = []
        for name in raw:
            role = self.role_by_name(guild, name)
            if role:
                roles.append(role)
        verified = self.role_by_name(guild, self.settings.get("verified_role"))
        if verified and verified not in roles:
            roles.append(verified)
        return roles

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_locks (
                discord_id INTEGER PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_result TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def is_admin(self, member):
        name = str(self.settings.get("admin_role") or "").strip()
        if not name:
            return False
        return any(role.name == name for role in getattr(member, "roles", []))

    def dry_run(self):
        return bool(self.settings.get("dry_run"))

    def lock_remaining(self, discord_id):
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute(
            "SELECT last_attempt_at FROM verify_locks WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            last = datetime.datetime.fromisoformat(row[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
        expires = last + datetime.timedelta(hours=LOCKOUT_HOURS)
        now = utc_now()
        if now >= expires:
            return None
        return expires - now

    def record_attempt(self, discord_id, result):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "INSERT OR REPLACE INTO verify_locks (discord_id, last_attempt_at, last_result) VALUES (?, ?, ?)",
            (int(discord_id), utc_now().isoformat(), result),
        )
        conn.commit()
        conn.close()

    def lockout_message(self):
        return (
            f"You already used `/verify` in the last {LOCKOUT_HOURS} hours.\n"
            "After you finish official Torn verification, ask an admin to run "
            "`/verify member:@you force:True`, or wait for the automated check."
        )

    async def lookup_torn_from_discord(self, discord_id):
        roster = self.get_roster()
        if roster:
            for member in roster.all_members():
                if str(member.get("discord_id") or "") == str(discord_id):
                    return {
                        "id": int(member.get("id") or member.get("user_id")),
                        "name": member.get("name"),
                        "position": member.get("position") or "",
                        "in_faction": True,
                    }
        if not TORN_API_KEY:
            return None
        url = f"https://api.torn.com/user/{int(discord_id)}?selections=discord,profile,basic&key={TORN_API_KEY}&comment=StrikeBot"
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                data = await response.json()
        if data.get("error"):
            return None
        payload = data.get("discord") or {}
        torn_id = payload.get("userID") or payload.get("userId") or data.get("player_id") or data.get("id")
        if not torn_id:
            return None
        torn_id = int(torn_id)
        name = data.get("name") or payload.get("name")
        in_faction = False
        position = ""
        if roster:
            member = roster.get_member(torn_id)
            if member:
                in_faction = True
                position = member.get("position") or ""
                name = member.get("name") or name
        return {"id": torn_id, "name": name, "position": position, "in_faction": in_faction}

    async def log(self, text):
        channel_id = int(self.settings.get("log_channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    def plan_verified(self, member, torn):
        guild = member.guild
        keep = self.roles_for_rank(guild, torn.get("position") or "")
        mapped = {self.role_by_name(guild, name) for name in self.mapped_role_names()}
        mapped.discard(None)
        unverified = self.role_by_name(guild, self.settings.get("unverified_role"))
        to_remove = [role for role in member.roles if role in mapped and role not in keep]
        if unverified and unverified in member.roles and unverified not in to_remove:
            to_remove.append(unverified)
        to_add = [role for role in keep if role not in member.roles]
        nick = None
        if self.settings.get("set_nickname") and torn.get("name"):
            wanted = str(torn["name"])[:32]
            if member.nick != wanted and member.display_name != wanted:
                nick = wanted
        recruit = str(torn.get("position") or "").lower() == "recruit"
        return {
            "add": to_add,
            "remove": to_remove,
            "nick": nick,
            "recruit": recruit,
            "reason": f"Verified as {torn.get('name')} [{torn.get('id')}]",
        }

    def describe_plan(self, member, torn, plan, kind="verify"):
        lines = [
            f"{member.mention} (`{member.id}`)",
            f"Torn: `{torn.get('name')}` [{torn.get('id')}] rank `{torn.get('position') or 'none'}` ({kind})",
        ]
        if plan["add"]:
            lines.append("Add roles: " + ", ".join(role.name for role in plan["add"]))
        if plan["remove"]:
            lines.append("Remove roles: " + ", ".join(role.name for role in plan["remove"]))
        if plan["nick"]:
            lines.append(f"Nickname → `{plan['nick']}`")
        if plan["recruit"]:
            lines.append("Would send recruit message")
        if not plan["add"] and not plan["remove"] and not plan["nick"]:
            lines.append("No role/nick changes needed")
        return "\n".join(lines)

    async def apply_verified(self, member, torn):
        plan = self.plan_verified(member, torn)
        if self.dry_run():
            return plan
        reason = plan["reason"]
        if plan["remove"]:
            try:
                await member.remove_roles(*plan["remove"], reason=reason)
            except discord.HTTPException as exc:
                print(f"[Verify] remove_roles failed: {exc}")
        if plan["add"]:
            try:
                await member.add_roles(*plan["add"], reason=reason)
            except discord.HTTPException as exc:
                print(f"[Verify] add_roles failed: {exc}")
        if plan["nick"]:
            try:
                await member.edit(nick=plan["nick"], reason=reason)
            except discord.HTTPException:
                pass
        await self.log(
            f"Verified {member.mention} as `{torn.get('name')}` [{torn.get('id')}] rank `{torn.get('position') or 'unknown'}`."
        )
        if plan["recruit"]:
            msg = str(self.settings.get("recruit_message") or "").strip()
            if msg:
                try:
                    await member.send(msg)
                except discord.HTTPException:
                    lobby_id = int(self.settings.get("lobby_channel_id") or 0)
                    lobby = member.guild.get_channel(lobby_id) if lobby_id else None
                    if lobby:
                        await lobby.send(f"{member.mention} {msg}")
        return plan

    def plan_unverified(self, member):
        guild = member.guild
        mapped = [self.role_by_name(guild, name) for name in self.mapped_role_names()]
        mapped = [role for role in mapped if role]
        unverified = self.role_by_name(guild, self.settings.get("unverified_role"))
        to_remove = [role for role in member.roles if role in mapped and role != unverified]
        to_add = []
        if unverified and unverified not in member.roles:
            to_add.append(unverified)
        return {"add": to_add, "remove": to_remove, "nick": None, "recruit": False}

    async def mark_unverified(self, member, reason="Not verified"):
        if self.excluded(member) or member.bot:
            return None
        plan = self.plan_unverified(member)
        if self.dry_run():
            return plan
        if plan["remove"]:
            try:
                await member.remove_roles(*plan["remove"], reason=reason)
            except discord.HTTPException:
                pass
        if plan["add"]:
            try:
                await member.add_roles(*plan["add"], reason=reason)
            except discord.HTTPException:
                pass
        return plan

    def instructions(self):
        url = self.settings.get("torn_discord_url") or TORN_DISCORD
        return OFFICIAL_VERIFY_HELP.format(torn_discord=url)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not self.is_enabled():
            return
        if self.excluded(member):
            return
        torn = await self.lookup_torn_from_discord(member.id)
        if torn and torn.get("in_faction"):
            plan = await self.apply_verified(member, torn)
            if self.dry_run():
                await self.log("DRY RUN join:\n" + self.describe_plan(member, torn, plan, "join-verify"))
            return
        plan = await self.mark_unverified(member, "Joined; not on faction roster")
        if self.dry_run() and plan:
            await self.log(
                "DRY RUN join unverified:\n"
                + f"{member.mention} would lose "
                + (", ".join(r.name for r in plan['remove']) or "nothing")
                + " and gain "
                + (", ".join(r.name for r in plan['add']) or "nothing")
            )
        lobby_id = int(self.settings.get("lobby_channel_id") or 0)
        lobby = member.guild.get_channel(lobby_id) if lobby_id else None
        text = (
            f"{member.mention} Welcome. You are gated here until Torn verification is visible to the API.\n\n"
            + self.instructions()
        )
        if torn and not torn.get("in_faction"):
            text += f"\nYour Discord is linked to Torn `{torn.get('name')}` [{torn.get('id')}], but that account is not in this faction."
        if lobby:
            await lobby.send(text)
        else:
            try:
                await member.send(text)
            except discord.HTTPException:
                pass

    @app_commands.command(name="verify", description="Verify yourself against Torn. Admins can verify others with force.")
    @app_commands.describe(
        member="Admin only: Discord member to verify",
        force="Admin only: bypass the 24 hour lockout",
    )
    async def verify_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None,
        force: bool = False,
    ):
        actor = interaction.user
        target = member or actor
        admin = self.is_admin(actor)
        if member and member.id != actor.id and not admin:
            return await interaction.response.send_message(
                "You can only `/verify` yourself. An admin has to verify someone else.",
                ephemeral=True,
            )
        if force and not admin:
            return await interaction.response.send_message("Only admins can use force.", ephemeral=True)
        if not self.is_enabled():
            return await interaction.response.send_message(
                "Verification is disabled until setup is finished.", ephemeral=True
            )
        if self.excluded(target):
            return await interaction.response.send_message(
                "That member is on the verification exclude role.", ephemeral=True
            )
        remaining = self.lock_remaining(target.id)
        if remaining and not (admin and force):
            hours = max(1, int(remaining.total_seconds() // 3600))
            if admin and target.id != actor.id:
                return await interaction.response.send_message(
                    f"{target.mention} is locked out for about {hours} more hour(s). "
                    "Run `/verify member:@them force:True` to override.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(self.lockout_message(), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if target.id == actor.id or not (admin and force):
            self.record_attempt(target.id, "attempt")
        torn = await self.lookup_torn_from_discord(target.id)
        prefix = "**DRY RUN — no changes applied.**\n" if self.dry_run() else ""
        if not torn:
            self.record_attempt(target.id, "not_linked")
            return await interaction.followup.send(
                prefix + f"{target.mention} is not linked on Torn yet.\n\n{self.instructions()}",
                ephemeral=True,
            )
        if not torn.get("in_faction"):
            plan = await self.mark_unverified(target, "Verified Torn account is not in faction")
            extra = ""
            if self.dry_run() and plan:
                extra = "\n" + self.describe_plan(target, torn, {**plan, "recruit": False}, "not-in-faction")
            return await interaction.followup.send(
                prefix
                + f"Torn account `{torn.get('name')}` [{torn.get('id')}] is linked, but they are not in this faction."
                + extra,
                ephemeral=True,
            )
        plan = await self.apply_verified(target, torn)
        self.record_attempt(target.id, "verified")
        if self.dry_run():
            return await interaction.followup.send(
                prefix + self.describe_plan(target, torn, plan),
                ephemeral=True,
            )
        await interaction.followup.send(
            f"Verified {target.mention} as `{torn.get('name')}` [{torn.get('id')}] ({torn.get('position') or 'no rank'}).",
            ephemeral=True,
        )

    @app_commands.command(name="verifyall", description="Admin: run a server-wide verification pass.")
    async def verifyall_command(self, interaction: discord.Interaction):
        if not self.is_admin(interaction.user):
            return await interaction.response.send_message("Only verification admins can run this.", ephemeral=True)
        if not self.is_enabled():
            return await interaction.response.send_message("Verification is disabled.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await interaction.followup.send("Run this in the server.", ephemeral=True)
        reports = []
        checked = 0
        for member in list(guild.members):
            if member.bot or self.excluded(member):
                continue
            checked += 1
            torn = await self.lookup_torn_from_discord(member.id)
            if torn and torn.get("in_faction"):
                plan = await self.apply_verified(member, torn)
                reports.append(self.describe_plan(member, torn, plan, "in-faction"))
            else:
                plan = await self.mark_unverified(member, "verifyall: not in faction")
                if plan and (plan["add"] or plan["remove"]):
                    fake = torn or {"name": "?", "id": "?", "position": ""}
                    reports.append(self.describe_plan(member, fake, {**plan, "recruit": False}, "strip"))
            await asyncio.sleep(0.35)
        header = "**DRY RUN — no changes applied.**\n" if self.dry_run() else ""
        body = header + f"Checked {checked} member(s).\n\n" + "\n\n".join(reports[:25])
        if len(reports) > 25:
            body += f"\n\n…and {len(reports) - 25} more."
        if len(body) > 1900:
            body = body[:1900] + "…"
        await interaction.followup.send(body or "Nothing to report.", ephemeral=True)

    @tasks.loop(time=datetime.time(hour=6, minute=0, tzinfo=datetime.timezone.utc))
    async def daily_task(self):
        if not self.is_enabled() or not self.settings.get("daily_check"):
            return
        guild = self.guild()
        roster = self.get_roster()
        if not guild or not roster:
            return
        in_faction = set()
        for member in roster.all_members():
            did = member.get("discord_id")
            if did:
                in_faction.add(int(did))
        for member in list(guild.members):
            if member.bot or self.excluded(member):
                continue
            if member.id in in_faction:
                continue
            await self.mark_unverified(member, "Daily check: not on faction roster")
            await asyncio.sleep(0.4)

    @daily_task.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()

    def web_tables(self):
        roster = self.get_roster()
        positions = set()
        if roster:
            for member in roster.all_members():
                pos = str(member.get("position") or "").strip()
                if pos:
                    positions.add(pos)
        for extra in ("Recruit", "Member", "Fluffer", "Talent", "Freeloader"):
            positions.add(extra)
        rank_map = self.settings.get("rank_roles") or {}
        rows = []
        for position in sorted(positions, key=str.lower):
            assigned = rank_map.get(position) or rank_map.get(position.lower()) or []
            if isinstance(assigned, str):
                assigned = [assigned]
            rows.append(
                {
                    "id": position,
                    "Faction rank": position,
                    "Discord roles": ", ".join(assigned),
                }
            )
        add_form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="set_rank_roles">
          <label>Faction rank</label>
          <div class="member-picker" data-field="position">
            <input type="hidden" name="position" id="position" value="">
            <div class="chips" id="chips-position"></div>
            <div class="search-wrap">
              <input type="text" class="member-search" data-field="position" data-source="ranks" data-mode="single"
                     placeholder="Search faction ranks" autocomplete="off">
              <div class="suggest" id="suggest-position"></div>
            </div>
          </div>
          <label>Discord roles</label>
          <div class="member-picker" data-field="roles">
            <input type="hidden" name="roles" id="roles" value="">
            <div class="chips" id="chips-roles"></div>
            <div class="search-wrap">
              <input type="text" class="member-search" data-field="roles" data-source="rolenames" data-mode="multi"
                     placeholder="Search Discord roles" autocomplete="off">
              <div class="suggest" id="suggest-roles"></div>
            </div>
          </div>
          <p class="help">Pick a rank, add one or more Discord roles, then save. Saving a rank with no roles clears it.</p>
          <button type="submit">Save rank mapping</button>
        </form>
        """
        return [
            {
                "title": "Faction rank → Discord roles",
                "columns": ["Faction rank", "Discord roles"],
                "rows": rows,
                "add_form": add_form,
            }
        ]

    def web_action(self, data):
        if data.get("action") != "set_rank_roles":
            return "Unknown action."
        position = str(data.get("position") or data.get("id") or "").strip()
        roles = [part.strip() for part in str(data.get("roles") or "").split(",") if part.strip()]
        if not position:
            return "Need a faction rank name."
        rank_map = dict(self.settings.get("rank_roles") or {})
        if roles:
            rank_map[position] = roles
        else:
            rank_map.pop(position, None)
        self.settings["rank_roles"] = rank_map
        save_settings(PLUGIN_DIR, self.settings)
        return f"Saved roles for {position}: {', '.join(roles) or '(none)'}."


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
