import datetime
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

PLUGIN_DIR = os.path.dirname(__file__)

try:
    from plugins.strike_management.plugin import AUTO_STRIKE_DELAY_HOURS
except ImportError:
    AUTO_STRIKE_DELAY_HOURS = 4

MAX_LINES = 12
MIN_DAYS_AFTER_RECRUIT = 3
FREELOADER_DAYS_IN_FACTION_MIN = 30
FREELOADER_IDLE_DAYS = 7


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class DailyDigest(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "run_hour_utc",
            "type": "int",
            "label": "Digest hour (0-23, stored as UTC, not shown in the post)",
            "default": 16,
        },
        {
            "key": "run_minute_utc",
            "type": "int",
            "label": "Digest minute (0-59, stored as UTC, not shown in the post)",
            "default": 0,
        },
        {
            "key": "exclude_all_ids",
            "type": "int_list",
            "label": "Exclude from every digest section",
            "default": [],
            "widget": "torn_ids",
        },
        {
            "key": "exclude_member_to_fluffer_ids",
            "type": "int_list",
            "label": "Exclude from Member → Fluffer",
            "default": [],
            "widget": "torn_ids",
        },
        {
            "key": "exclude_fluffer_to_talent_ids",
            "type": "int_list",
            "label": "Exclude from Fluffer → Talent",
            "default": [],
            "widget": "torn_ids",
        },
        {
            "key": "exclude_idle_ids",
            "type": "int_list",
            "label": "Exclude from idle / activity",
            "default": [],
            "widget": "torn_ids",
        },
        {
            "key": "exclude_tools_ids",
            "type": "int_list",
            "label": "Exclude from missing OC tools",
            "default": [],
            "widget": "torn_ids",
        },
        {
            "key": "exclude_cpr_ids",
            "type": "int_list",
            "label": "Exclude from OC CPR",
            "default": [],
            "widget": "torn_ids",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.digest_task.change_interval(time=self.digest_time())
        self.digest_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.digest_task.change_interval(time=self.digest_time())

    def digest_time(self):
        hour = max(0, min(23, int(self.settings.get("run_hour_utc") or 16)))
        minute = max(0, min(59, int(self.settings.get("run_minute_utc") or 0)))
        return datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc)

    def cog_unload(self):
        self.digest_task.cancel()

    def excluded(self, user_id, key):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return False
        all_ids = {int(x) for x in (self.settings.get("exclude_all_ids") or []) if str(x).isdigit() or isinstance(x, int)}
        specific = {int(x) for x in (self.settings.get(key) or []) if str(x).isdigit() or isinstance(x, int)}
        return user_id in all_ids or user_id in specific

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def is_allowed_role(self, interaction: discord.Interaction):
        allowed = str(load_root_settings().get("ALLOWED_ROLE_NAME") or "Concierge")
        return any(role.name == allowed for role in interaction.user.roles)

    def format_list(self, items):
        if not items:
            return "_None_"
        shown = items[:MAX_LINES]
        extra = len(items) - len(shown)
        text = "\n".join(shown)
        if extra > 0:
            text += f"\n…and {extra} more"
        return text

    def collect_sections(self):
        roster = self.get_roster()
        if not roster or not roster.members_by_id:
            return None
        now_ts = int(utc_now().timestamp())
        idle_cut = now_ts - (FREELOADER_IDLE_DAYS * 86400)
        delay_cut = now_ts - (AUTO_STRIKE_DELAY_HOURS * 3600)

        member_to_fluffer = []
        fluffer_to_talent = []
        idle = []
        delayed_ocs = []

        for member in roster.all_members():
            user_id = member.get("id") or member.get("user_id")
            name = member.get("name", "Unknown")
            position = str(member.get("position") or "").strip()
            pos_l = position.lower()
            days = int(member.get("days_in_faction") or 0)
            in_oc = bool(member.get("is_in_oc"))
            last_completed = roster.last_completed(user_id)
            line = f"• `{name}` [{user_id}] — {position or 'No title'}, {days}d"

            if pos_l in {"talent", "freeloader", "recruit"}:
                pass
            elif days >= MIN_DAYS_AFTER_RECRUIT and pos_l in {"member", ""} and not in_oc:
                if not self.excluded(user_id, "exclude_member_to_fluffer_ids"):
                    member_to_fluffer.append(line)
            elif pos_l == "fluffer" and in_oc:
                if not self.excluded(user_id, "exclude_fluffer_to_talent_ids"):
                    fluffer_to_talent.append(line)

            if (
                days >= FREELOADER_DAYS_IN_FACTION_MIN
                and not in_oc
                and pos_l != "freeloader"
                and (last_completed is None or last_completed <= idle_cut)
                and not self.excluded(user_id, "exclude_idle_ids")
            ):
                idle.append(line)

        for crime in roster.active_crimes or []:
            ready_at = crime.get("ready_at")
            if not ready_at:
                continue
            try:
                ready_at = int(ready_at)
            except (TypeError, ValueError):
                continue
            if ready_at > delay_cut:
                continue
            hours = max(0, int((now_ts - ready_at) / 3600))
            delayed_ocs.append(
                f"• {crime.get('name', 'OC')} `{crime.get('id')}` ready ~{hours}h ago"
            )

        missing_tools = []
        tools_cog = self.bot.get_cog("OCToolsMonitor")
        if tools_cog:
            for entry in tools_cog.collect_missing(roster.active_crimes):
                if self.excluded(entry.get("user_id"), "exclude_tools_ids"):
                    continue
                name, kind, _item_id = tools_cog.item_label(entry["req"])
                who = entry.get("user_name") or entry.get("user_id")
                missing_tools.append(
                    f"• `{who}` missing {name} ({kind}) for {entry['crime_name']}"
                )

        cpr_issues = []
        cpr_cog = self.bot.get_cog("OCCPRMonitor")
        if cpr_cog:
            for entry in cpr_cog.collect_out_of_range(roster.active_crimes):
                offenders = [
                    p
                    for p in entry["offenders"]
                    if not self.excluded(p.get("user_id"), "exclude_cpr_ids")
                ]
                if not offenders:
                    continue
                names = ", ".join(
                    f"{p.get('user_name') or p['user_id']} {p['cpr']:g}"
                    for p in offenders
                )
                cpr_issues.append(
                    f"• {entry['crime_name']} L{entry['difficulty']} "
                    f"({entry['low']:g}–{entry['high']:g}): {names}"
                )

        return {
            "members": len(roster.members_by_id),
            "roster_age": roster.last_member_refresh,
            "member_to_fluffer": member_to_fluffer,
            "fluffer_to_talent": fluffer_to_talent,
            "idle": idle,
            "delayed_ocs": delayed_ocs,
            "missing_tools": missing_tools,
            "cpr_issues": cpr_issues,
        }

    def build_embed(self, data):
        embed = discord.Embed(
            title="Faction daily digest",
            description=f"Roster size: **{data['members']}**",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Member → Fluffer",
            value=self.format_list(data["member_to_fluffer"]),
            inline=False,
        )
        embed.add_field(
            name="Fluffer → Talent (in an OC)",
            value=self.format_list(data["fluffer_to_talent"]),
            inline=False,
        )
        embed.add_field(
            name=f"Idle {FREELOADER_IDLE_DAYS}+ days (not Freeloader)",
            value=self.format_list(data["idle"]),
            inline=False,
        )
        embed.add_field(
            name=f"OCs ready > {AUTO_STRIKE_DELAY_HOURS}h",
            value=self.format_list(data["delayed_ocs"]),
            inline=False,
        )
        embed.add_field(
            name="Missing OC tools / consumables",
            value=self.format_list(data.get("missing_tools") or []),
            inline=False,
        )
        embed.add_field(
            name="OC CPR out of range",
            value=self.format_list(data.get("cpr_issues") or []),
            inline=False,
        )
        embed.set_footer(text="Info only — no automated action from this digest.")
        return embed

    async def post_digest(self, channel=None, mention_roles=True):
        data = self.collect_sections()
        if not data:
            return False, "Roster is empty."
        embed = self.build_embed(data)
        root = load_root_settings()
        if channel is None:
            channel = self.bot.get_channel(int(root.get("SPAM_CHANNEL_ID") or 0))
        if channel is None:
            return False, "No channel available for the digest."
        mentions = []
        if mention_roles and getattr(channel, "guild", None):
            for role_name in root.get("ROLES_TO_TAG") or []:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        content = " ".join(mentions) if mentions else None
        await channel.send(content=content, embed=embed)
        return True, None

    @tasks.loop(time=datetime.time(hour=16, minute=0, tzinfo=datetime.timezone.utc))
    async def digest_task(self):
        try:
            ok, err = await self.post_digest()
            if not ok:
                print(f"[Digest] Skipped: {err}")
        except Exception as e:
            print(f"[Digest] Failed: {e}")

    @digest_task.before_loop
    async def before_digest(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()

    @app_commands.command(name="digest", description="Post the faction review digest now.")
    async def digest_command(self, interaction: discord.Interaction):
        if not self.is_allowed_role(interaction):
            allowed = str(load_root_settings().get("ALLOWED_ROLE_NAME") or "Concierge")
            return await interaction.response.send_message(
                f"You need the **{allowed}** role to use this.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        target = interaction.channel
        if target is None:
            return await interaction.followup.send(
                "I can't see the channel this was used in.", ephemeral=True
            )
        ok, err = await self.post_digest(channel=target, mention_roles=False)
        if ok:
            await interaction.followup.send("Digest posted here.", ephemeral=True)
        else:
            await interaction.followup.send(f"Could not post digest: {err}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyDigest(bot))
