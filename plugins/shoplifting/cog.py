import datetime
import sqlite3
import os
import sys
import aiohttp
import discord
import logging
from logging.handlers import RotatingFileHandler
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "shoplifting_monitor.db")
LOG_FILE = os.path.join(PLUGIN_DIR, "shoplifting.log")

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

logger = logging.getLogger("ShopliftingMonitor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=100000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

STORES = {
    "sallys_sweet_shop": "Sally's Sweet Shop",
    "Bits_n_bobs": "Bits 'n' Bobs",
    "tc_clothing": "TC Clothing",
    "super_store": "Super Store",
    "cyber_force": "Cyber Force",
    "pharmacy": "Pharmacy",
    "big_als": "Big Al's Gun Shop",
    "jewelry_store": "Jewelry Store",
}
SINGLE_DEVICE_STORES = {"sallys_sweet_shop", "Bits_n_bobs"}


class ShopliftingMonitor(commands.Cog):
    SETTINGS_SCHEMA = [
        {"key": "enable_logging", "type": "bool", "label": "Write shoplifting.log", "default": True},
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.monitor_task.start()
        self.cleanup_subscriptions_task.start()
        logger.info("ShopliftingMonitor initialized and tasks started.")

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))

    def cog_unload(self):
        self.monitor_task.cancel()
        self.cleanup_subscriptions_task.cancel()

    def target_channel_id(self):
        root = load_root_settings()
        return int(root.get("SHOPLIFTING_CHANNEL_ID") or root.get("SPAM_CHANNEL_ID") or 0)

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS subscriptions
               (user_id INTEGER, store_key TEXT, notify_on_single INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, store_key))"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS store_states
               (store_key TEXT PRIMARY KEY, disabled_count INTEGER DEFAULT 0, last_changed TEXT)"""
        )
        conn.commit()
        conn.close()

    async def fetch_shoplifting_data(self):
        if not TORN_API_KEY:
            logger.error("No TORN_API_KEY found.")
            return None
        url = f"https://api.torn.com/torn/?selections=shoplifting&key={TORN_API_KEY}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.error("API Error: Status %s", response.status)
            except Exception as e:
                logger.error("Exception during API fetch: %s", e)
        return None

    @tasks.loop(minutes=1.0)
    async def monitor_task(self):
        try:
            conn = sqlite3.connect(DB_NAME)
            has_subs = conn.execute("SELECT 1 FROM subscriptions LIMIT 1").fetchone() is not None
            conn.close()
            if not has_subs:
                return
            data = await self.fetch_shoplifting_data()
            if not data or "shoplifting" not in data:
                return
            shop_data = data["shoplifting"]
            conn = sqlite3.connect(DB_NAME)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for key, display_name in STORES.items():
                security_items = shop_data.get(key, [])
                total_devices = len(security_items)
                current_disabled = sum(1 for item in security_items if item.get("disabled") is True)
                row = conn.execute(
                    "SELECT disabled_count, last_changed FROM store_states WHERE store_key = ?",
                    (key,),
                ).fetchone()
                last_disabled = row[0] if row is not None else 0
                if current_disabled != last_disabled:
                    conn.execute(
                        "INSERT OR REPLACE INTO store_states (store_key, disabled_count, last_changed) VALUES (?, ?, ?)",
                        (key, current_disabled, now_iso),
                    )
                    if current_disabled > last_disabled and current_disabled > 0:
                        await self.notify_subscribers(key, display_name, current_disabled, total_devices)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Unexpected error in monitor_task: %s", e, exc_info=True)

    @tasks.loop(hours=24.0)
    async def cleanup_subscriptions_task(self):
        conn = sqlite3.connect(DB_NAME)
        channel = self.bot.get_channel(self.target_channel_id())
        if not channel or not hasattr(channel, "guild"):
            conn.close()
            return
        guild = channel.guild
        users = conn.execute("SELECT DISTINCT user_id FROM subscriptions").fetchall()
        for (user_id,) in users:
            if guild.get_member(user_id) is None:
                conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    @monitor_task.before_loop
    @cleanup_subscriptions_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    async def notify_subscribers(self, store_key, display_name, disabled_count, total_devices):
        all_down = disabled_count >= total_devices
        is_single_store = store_key in SINGLE_DEVICE_STORES
        conn = sqlite3.connect(DB_NAME)
        if is_single_store or all_down:
            subscribers = conn.execute(
                "SELECT user_id FROM subscriptions WHERE store_key = ?", (store_key,)
            ).fetchall()
        else:
            subscribers = conn.execute(
                "SELECT user_id FROM subscriptions WHERE store_key = ? AND notify_on_single = 1",
                (store_key,),
            ).fetchall()
        conn.close()
        if not subscribers:
            return
        channel = self.bot.get_channel(self.target_channel_id())
        if not channel:
            return
        mentions = " ".join(f"<@{sub[0]}>" for sub in subscribers)
        status_str = "ALL security devices down" if all_down else f"{disabled_count}/{total_devices} security devices down"
        await channel.send(f"🚨 **Alert:** {display_name} — {status_str}! {mentions}")

    @app_commands.command(name="notify_shop", description="Subscribe to alerts for a specific store.")
    @app_commands.choices(store=[app_commands.Choice(name=v, value=k) for k, v in STORES.items()])
    async def notify_shop(self, interaction: discord.Interaction, store: str, notify_on_single: bool = False):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, store_key, notify_on_single) VALUES (?, ?, ?)",
            (interaction.user.id, store, 1 if notify_on_single else 0),
        )
        conn.commit()
        conn.close()
        mode_desc = "single device down" if (notify_on_single or store in SINGLE_DEVICE_STORES) else "ALL devices down"
        await interaction.response.send_message(
            f"✅ Subscribed to **{STORES[store]}** (Alert mode: **{mode_desc}**).",
            ephemeral=True,
        )

    @app_commands.command(name="unnotify_shop", description="Remove alerts for a specific store.")
    @app_commands.choices(store=[app_commands.Choice(name=v, value=k) for k, v in STORES.items()])
    async def unnotify_shop(self, interaction: discord.Interaction, store: str):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND store_key = ?",
            (interaction.user.id, store),
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ Notifications for **{STORES[store]}** disabled.", ephemeral=True)

    @app_commands.command(name="list_subscriptions", description="View your current shoplifting alerts.")
    async def list_subscriptions(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute(
            "SELECT store_key, notify_on_single FROM subscriptions WHERE user_id = ?",
            (interaction.user.id,),
        ).fetchall()
        conn.close()
        if not rows:
            return await interaction.response.send_message(
                "You are not currently subscribed to any shoplifting alerts.", ephemeral=True
            )
        subs = []
        for store_key, single_flag in rows:
            if store_key in STORES:
                mode = "Single or All" if (single_flag or store_key in SINGLE_DEVICE_STORES) else "All Down Only"
                subs.append(f"• **{STORES[store_key]}** ({mode})")
        await interaction.response.send_message("🔔 **Your Current Alerts:**\n" + "\n".join(subs), ephemeral=True)

    def web_tables(self):
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute(
            "SELECT user_id, store_key, notify_on_single FROM subscriptions ORDER BY store_key, user_id"
        ).fetchall()
        conn.close()
        table_rows = []
        for user_id, store_key, single_flag in rows:
            member = self.bot.get_user(user_id)
            who = f"{member} ({user_id})" if member else str(user_id)
            mode = "Single or All" if (single_flag or store_key in SINGLE_DEVICE_STORES) else "All Down Only"
            table_rows.append(
                {
                    "id": f"{user_id}|{store_key}",
                    "Discord": who,
                    "Store": STORES.get(store_key, store_key),
                    "Mode": mode,
                }
            )
        return [
            {
                "title": "Active subscriptions",
                "columns": ["Discord", "Store", "Mode"],
                "rows": table_rows,
                "remove_action": "remove_subscription",
            }
        ]

    def web_action(self, data):
        if data.get("action") != "remove_subscription":
            return "Unknown action."
        raw = str(data.get("id") or "")
        if "|" not in raw:
            return "Invalid subscription id."
        user_id, store_key = raw.split("|", 1)
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND store_key = ?",
            (int(user_id), store_key),
        )
        conn.commit()
        conn.close()
        return f"Removed {STORES.get(store_key, store_key)} for {user_id}."


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopliftingMonitor(bot))
