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
DB_NAME = os.path.join(PLUGIN_DIR, "chain_alert.db")
MILESTONES = [25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def schema():
    fields = [
        {
            "key": "channel_id",
            "type": "int",
            "label": "Alert channel (Disabled turns this off)",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "min_chain",
            "type": "int",
            "label": "Minimum current hits before any alert",
            "default": 25,
        },
        {
            "key": "check_seconds",
            "type": "int",
            "label": "Check interval (seconds)",
            "default": 30,
        },
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping",
            "default": [],
            "widget": "discord_roles",
        },
    ]
    defaults = {
        25: 60,
        50: 90,
        100: 120,
        250: 150,
        500: 180,
        1000: 240,
        2500: 300,
        5000: 360,
        10000: 420,
        25000: 480,
        50000: 540,
        100000: 600,
    }
    for mark in MILESTONES:
        fields.append(
            {
                "key": f"warn_{mark}",
                "type": "int",
                "label": f"{mark:,} hits — warn this many seconds before timeout",
                "default": defaults[mark],
            }
        )
    return fields


class ChainAlert(commands.Cog):
    SETTINGS_SCHEMA = schema()

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.check_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        seconds = max(15, int(self.settings.get("check_seconds") or 30))
        self.check_task.change_interval(seconds=seconds)

    def cog_unload(self):
        self.check_task.cancel()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chain_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                start INTEGER,
                last_alert_mark INTEGER,
                last_alert_at TEXT
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO chain_state (id) VALUES (1)")
        conn.commit()
        conn.close()

    def channel(self):
        channel_id = int(self.settings.get("channel_id") or 0)
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)

    def warn_seconds_for(self, current):
        mark = 0
        seconds = 0
        for milestone in MILESTONES:
            if current >= milestone:
                mark = milestone
                seconds = int(self.settings.get(f"warn_{milestone}") or 0)
        return mark, seconds

    async def fetch_chain(self):
        if not TORN_API_KEY:
            return None
        url = f"https://api.torn.com/faction/?selections=chain&key={TORN_API_KEY}&comment=StrikeBot"
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get("error"):
            print(f"[Chain] API error: {data['error']}")
            return None
        return data.get("chain") or data

    @tasks.loop(seconds=30)
    async def check_task(self):
        channel = self.channel()
        if not channel:
            return
        try:
            chain = await self.fetch_chain()
        except Exception as exc:
            print(f"[Chain] Fetch failed: {exc}")
            return
        if not chain:
            return
        current = int(chain.get("current") or 0)
        timeout = int(chain.get("timeout") or 0)
        start = int(chain.get("start") or 0)
        minimum = int(self.settings.get("min_chain") or 0)
        conn = sqlite3.connect(DB_NAME)
        state = conn.execute("SELECT * FROM chain_state WHERE id = 1").fetchone()
        last_start = state[1] if state else None
        last_mark = state[2] if state else None
        if current <= 0 or timeout <= 0:
            conn.execute("UPDATE chain_state SET start = NULL, last_alert_mark = NULL WHERE id = 1")
            conn.commit()
            conn.close()
            return
        if last_start != start:
            last_mark = None
            conn.execute(
                "UPDATE chain_state SET start = ?, last_alert_mark = NULL WHERE id = 1",
                (start,),
            )
            conn.commit()
        if current < minimum:
            conn.close()
            return
        mark, warn_in = self.warn_seconds_for(current)
        if not mark or warn_in <= 0:
            conn.close()
            return
        if timeout > warn_in:
            conn.close()
            return
        if last_mark == mark:
            conn.close()
            return
        conn.execute(
            "UPDATE chain_state SET last_alert_mark = ?, last_alert_at = ? WHERE id = 1",
            (mark, utc_now().isoformat()),
        )
        conn.commit()
        conn.close()
        minutes, seconds = divmod(timeout, 60)
        clock = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        embed = discord.Embed(
            title="Chain about to drop",
            description=(
                f"Current chain is **{current:,}** (using the **{mark:,}** warning).\n"
                f"**{clock}** left on the timer."
            ),
            color=discord.Color.red(),
            timestamp=utc_now(),
        )
        mentions = []
        if channel.guild:
            for role_name in self.settings.get("ping_roles") or load_root_settings().get("ROLES_TO_TAG") or []:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        try:
            await channel.send(content=" ".join(mentions) if mentions else None, embed=embed)
        except discord.HTTPException as exc:
            print(f"[Chain] Send failed: {exc}")

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ChainAlert(bot))
