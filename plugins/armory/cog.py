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
DB_NAME = os.path.join(PLUGIN_DIR, "armory.db")

# User-requested classes. API v1 faction selections use these names.
CATEGORIES = [
    ("temporary", "Temporary"),
    ("medical", "Medical"),
    ("drugs", "Drugs"),
    ("boosters", "Boosters"),
    ("utilities", "Utilities"),
]
# "consumable" is not a single Torn armory tab; medical/drugs/boosters cover it.
SELECTIONS = "temporary,medical,drugs,boosters,utilities"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class ArmoryTracker(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "channel_id",
            "type": "int",
            "label": "Alert channel ID (0 = Bot spam channel)",
            "default": 0,
        },
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping when stock is low",
            "default": [],
            "widget": "discord_roles",
        },
        {
            "key": "check_hour_utc",
            "type": "int",
            "label": "Daily check hour (UTC)",
            "default": 16,
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.check_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))

    def cog_unload(self):
        self.check_task.cancel()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                available INTEGER NOT NULL,
                loaned INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked (
                item_id INTEGER PRIMARY KEY,
                min_qty INTEGER NOT NULL,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn

    def on_hand(self, available, loaned, quantity):
        if available is not None:
            return max(0, int(available))
        qty = int(quantity or 0)
        loaned = int(loaned or 0)
        return max(0, qty - loaned)

    async def fetch_armory(self):
        if not TORN_API_KEY:
            raise RuntimeError("TORN_API_KEY missing")
        url = (
            f"https://api.torn.com/faction/?selections={SELECTIONS}"
            f"&key={TORN_API_KEY}&comment=StrikeBot"
        )
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=30) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        items = []
        for key, label in CATEGORIES:
            for raw in data.get(key) or []:
                item_id = raw.get("ID") or raw.get("id")
                if not item_id:
                    continue
                quantity = int(raw.get("quantity") or 0)
                loaned = int(raw.get("loaned") or 0)
                available = raw.get("available")
                available = int(available) if available is not None else self.on_hand(None, loaned, quantity)
                if quantity <= 0 and available <= 0 and loaned <= 0:
                    continue
                items.append(
                    {
                        "item_id": int(item_id),
                        "name": raw.get("name") or f"Item {item_id}",
                        "category": label,
                        "available": available,
                        "loaned": loaned,
                        "quantity": quantity,
                    }
                )
        return items

    def save_stock(self, items):
        now = utc_now().isoformat()
        conn = self.db()
        conn.execute("DELETE FROM stock")
        for item in items:
            conn.execute(
                """
                INSERT INTO stock (item_id, name, category, available, loaned, quantity, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"],
                    item["name"],
                    item["category"],
                    item["available"],
                    item["loaned"],
                    item["quantity"],
                    now,
                ),
            )
        conn.commit()
        conn.close()

    def low_stock(self):
        conn = self.db()
        rows = conn.execute(
            """
            SELECT t.item_id, t.min_qty, s.name, s.category, s.available, s.loaned, s.quantity
            FROM tracked t
            LEFT JOIN stock s ON s.item_id = t.item_id
            ORDER BY s.category, s.name
            """
        ).fetchall()
        conn.close()
        low = []
        for row in rows:
            available = int(row["available"] or 0)
            if available < int(row["min_qty"]):
                low.append(dict(row))
        return low

    async def post_low_stock(self, low):
        root = load_root_settings()
        channel_id = int(self.settings.get("channel_id") or 0) or int(root.get("SPAM_CHANNEL_ID") or 0)
        channel = self.bot.get_channel(channel_id)
        if not channel:
            print("[Armory] Alert channel not found.")
            return False
        by_cat = {}
        for row in low:
            by_cat.setdefault(row.get("category") or "Unknown", []).append(row)
        embed = discord.Embed(
            title="Armory stock below minimum",
            description=f"{len(low)} tracked item(s) are below the configured on-hand minimum. Loaned copies are not counted.",
            color=discord.Color.red(),
            timestamp=utc_now(),
        )
        for category, rows in by_cat.items():
            lines = [
                f"• {row.get('name') or row['item_id']}: **{int(row['available'] or 0)}** on hand / min **{row['min_qty']}**"
                for row in rows
            ]
            embed.add_field(name=category, value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="https://www.torn.com/factions.php?step=your#/tab=armoury")
        mentions = []
        role_names = self.settings.get("ping_roles") or root.get("ROLES_TO_TAG") or []
        if channel.guild:
            for role_name in role_names:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        await channel.send(content=" ".join(mentions) if mentions else None, embed=embed)
        return True

    async def run_check(self):
        items = await self.fetch_armory()
        self.save_stock(items)
        low = self.low_stock()
        if low:
            await self.post_low_stock(low)
        print(f"[Armory] Snapshot {len(items)} in-stock items, {len(low)} below minimum.")

    @tasks.loop(hours=1)
    async def check_task(self):
        try:
            hour = int(self.settings.get("check_hour_utc") or 16)
            if utc_now().hour != hour:
                return
            await self.run_check()
        except Exception as exc:
            print(f"[Armory] Daily check failed: {exc}")

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    def web_tables(self):
        conn = self.db()
        tracked = conn.execute(
            """
            SELECT t.item_id, t.min_qty, s.name, s.category, s.available, s.loaned
            FROM tracked t
            LEFT JOIN stock s ON s.item_id = t.item_id
            ORDER BY s.category, s.name
            """
        ).fetchall()
        stock = conn.execute(
            """
            SELECT s.item_id, s.name, s.category, s.available, s.loaned, s.quantity
            FROM stock s
            WHERE s.item_id NOT IN (SELECT item_id FROM tracked)
            ORDER BY s.category, s.name
            """
        ).fetchall()
        conn.close()
        tracked_rows = [
            {
                "id": row["item_id"],
                "Category": row["category"] or "?",
                "Item": row["name"] or row["item_id"],
                "On hand": row["available"] if row["available"] is not None else "no snapshot",
                "Loaned": row["loaned"] if row["loaned"] is not None else "",
                "Min": row["min_qty"],
            }
            for row in tracked
        ]
        addable_rows = [
            {
                "id": row["item_id"],
                "Category": row["category"],
                "Item": row["name"],
                "On hand": row["available"],
                "Loaned": row["loaned"],
            }
            for row in stock
        ]
        import html as html_lib
        options = "".join(
            f'<option value="{html_lib.escape(row["name"], quote=True)}"></option>'
            for row in stock
        )
        add_form = f"""
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="run_now">
          <button type="submit">Refresh armory snapshot now</button>
        </form>
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="track_item">
          <label>Item name</label>
          <input name="item_name" list="armory-stock" placeholder="Start typing, e.g. lollipop or beer" autocomplete="off">
          <datalist id="armory-stock">{options}</datalist>
          <label>Minimum on-hand quantity</label>
          <input name="min_qty" placeholder="100">
          <button type="submit">Track item</button>
        </form>
        <p class="help">Type an item name from the in-stock list. Item IDs are not needed.</p>
        """
        return [
            {
                "title": "Tracked minimums",
                "columns": ["Category", "Item", "On hand", "Loaned", "Min"],
                "rows": tracked_rows,
                "row_actions": [
                    {"action": "update_min", "label": "Save min", "include_min": True},
                    {"action": "untrack_item", "label": "Remove"},
                ],
                "add_form": add_form,
            },
            {
                "title": "In stock and not tracked",
                "columns": ["Category", "Item", "On hand", "Loaned"],
                "rows": addable_rows,
                "row_actions": [
                    {"action": "track_item", "label": "Track", "include_min": True},
                ],
            },
        ]

    def web_action(self, data):
        action = data.get("action")
        if action == "update_min":
            item_id = int(data.get("id"))
            min_qty = int(data.get("min_qty") or 0)
            conn = self.db()
            conn.execute("UPDATE tracked SET min_qty = ? WHERE item_id = ?", (min_qty, item_id))
            conn.commit()
            conn.close()
            return f"Updated minimum for item {item_id} to {min_qty}."
        if action == "untrack_item":
            item_id = int(data.get("id"))
            conn = self.db()
            conn.execute("DELETE FROM tracked WHERE item_id = ?", (item_id,))
            conn.commit()
            conn.close()
            return f"Stopped tracking item {item_id}."
        if action == "track_item":
            min_qty = int(data.get("min_qty") or 0)
            name = str(data.get("item_name") or data.get("item_id") or "").strip()
            if not name:
                return "Enter an item name from the in-stock list."
            conn = self.db()
            stock = conn.execute(
                "SELECT item_id, name FROM stock WHERE lower(name) = lower(?)",
                (name,),
            ).fetchone()
            if not stock:
                stock = conn.execute(
                    "SELECT item_id, name FROM stock WHERE lower(name) LIKE ? ORDER BY name LIMIT 6",
                    (f"%{name.lower()}%",),
                ).fetchall()
                if len(stock) == 1:
                    stock = stock[0]
                elif stock:
                    choices = ", ".join(row["name"] for row in stock)
                    conn.close()
                    return f"Several matches: {choices}. Type the exact name."
                else:
                    conn.close()
                    return f"No in-stock item matches '{name}'. Refresh the snapshot if it should be there."
            conn.execute(
                "INSERT OR REPLACE INTO tracked (item_id, min_qty, added_at) VALUES (?, ?, ?)",
                (stock["item_id"], min_qty, utc_now().isoformat()),
            )
            conn.commit()
            conn.close()
            return f"Tracking {stock['name']} with minimum {min_qty} on hand."
        if action == "run_now":
            self.bot.loop.create_task(self._run_now())
            return "Armory refresh started."
        return "Unknown action."

    async def _run_now(self):
        try:
            await self.run_check()
        except Exception as exc:
            print(f"[Armory] Manual refresh failed: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ArmoryTracker(bot))
