import datetime
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
try:
    from config import SPAM_CHANNEL_ID, ROLES_TO_TAG, ALLOWED_ROLE_NAME
except ImportError:
    SPAM_CHANNEL_ID = 0
    ROLES_TO_TAG = []
    ALLOWED_ROLE_NAME = "Concierge"

try:
    from plugins.strike_management.plugin import AUTO_STRIKE_DELAY_HOURS
except ImportError:
    AUTO_STRIKE_DELAY_HOURS = 4

CHECK_HOUR_UTC = 23
MAX_LINES = 12
MIN_DAYS_AFTER_RECRUIT = 3
FREELOADER_DAYS_IN_FACTION_MIN = 30
FREELOADER_IDLE_DAYS = 7


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class DailyDigest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.digest_task.start()

    def cog_unload(self):
        self.digest_task.cancel()

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def is_allowed_role(self, interaction: discord.Interaction):
        return any(role.name == ALLOWED_ROLE_NAME for role in interaction.user.roles)

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
                member_to_fluffer.append(line)
            elif pos_l == "fluffer" and in_oc:
                fluffer_to_talent.append(line)

            if (
                days >= FREELOADER_DAYS_IN_FACTION_MIN
                and not in_oc
                and pos_l != "freeloader"
                and (last_completed is None or last_completed <= idle_cut)
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

        return {
            "members": len(roster.members_by_id),
            "roster_age": roster.last_member_refresh,
            "member_to_fluffer": member_to_fluffer,
            "fluffer_to_talent": fluffer_to_talent,
            "idle": idle,
            "delayed_ocs": delayed_ocs,
        }

    def build_embed(self, data):
        embed = discord.Embed(
            title="Faction daily digest",
            description=(
                f"Roster size: **{data['members']}**\n"
                f"Last roster refresh: `{data['roster_age']}`"
            ),
            color=discord.Color.blurple(),
            timestamp=utc_now(),
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
        embed.set_footer(text="Info only — no automated action from this digest.")
        return embed

    async def post_digest(self, channel=None):
        data = self.collect_sections()
        if not data:
            return False, "Roster is empty."
        embed = self.build_embed(data)
        if channel is None:
            channel = self.bot.get_channel(SPAM_CHANNEL_ID)
        if channel is None:
            return False, "Spam channel not found."
        mentions = []
        if getattr(channel, "guild", None):
            for role_name in ROLES_TO_TAG:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        content = " ".join(mentions) if mentions else None
        await channel.send(content=content, embed=embed)
        return True, None

    @tasks.loop(time=datetime.time(hour=CHECK_HOUR_UTC, minute=0, tzinfo=datetime.timezone.utc))
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
            return await interaction.response.send_message(
                f"You need the **{ALLOWED_ROLE_NAME}** role to use this.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        ok, err = await self.post_digest()
        if ok:
            await interaction.followup.send("Digest posted to the spam channel.", ephemeral=True)
        else:
            await interaction.followup.send(f"Could not post digest: {err}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyDigest(bot))
