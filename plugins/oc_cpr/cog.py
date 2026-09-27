import datetime
import os
import sqlite3
import sys

import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "oc_cpr.db")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def level_keys(level):
    return f"level_{level}_min", f"level_{level}_max"


def build_schema():
    fields = [
        {
            "key": "check_minutes",
            "type": "int",
            "label": "Check interval (minutes)",
            "default": 10,
        },
        {
            "key": "mention_discord",
            "type": "bool",
            "label": "Mention the Discord member when we can match their Torn name",
            "default": True,
        },
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping",
            "default": [],
            "widget": "discord_roles",
            "help": "Leave empty to use the Bot page Roles to tag list.",
        },
    ]
    for level in range(1, 11):
        fields.append(
            {
                "key": f"level_{level}_min",
                "type": "int",
                "label": f"Level {level} minimum CPR",
                "default": 0,
            }
        )
        fields.append(
            {
                "key": f"level_{level}_max",
                "type": "int",
                "label": f"Level {level} maximum CPR",
                "default": 100,
            }
        )
    return fields


class OCCPRMonitor(commands.Cog):
    SETTINGS_SCHEMA = build_schema()

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.check_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        minutes = max(1, int(self.settings.get("check_minutes") or 10))
        self.check_task.change_interval(minutes=minutes)

    def cog_unload(self):
        self.check_task.cancel()

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cpr_alerts (
                crime_id INTEGER PRIMARY KEY,
                last_alerted_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def already_alerted(self, crime_id):
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute("SELECT 1 FROM cpr_alerts WHERE crime_id = ?", (crime_id,)).fetchone()
        conn.close()
        return bool(row)

    def record_alert(self, crime_id):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "INSERT OR REPLACE INTO cpr_alerts (crime_id, last_alerted_at) VALUES (?, ?)",
            (crime_id, utc_now().isoformat()),
        )
        conn.commit()
        conn.close()

    def bounds_for(self, difficulty):
        try:
            level = int(difficulty)
        except (TypeError, ValueError):
            return None
        if level < 1 or level > 10:
            return None
        min_key, max_key = level_keys(level)
        try:
            low = int(self.settings.get(min_key))
            high = int(self.settings.get(max_key))
        except (TypeError, ValueError):
            return None
        return low, high

    def slot_cpr(self, slot):
        for key in ("checkpoint_pass_rate", "crime_pass_rate", "success_chance"):
            if slot.get(key) is not None:
                try:
                    return float(slot.get(key))
                except (TypeError, ValueError):
                    continue
        user = slot.get("user") or {}
        for key in ("checkpoint_pass_rate", "crime_pass_rate", "success_chance"):
            if user.get(key) is not None:
                try:
                    return float(user.get(key))
                except (TypeError, ValueError):
                    continue
        return None

    def collect_out_of_range(self, crimes=None):
        roster = self.get_roster()
        if crimes is None:
            crimes = (roster.active_crimes if roster else None) or []
        results = []
        for crime in crimes:
            try:
                crime_id = int(crime.get("id"))
            except (TypeError, ValueError):
                continue
            bounds = self.bounds_for(crime.get("difficulty"))
            if not bounds:
                continue
            low, high = bounds
            offenders = []
            for slot in crime.get("slots") or []:
                user = slot.get("user") or {}
                if not user.get("id"):
                    continue
                cpr = self.slot_cpr(slot)
                if cpr is None:
                    continue
                if cpr < low or cpr > high:
                    try:
                        user_id = int(user["id"])
                    except (TypeError, ValueError):
                        continue
                    offenders.append(
                        {
                            "user_id": user_id,
                            "user_name": user.get("name"),
                            "cpr": cpr,
                            "position": slot.get("position") or slot.get("position_id") or "",
                        }
                    )
            if offenders:
                results.append(
                    {
                        "crime_id": crime_id,
                        "crime_name": crime.get("name") or "Organized crime",
                        "difficulty": crime.get("difficulty"),
                        "low": low,
                        "high": high,
                        "offenders": offenders,
                    }
                )
        return results

    def find_discord_member(self, guild, torn_username):
        if not guild or not torn_username:
            return None
        target = torn_username.strip().lower()
        for member in guild.members:
            names = [
                getattr(member, "display_name", None),
                getattr(member, "name", None),
                getattr(member, "nick", None),
                getattr(member, "global_name", None),
            ]
            lowered = [str(n).strip().lower() for n in names if n]
            if target in lowered:
                return member
        return None

    async def post_alert(self, entry):
        root = load_root_settings()
        channel = self.bot.get_channel(int(root.get("SPAM_CHANNEL_ID") or 0))
        if not channel:
            print("[OC CPR] Spam channel not found.")
            return False
        roster = self.get_roster()
        lines = []
        mentions = []
        mention_discord = bool(self.settings.get("mention_discord"))
        for person in entry["offenders"]:
            member = roster.get_member(person["user_id"]) if roster else None
            name = person.get("user_name") or (member.get("name") if member else "Unknown")
            profile = f"https://www.torn.com/profiles.php?XID={person['user_id']}"
            extra = f" ({person['position']})" if person.get("position") else ""
            lines.append(f"• [{name}]({profile}){extra} — CPR **{person['cpr']:g}**")
            if mention_discord:
                discord_id = member.get("discord_id") if member else None
                if discord_id:
                    mentions.append(f"<@{discord_id}>")
                elif channel.guild:
                    discord_member = self.find_discord_member(channel.guild, name)
                    if discord_member:
                        mentions.append(discord_member.mention)
        embed = discord.Embed(
            title="OC CPR out of range",
            description=(
                f"**{entry['crime_name']}** (level {entry['difficulty']}) has "
                f"{len(entry['offenders'])} member(s) outside **{entry['low']:g}–{entry['high']:g}** CPR."
            ),
            color=discord.Color.orange(),
            timestamp=utc_now(),
        )
        embed.add_field(name="Members", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"Crime {entry['crime_id']}")
        role_names = self.settings.get("ping_roles") or root.get("ROLES_TO_TAG") or []
        if channel.guild:
            for role_name in role_names:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        # unique mentions, roles last
        seen = []
        for mention in mentions:
            if mention not in seen:
                seen.append(mention)
        try:
            await channel.send(content=" ".join(seen) if seen else None, embed=embed)
            return True
        except discord.Forbidden:
            print("[OC CPR] Forbidden sending CPR alert.")
            return False

    @tasks.loop(minutes=10)
    async def check_task(self):
        for entry in self.collect_out_of_range():
            if self.already_alerted(entry["crime_id"]):
                continue
            if await self.post_alert(entry):
                self.record_alert(entry["crime_id"])

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()


async def setup(bot: commands.Bot):
    await bot.add_cog(OCCPRMonitor(bot))
