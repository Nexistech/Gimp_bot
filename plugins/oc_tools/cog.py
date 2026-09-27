import datetime
import os
import sqlite3
import sys

import aiohttp
import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "oc_tools.db")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class OCToolsMonitor(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "hours_before_ready",
            "type": "int",
            "label": "Only alert when OC is this many hours from ready (0 = any time)",
            "default": 24,
            "help": "Tornium default is 24. Set 0 to alert as soon as someone is missing an item.",
        },
        {
            "key": "check_minutes",
            "type": "int",
            "label": "Check interval (minutes)",
            "default": 10,
        },
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping for missing tools",
            "default": [],
            "widget": "discord_roles",
            "help": "Leave empty to use the Bot page Roles to tag list.",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.item_names = {}
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
            CREATE TABLE IF NOT EXISTS missing_alerts (
                crime_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                last_alerted_at TEXT NOT NULL,
                PRIMARY KEY (crime_id, user_id, item_id)
            )
            """
        )
        conn.commit()
        conn.close()

    def already_alerted(self, crime_id, user_id, item_id):
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute(
            "SELECT 1 FROM missing_alerts WHERE crime_id = ? AND user_id = ? AND item_id = ?",
            (crime_id, user_id, item_id),
        ).fetchone()
        conn.close()
        return bool(row)

    def record_alert(self, crime_id, user_id, item_id):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            INSERT OR REPLACE INTO missing_alerts (crime_id, user_id, item_id, last_alerted_at)
            VALUES (?, ?, ?, ?)
            """,
            (crime_id, user_id, item_id, utc_now().isoformat()),
        )
        conn.commit()
        conn.close()

    async def ensure_item_names(self, needed_ids):
        missing = [item_id for item_id in needed_ids if item_id not in self.item_names]
        if not missing or not TORN_API_KEY:
            return
        url = f"https://api.torn.com/torn/?selections=items&key={TORN_API_KEY}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as response:
                    response.raise_for_status()
                    data = await response.json()
            items = data.get("items") or {}
            for raw_id, info in items.items():
                try:
                    self.item_names[int(raw_id)] = info.get("name") or f"Item {raw_id}"
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            print(f"[OC Tools] Item catalog fetch failed: {exc}")

    def item_label(self, req):
        item_id = req.get("id")
        name = req.get("name") or self.item_names.get(int(item_id) if item_id else 0)
        if not name:
            name = f"Item {item_id}" if item_id else "Unknown item"
        kind = "reusable tool" if req.get("is_reusable") else "consumable"
        return name, kind, item_id

    def within_ready_window(self, crime):
        hours = int(self.settings.get("hours_before_ready") or 0)
        if hours <= 0:
            return True
        ready_at = crime.get("ready_at")
        if not ready_at:
            return True
        try:
            ready_at = int(ready_at)
        except (TypeError, ValueError):
            return True
        now_ts = int(utc_now().timestamp())
        return ready_at - now_ts <= hours * 3600

    def collect_missing(self, crimes):
        found = []
        for crime in crimes or []:
            if not self.within_ready_window(crime):
                continue
            crime_id = crime.get("id")
            crime_name = crime.get("name") or "Organized crime"
            for slot in crime.get("slots") or []:
                user = slot.get("user") or {}
                req = slot.get("item_requirement") or slot.get("required_item")
                if not user or not req:
                    continue
                available = req.get("is_available")
                if available is not False and str(available).lower() not in {"false", "0"}:
                    continue
                try:
                    user_id = int(user.get("id"))
                    item_id = int(req.get("id") or 0)
                    crime_id_int = int(crime_id)
                except (TypeError, ValueError):
                    continue
                if not item_id:
                    continue
                found.append(
                    {
                        "crime_id": crime_id_int,
                        "crime_name": crime_name,
                        "ready_at": crime.get("ready_at"),
                        "user_id": user_id,
                        "user_name": user.get("name"),
                        "position": slot.get("position") or slot.get("position_id") or "",
                        "req": req,
                        "item_id": item_id,
                    }
                )
        return found

    async def post_alert(self, entry, item_name, kind):
        root = load_root_settings()
        channel = self.bot.get_channel(int(root.get("SPAM_CHANNEL_ID") or 0))
        if not channel:
            print("[OC Tools] Spam channel not found.")
            return False
        roster = self.get_roster()
        member = roster.get_member(entry["user_id"]) if roster else None
        username = entry.get("user_name") or (member.get("name") if member else "Unknown")
        profile = f"https://www.torn.com/profiles.php?XID={entry['user_id']}"
        armory_url = "https://www.torn.com/factions.php?step=your#/tab=armoury"
        ready_txt = "unknown"
        if entry.get("ready_at"):
            try:
                ready_dt = datetime.datetime.fromtimestamp(int(entry["ready_at"]), datetime.timezone.utc)
                ready_txt = ready_dt.strftime("%Y-%m-%d %H:%M UTC")
            except (TypeError, ValueError, OSError):
                pass
        embed = discord.Embed(
            title="OC item needs to be loaned",
            description=(
                f"**[{username}]({profile})** is missing a required {kind} for "
                f"**{entry['crime_name']}**."
            ),
            color=discord.Color.gold(),
            timestamp=utc_now(),
        )
        embed.add_field(name="Item", value=item_name, inline=True)
        embed.add_field(name="Type", value=kind, inline=True)
        if entry.get("position"):
            embed.add_field(name="Slot", value=str(entry["position"]), inline=True)
        embed.add_field(name="Ready at", value=ready_txt, inline=True)
        embed.add_field(name="Faction armory", value=f"[Open armory]({armory_url})", inline=False)
        embed.set_footer(text=f"Torn ID {entry['user_id']} · Crime {entry['crime_id']}")
        role_names = self.settings.get("ping_roles") or root.get("ROLES_TO_TAG") or []
        mentions = []
        if channel.guild:
            for role_name in role_names:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        try:
            await channel.send(content=" ".join(mentions) if mentions else None, embed=embed)
            return True
        except discord.Forbidden:
            print("[OC Tools] Forbidden sending missing-item alert.")
            return False

    @tasks.loop(minutes=10)
    async def check_task(self):
        roster = self.get_roster()
        if not roster:
            return
        crimes = roster.active_crimes or []
        missing = self.collect_missing(crimes)
        await self.ensure_item_names({row["item_id"] for row in missing})
        for entry in missing:
            if self.already_alerted(entry["crime_id"], entry["user_id"], entry["item_id"]):
                continue
            name, kind, _item_id = self.item_label(entry["req"])
            if await self.post_alert(entry, name, kind):
                self.record_alert(entry["crime_id"], entry["user_id"], entry["item_id"])

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster:
            await roster.wait_until_populated()


async def setup(bot: commands.Bot):
    await bot.add_cog(OCToolsMonitor(bot))
