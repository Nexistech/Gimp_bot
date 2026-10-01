"""Find high weav3r prices for an item.

Uses the OC tools item catalog when that plugin has already cached Torn item
names. Otherwise reads plugins/oc_tools/oc_tools.db, then falls back to one
Torn items API pull stored in this plugin's own database.
"""

import difflib
import os
import sqlite3
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

sys.path.append(os.getcwd())

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "find_trader.db")
OC_TOOLS_DB = os.path.join(os.getcwd(), "plugins", "oc_tools", "oc_tools.db")
WEAV3R_URL = "https://weav3r.dev/api/marketplace/{item_id}/traders"


class FindTrader(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.item_names = {}
        self.initialize_db()
        self.load_local_names()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS torn_items (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def load_local_names(self):
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute("SELECT item_id, name FROM torn_items").fetchall()
        conn.close()
        self.item_names = {int(item_id): name for item_id, name in rows}

    def store_names(self, mapping):
        conn = sqlite3.connect(DB_NAME)
        conn.executemany(
            "INSERT OR REPLACE INTO torn_items (item_id, name) VALUES (?, ?)",
            [(int(item_id), name) for item_id, name in mapping.items()],
        )
        conn.commit()
        conn.close()
        self.item_names.update(mapping)

    def catalog(self):
        tools = self.bot.get_cog("OCToolsMonitor")
        shared = getattr(tools, "item_names", None) if tools else None
        if shared:
            return dict(shared)
        if os.path.isfile(OC_TOOLS_DB):
            try:
                conn = sqlite3.connect(OC_TOOLS_DB)
                rows = conn.execute("SELECT item_id, name FROM torn_items").fetchall()
                conn.close()
                if rows:
                    return {int(item_id): name for item_id, name in rows}
            except sqlite3.Error:
                pass
        return dict(self.item_names)

    async def ensure_catalog(self):
        if self.catalog():
            return self.catalog()
        if not TORN_API_KEY:
            return {}
        url = f"https://api.torn.com/torn/?selections=items&key={TORN_API_KEY}&comment=GimpBot"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "GimpBot/1.0"}) as session:
                async with session.get(url, timeout=30) as response:
                    response.raise_for_status()
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            print(f"[FindTrader] Item catalog fetch failed: {exc}")
            return {}
        if data.get("error"):
            print(f"[FindTrader] Item catalog API error: {data['error']}")
            return {}
        mapping = {}
        for raw_id, info in (data.get("items") or {}).items():
            name = (info or {}).get("name") if isinstance(info, dict) else None
            if not name:
                continue
            try:
                mapping[int(raw_id)] = name
            except (TypeError, ValueError):
                continue
        if mapping:
            self.store_names(mapping)
        return dict(self.item_names)

    def match_item(self, query, catalog):
        text = " ".join(str(query or "").strip().split())
        if text.isdigit():
            item_id = int(text)
            if item_id in catalog:
                return item_id, catalog[item_id], 1.0
        lowered = text.lower()
        for item_id, name in catalog.items():
            if name.lower() == lowered:
                return item_id, name, 1.0
        contains = [(item_id, name) for item_id, name in catalog.items() if lowered and lowered in name.lower()]
        if len(contains) == 1:
            return contains[0][0], contains[0][1], 0.95
        by_name = {name.lower(): (item_id, name) for item_id, name in catalog.items()}
        close = difflib.get_close_matches(lowered, by_name.keys(), n=1, cutoff=0.55)
        if close:
            item_id, name = by_name[close[0]]
            score = difflib.SequenceMatcher(None, lowered, close[0]).ratio()
            return item_id, name, score
        if contains:
            contains.sort(key=lambda row: len(row[1]))
            return contains[0][0], contains[0][1], 0.7
        return None, None, 0.0

    def autocomplete_choices(self, current, catalog):
        query = " ".join(str(current or "").lower().split())
        if len(query) < 2 or not catalog:
            return []
        starts = []
        contains = []
        for item_id, name in catalog.items():
            lowered = name.lower()
            choice = app_commands.Choice(name=name[:100], value=str(item_id))
            if lowered.startswith(query):
                starts.append(choice)
            elif query in lowered:
                contains.append(choice)
        picks = starts + contains
        if not picks:
            names = {name.lower(): (item_id, name) for item_id, name in catalog.items()}
            for lowered in difflib.get_close_matches(query, names.keys(), n=10, cutoff=0.5):
                item_id, name = names[lowered]
                picks.append(app_commands.Choice(name=name[:100], value=str(item_id)))
        return picks[:25]

    async def fetch_traders(self, item_id, limit, hours):
        limit = max(1, min(25, int(limit)))
        hours = max(1, min(168, int(hours)))
        url = WEAV3R_URL.format(item_id=int(item_id))
        params = {"limit": limit, "sort": "price", "tradedWithinHours": hours}
        async with aiohttp.ClientSession(headers={"User-Agent": "GimpBot/1.0"}) as session:
            async with session.get(url, params=params, timeout=20) as response:
                response.raise_for_status()
                return await response.json()

    def build_embed(self, item_id, item_name, payload, matched_note, hours):
        traders = payload.get("traders") or []
        embed = discord.Embed(
            title=item_name,
            description=matched_note or f"Highest weav3r prices from traders active in the last {hours} hours.",
            color=discord.Color.green(),
        )
        if not traders:
            embed.description = f"No traders for **{item_name}** were active in the last {hours} hours."
            return embed
        lines = []
        for index, trader in enumerate(traders, start=1):
            player_id = trader.get("player_id")
            name = trader.get("player_name") or "Unknown"
            profile = f"https://www.torn.com/profiles.php?XID={player_id}"
            price = trader.get("price")
            price_text = f"${int(price):,}" if isinstance(price, int) else str(price)
            rating = trader.get("rating") or {}
            up = rating.get("upvotes", 0)
            down = rating.get("downvotes", 0)
            lines.append(
                f"**{index}. [{name}]({profile})**\n"
                f"Price `{price_text}` · rating {up} up / {down} down"
            )
        embed.description = (matched_note + "\n\n" if matched_note else "") + "\n\n".join(lines)
        embed.set_footer(text=f"Item {item_id} · {payload.get('total_count', len(traders))} traders on weav3r")
        return embed

    @app_commands.command(name="findtrader", description="Find recent high-price weav3r buyers for an item.")
    @app_commands.describe(
        item="Item name, such as Drug Pack or Erotic DVD",
        traders="How many traders to return",
        hours="Only include traders active within this many hours",
    )
    async def findtrader(
        self,
        interaction: discord.Interaction,
        item: str,
        traders: app_commands.Range[int, 1, 25] = 5,
        hours: app_commands.Range[int, 1, 168] = 2,
    ):
        await interaction.response.defer(ephemeral=True)
        catalog = await self.ensure_catalog()
        if not catalog:
            return await interaction.followup.send(
                "Item list is empty. Load the OC tools catalog or set TORN_API_KEY so this plugin can cache item names.",
                ephemeral=True,
            )
        item_id, item_name, score = self.match_item(item, catalog)
        if not item_id:
            return await interaction.followup.send(
                f"No item matched `{item}`. Try a shorter name, or pick one from the suggestions.",
                ephemeral=True,
            )
        try:
            payload = await self.fetch_traders(item_id, traders, hours)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return await interaction.followup.send(f"weav3r lookup failed: {exc}", ephemeral=True)
        note = ""
        if item.strip().lower() != item_name.lower() and not item.strip().isdigit():
            note = f"Matched **{item_name}** from `{item}`."
        await interaction.followup.send(
            embed=self.build_embed(item_id, item_name, payload, note, int(hours)),
            ephemeral=True,
        )

    @findtrader.autocomplete("item")
    async def findtrader_item(self, interaction: discord.Interaction, current: str):
        return self.autocomplete_choices(current, self.catalog() or self.item_names)


async def setup(bot: commands.Bot):
    await bot.add_cog(FindTrader(bot))
