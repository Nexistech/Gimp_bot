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

# Importing configuration safely
try:
    from config import TORN_API_KEY, SHOPLIFTING_CHANNEL_ID, SPAM_CHANNEL_ID
except ImportError:
    TORN_API_KEY = ""
    SHOPLIFTING_CHANNEL_ID = 0
    SPAM_CHANNEL_ID = 0

# --- CONFIGURATION TOGGLES ---
ENABLE_LOGGING = True  # Set to False to turn off file logging

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "shoplifting_monitor.db")
LOG_FILE = os.path.join(PLUGIN_DIR, "shoplifting.log")
TARGET_CHANNEL_ID = SHOPLIFTING_CHANNEL_ID or SPAM_CHANNEL_ID

# Setting up logger conditionally
logger = logging.getLogger("ShopliftingMonitor")
logger.setLevel(logging.INFO if ENABLE_LOGGING else logging.CRITICAL)

if ENABLE_LOGGING:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=100000, backupCount=3)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(handler)
else:
    logger.addHandler(logging.NullHandler())

STORES = {
    "sallys_sweet_shop": "Sally's Sweet Shop",
    "Bits_n_bobs": "Bits 'n' Bobs",
    "tc_clothing": "TC Clothing",
    "super_store": "Super Store",
    "cyber_force": "Cyber Force",
    "pharmacy": "Pharmacy",
    "big_als": "Big Al's Gun Shop",
    "jewelry_store": "Jewelry Store"
}

# Stores that naturally only have a single security device
SINGLE_DEVICE_STORES = {"sallys_sweet_shop", "Bits_n_bobs"}

class ShopliftingMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.initialize_db()
        self.monitor_task.start()
        self.cleanup_subscriptions_task.start()
        logger.info("ShopliftingMonitor initialized and tasks started.")

    def cog_unload(self):
        self.monitor_task.cancel()
        self.cleanup_subscriptions_task.cancel()
        logger.info("ShopliftingMonitor tasks cancelled.")

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER, store_key TEXT, notify_on_single INTEGER DEFAULT 0, 
                      PRIMARY KEY (user_id, store_key))''')
        c.execute('''CREATE TABLE IF NOT EXISTS store_states
                     (store_key TEXT PRIMARY KEY, disabled_count INTEGER DEFAULT 0, last_changed TEXT)''')
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
                    else:
                        logger.error(f"API Error: Status {response.status}")
            except Exception as e:
                logger.error(f"Exception during API fetch: {e}")
        return None

    @tasks.loop(minutes=1.0)
    async def monitor_task(self):
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.execute("SELECT 1 FROM subscriptions LIMIT 1")
            has_subs = cursor.fetchone() is not None
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

                cursor = conn.execute("SELECT disabled_count, last_changed FROM store_states WHERE store_key = ?", (key,))
                row = cursor.fetchone()
                last_disabled = row[0] if row is not None else 0
                last_changed = row[1] if row is not None else None

                # Detect state transition
                if current_disabled != last_disabled:
                    # Log state change frequency for timing analysis
                    if last_changed:
                        prev_time = datetime.datetime.fromisoformat(last_changed)
                        curr_time = datetime.datetime.fromisoformat(now_iso)
                        duration_mins = round((curr_time - prev_time).total_seconds() / 60, 2)
                        logger.info(f"STATE CHANGE: {display_name} moved from {last_disabled}/{total_devices} down to {current_disabled}/{total_devices} down after {duration_mins} minutes.")
                    else:
                        logger.info(f"STATE CHANGE: {display_name} initial state recorded as {current_disabled}/{total_devices} down.")

                    conn.execute(
                        "INSERT OR REPLACE INTO store_states (store_key, disabled_count, last_changed) VALUES (?, ?, ?)",
                        (key, current_disabled, now_iso)
                    )

                    # Notify subscribers if conditions deteriorated
                    if current_disabled > last_disabled and current_disabled > 0:
                        await self.notify_subscribers(key, display_name, current_disabled, total_devices)

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Unexpected error in monitor_task: {e}", exc_info=True)

    @tasks.loop(hours=24.0)
    async def cleanup_subscriptions_task(self):
        """Removes subscriptions for users no longer in the guild."""
        logger.info("Running daily subscription cleanup...")
        conn = sqlite3.connect(DB_NAME)
        channel = self.bot.get_channel(TARGET_CHANNEL_ID)
        if not channel or not hasattr(channel, 'guild'):
            conn.close()
            return

        guild = channel.guild
        users = conn.execute("SELECT DISTINCT user_id FROM subscriptions").fetchall()

        for (user_id,) in users:
            if guild.get_member(user_id) is None:
                logger.info(f"User {user_id} no longer in guild. Removing their subscriptions.")
                conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))

        conn.commit()
        conn.close()

    @monitor_task.before_loop
    @cleanup_subscriptions_task.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()
        logger.info("Bot ready, tasks prepared.")

    async def notify_subscribers(self, store_key, display_name, disabled_count, total_devices):
        all_down = (disabled_count >= total_devices)
        is_single_store = (store_key in SINGLE_DEVICE_STORES)

        conn = sqlite3.connect(DB_NAME)

        # Logic:
        # - If store only has 1 device OR all devices are down: notify all subscribers of this store.
        # - If only 1 of multiple devices is down: notify only subscribers with notify_on_single = 1.
        if is_single_store or all_down:
            query = "SELECT user_id FROM subscriptions WHERE store_key = ?"
            params = (store_key,)
        else:
            query = "SELECT user_id FROM subscriptions WHERE store_key = ? AND notify_on_single = 1"
            params = (store_key,)

        subscribers = conn.execute(query, params).fetchall()
        conn.close()

        if not subscribers:
            return

        channel = self.bot.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            return

        mentions = " ".join([f"<@{sub[0]}>" for sub in subscribers])
        status_str = "ALL security devices down" if all_down else f"{disabled_count}/{total_devices} security devices down"

        try:
            await channel.send(f"🚨 **Alert:** {display_name} — {status_str}! {mentions}")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    @app_commands.command(name="notify_shop", description="Subscribe to alerts for a specific store.")
    @app_commands.choices(store=[app_commands.Choice(name=v, value=k) for k, v in STORES.items()])
    @app_commands.describe(notify_on_single="Alert when even a single device is down (default: False, requires ALL down)")
    async def notify_shop(self, interaction: discord.Interaction, store: str, notify_on_single: bool = False):
        single_flag = 1 if notify_on_single else 0
        conn = sqlite3.connect(DB_NAME)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions (user_id, store_key, notify_on_single) VALUES (?, ?, ?)",
                (interaction.user.id, store, single_flag)
            )
            conn.commit()
            
            mode_desc = "single device down" if (notify_on_single or store in SINGLE_DEVICE_STORES) else "ALL devices down"
            await interaction.response.send_message(
                f"✅ Subscribed to **{STORES[store]}** (Alert mode: **{mode_desc}**).",
                ephemeral=True
            )
        finally:
            conn.close()

    @app_commands.command(name="unnotify_shop", description="Remove alerts for a specific store.")
    @app_commands.choices(store=[app_commands.Choice(name=v, value=k) for k, v in STORES.items()])
    async def unnotify_shop(self, interaction: discord.Interaction, store: str):
        conn = sqlite3.connect(DB_NAME)
        conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND store_key = ?", (interaction.user.id, store))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ Notifications for **{STORES[store]}** disabled.", ephemeral=True)

    @app_commands.command(name="list_subscriptions", description="View your current shoplifting alerts.")
    async def list_subscriptions(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute("SELECT store_key, notify_on_single FROM subscriptions WHERE user_id = ?", (interaction.user.id,)).fetchall()
        conn.close()

        if not rows:
            return await interaction.response.send_message("You are not currently subscribed to any shoplifting alerts.", ephemeral=True)

        subs = []
        for store_key, single_flag in rows:
            if store_key in STORES:
                mode = "Single or All" if (single_flag or store_key in SINGLE_DEVICE_STORES) else "All Down Only"
                subs.append(f"• **{STORES[store_key]}** ({mode})")

        await interaction.response.send_message(f"🔔 **Your Current Alerts:**\n" + "\n".join(subs), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ShopliftingMonitor(bot))
