import datetime
import os
import re
import sqlite3
import sys

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_root_settings, load_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "banking.db")
GIVE_URL = "https://www.torn.com/factions.php?step=your#/tab=controls&option=give-to-user"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def format_money(amount):
    return f"${int(amount):,}"


def parse_amount(text, balance=None):
    raw = str(text or "").strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    if raw in {"all", "max", "full"}:
        if balance is None:
            raise ValueError("Balance unknown for 'all'.")
        return int(balance)
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([kmb])?", raw)
    if not match:
        raise ValueError("Could not parse that amount.")
    number = float(match.group(1))
    suffix = match.group(2)
    mult = {None: 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    amount = int(number * mult)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return amount


def parse_timeout(text):
    raw = str(text or "60m").strip().lower()
    if raw in {"never", "none", "0", "off"}:
        return None
    if re.fullmatch(r"\d+:\d+", raw):
        hours, minutes = raw.split(":")
        total = int(hours) * 60 + int(minutes)
        return total * 60 if total > 0 else None
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([hm])?", raw)
    if not match:
        raise ValueError("Could not parse timeout. Use 30m, 4h, 1:30, or never.")
    number = float(match.group(1))
    unit = match.group(2) or "m"
    seconds = int(number * (3600 if unit == "h" else 60))
    return seconds if seconds > 0 else None


class WithdrawView(ui.View):
    def __init__(self, cog, request_id, give_url):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        self.add_item(ui.Button(label="Fulfill in Torn", style=discord.ButtonStyle.link, url=give_url))

    @ui.button(label="Mark fulfilling", style=discord.ButtonStyle.green, custom_id="banking:fulfilling")
    async def mark_fulfilling(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.mark_fulfilling(interaction, self.request_id)

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="banking:cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.cancel_request(interaction, self.request_id)


class WithdrawAllView(ui.View):
    def __init__(self, cog, timeout_text):
        super().__init__(timeout=120)
        self.cog = cog
        self.timeout_text = timeout_text

    @ui.button(label="Withdraw all", style=discord.ButtonStyle.primary)
    async def withdraw_all(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.create_request(interaction, "all", self.timeout_text, ephemeral=True)


class Banking(commands.Cog):
    SETTINGS_SCHEMA = [
        {"key": "channel_id", "type": "int", "label": "Banking channel ID", "default": 0},
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping for a withdraw request",
            "default": [],
            "widget": "discord_roles",
        },
        {
            "key": "verified_role",
            "type": "str",
            "label": "Verified Discord role required to use /withdraw",
            "default": "Verified",
            "widget": "discord_role",
        },
        {
            "key": "default_timeout",
            "type": "str",
            "label": "Default request timeout",
            "default": "60m",
            "help": "Examples: 30m, 60m, 4h, never",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self.watch_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))

    def cog_unload(self):
        self.watch_task.cancel()

    def initialize_db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                torn_name TEXT,
                discord_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                fulfiller_discord_id INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                fulfilling_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def has_verified_role(self, member):
        role_name = str(self.settings.get("verified_role") or "").strip()
        if not role_name:
            return True
        return any(role.name == role_name for role in getattr(member, "roles", []))

    def resolve_torn_member(self, discord_user):
        roster = self.get_roster()
        if not roster:
            return None
        for member in roster.all_members():
            if str(member.get("discord_id") or "") == str(discord_user.id):
                return member
        return roster.get_member_by_name(getattr(discord_user, "display_name", "") or discord_user.name)

    async def fetch_donations(self):
        if not TORN_API_KEY:
            raise RuntimeError("TORN_API_KEY missing")
        url = f"https://api.torn.com/faction/?selections=donations&key={TORN_API_KEY}&comment=StrikeBot"
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("donations") or {}

    async def vault_balance(self, torn_id):
        donations = await self.fetch_donations()
        entry = donations.get(str(torn_id)) or donations.get(int(torn_id)) if False else donations.get(str(torn_id))
        if not entry:
            return 0
        return int(entry.get("money_balance") or 0)

    async def fetch_funds_news(self):
        url = f"https://api.torn.com/faction/?selections=fundsnews&key={TORN_API_KEY}&comment=StrikeBot"
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
        return data.get("fundsnews") or data.get("news") or {}

    def give_url(self, torn_id, amount):
        return (
            f"{GIVE_URL}&userID={int(torn_id)}&userId={int(torn_id)}"
            f"&XID={int(torn_id)}&money={int(amount)}"
        )

    def request_embed(self, row, extra=None):
        status = row["status"]
        color = {
            "open": discord.Color.gold(),
            "fulfilling": discord.Color.blue(),
            "paid": discord.Color.green(),
            "cancelled": discord.Color.dark_grey(),
            "expired": discord.Color.dark_grey(),
        }.get(status, discord.Color.gold())
        profile = f"https://www.torn.com/profiles.php?XID={row['torn_id']}"
        who = row["torn_name"] or f"Torn {row['torn_id']}"
        embed = discord.Embed(
            title=f"Vault withdraw #{row['id']}",
            description=(
                f"<@{row['discord_id']}> (**[{who}]({profile})** `{row['torn_id']}`) "
                f"requested **{format_money(row['amount'])}**."
            ),
            color=color,
            timestamp=utc_now(),
        )
        embed.add_field(name="Member", value=f"[{who}]({profile}) `{row['torn_id']}`", inline=True)
        embed.add_field(name="Amount", value=format_money(row["amount"]), inline=True)
        embed.add_field(name="Status", value=status.replace("_", " ").title(), inline=True)
        if row["expires_at"]:
            embed.add_field(name="Expires", value=row["expires_at"], inline=False)
        else:
            embed.add_field(name="Expires", value="Never", inline=False)
        if extra:
            embed.add_field(name="Note", value=extra, inline=False)
        return embed

    def view_for(self, row):
        if row["status"] not in {"open", "fulfilling"}:
            return None
        return WithdrawView(self, row["id"], self.give_url(row["torn_id"], row["amount"]))

    async def update_message(self, row, extra=None):
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if not channel or not row["message_id"]:
            return
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=self.request_embed(row, extra), view=self.view_for(row))
        except discord.HTTPException:
            pass

    async def create_request(self, interaction: discord.Interaction, amount_text, timeout_text, ephemeral=False):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.NotFound:
                pass
        if not self.has_verified_role(interaction.user):
            return await interaction.followup.send(
                f"You need the **{self.settings.get('verified_role') or 'Verified'}** role to withdraw.",
                ephemeral=True,
            )
        member = self.resolve_torn_member(interaction.user)
        if not member:
            return await interaction.followup.send(
                "I can't match your Discord account to a faction member. Verify / wait for a Discord ID backfill.",
                ephemeral=True,
            )
        torn_id = int(member.get("id") or member.get("user_id"))
        try:
            balance = await self.vault_balance(torn_id)
            amount = parse_amount(amount_text, balance)
            timeout_seconds = parse_timeout(timeout_text or self.settings.get("default_timeout") or "60m")
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            return await interaction.followup.send(f"Could not read the vault: {exc}", ephemeral=True)
        if amount > balance:
            view = WithdrawAllView(self, timeout_text or self.settings.get("default_timeout") or "60m")
            return await interaction.followup.send(
                f"Not enough in the vault. Your balance is **{format_money(balance)}**. "
                f"You asked for **{format_money(amount)}**.",
                view=view,
                ephemeral=True,
            )
        expires_at = None
        if timeout_seconds:
            expires_at = (utc_now() + datetime.timedelta(seconds=timeout_seconds)).isoformat()
        conn = self.db()
        cur = conn.execute(
            """
            INSERT INTO requests (
                torn_id, torn_name, discord_id, amount, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                torn_id,
                member.get("name"),
                interaction.user.id,
                amount,
                utc_now().isoformat(),
                expires_at,
            ),
        )
        request_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        conn.close()
        channel_id = int(self.settings.get("channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else interaction.channel
        if not channel:
            return await interaction.followup.send("Banking channel is not configured.", ephemeral=True)
        mentions = []
        if channel.guild:
            for role_name in self.settings.get("ping_roles") or []:
                role = discord.utils.get(channel.guild.roles, name=role_name)
                if role:
                    mentions.append(role.mention)
        try:
            message = await channel.send(
                content=" ".join(mentions) if mentions else None,
                embed=self.request_embed(row),
                view=self.view_for(row),
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                f"I don't have access to post in <#{channel.id}>. "
                "Give the bot View Channel + Send Messages + Embed Links there, or pick another banking channel.",
                ephemeral=True,
            )
        conn = self.db()
        conn.execute(
            "UPDATE requests SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel.id, message.id, request_id),
        )
        conn.commit()
        conn.close()
        await interaction.followup.send(
            f"Withdraw request #{request_id} posted for **{format_money(amount)}**.",
            ephemeral=True,
        )

    async def mark_fulfilling(self, interaction: discord.Interaction, request_id):
        conn = self.db()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row or row["status"] not in {"open", "fulfilling"}:
            conn.close()
            return await interaction.response.send_message("That request is no longer active.", ephemeral=True)
        conn.execute(
            "UPDATE requests SET status = 'fulfilling', fulfiller_discord_id = ?, fulfilling_at = ? WHERE id = ?",
            (interaction.user.id, utc_now().isoformat(), request_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        conn.close()
        await interaction.response.send_message(
            f"Marked #{request_id} as in progress. Open **Fulfill in Torn** and send the money. "
            "I will check the vault log in 10 minutes.",
            ephemeral=True,
        )
        await self.update_message(row, extra=f"In progress by <@{interaction.user.id}>")

    async def cancel_request(self, interaction: discord.Interaction, request_id):
        conn = self.db()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            conn.close()
            return await interaction.response.send_message("Request not found.", ephemeral=True)
        if interaction.user.id not in {row["discord_id"], row["fulfiller_discord_id"]} and not self.has_banker_power(interaction.user):
            if str(interaction.user.id) != str(row["discord_id"]):
                conn.close()
                return await interaction.response.send_message("Only the requester or a banker can cancel this.", ephemeral=True)
        conn.execute("UPDATE requests SET status = 'cancelled' WHERE id = ?", (request_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        conn.close()
        await interaction.response.send_message(f"Cancelled request #{request_id}.", ephemeral=True)
        await self.update_message(row, extra=f"Cancelled by <@{interaction.user.id}>")

    def has_banker_power(self, member):
        names = set(self.settings.get("ping_roles") or [])
        return any(role.name in names for role in getattr(member, "roles", []))

    async def confirm_paid(self, row):
        try:
            news = await self.fetch_funds_news()
        except Exception as exc:
            print(f"[Banking] fundsnews failed: {exc}")
            return False
        amount = int(row["amount"])
        torn_id = str(row["torn_id"])
        name = str(row["torn_name"] or "")
        items = news.values() if isinstance(news, dict) else news
        for item in items:
            text = str(item.get("news") or item.get("text") or item).lower()
            if torn_id in text or name.lower() in text:
                if str(amount) in text.replace(",", "") or format_money(amount).lower() in text:
                    return True
        # Fallback: balance dropped by about the requested amount.
        try:
            balance = await self.vault_balance(row["torn_id"])
            if balance + 1 < amount:
                return True
        except Exception:
            pass
        return False

    @tasks.loop(minutes=1)
    async def watch_task(self):
        conn = self.db()
        rows = conn.execute("SELECT * FROM requests WHERE status IN ('open', 'fulfilling')").fetchall()
        conn.close()
        now = utc_now()
        for row in rows:
            if row["status"] == "open" and row["expires_at"]:
                try:
                    expires = datetime.datetime.fromisoformat(row["expires_at"])
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=datetime.timezone.utc)
                    if now >= expires:
                        conn = self.db()
                        conn.execute("UPDATE requests SET status = 'expired' WHERE id = ?", (row["id"],))
                        conn.commit()
                        updated = conn.execute("SELECT * FROM requests WHERE id = ?", (row["id"],)).fetchone()
                        conn.close()
                        await self.update_message(updated, extra="Timed out.")
                        continue
                except ValueError:
                    pass
            if row["status"] == "fulfilling" and row["fulfilling_at"]:
                try:
                    started = datetime.datetime.fromisoformat(row["fulfilling_at"])
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue
                if now < started + datetime.timedelta(minutes=10):
                    continue
                if await self.confirm_paid(row):
                    conn = self.db()
                    conn.execute("UPDATE requests SET status = 'paid' WHERE id = ?", (row["id"],))
                    conn.commit()
                    updated = conn.execute("SELECT * FROM requests WHERE id = ?", (row["id"],)).fetchone()
                    conn.close()
                    who = f"<@{row['fulfiller_discord_id']}>" if row["fulfiller_discord_id"] else "a banker"
                    await self.update_message(updated, extra=f"Paid by {who}.")
                else:
                    conn = self.db()
                    conn.execute("UPDATE requests SET status = 'open', fulfilling_at = NULL WHERE id = ?", (row["id"],))
                    conn.commit()
                    updated = conn.execute("SELECT * FROM requests WHERE id = ?", (row["id"],)).fetchone()
                    conn.close()
                    extra = "Vault log did not show the payout. Request is open again."
                    await self.update_message(updated, extra=extra)
                    channel = self.bot.get_channel(updated["channel_id"]) if updated["channel_id"] else None
                    if channel and channel.guild:
                        mentions = []
                        for role_name in self.settings.get("ping_roles") or []:
                            role = discord.utils.get(channel.guild.roles, name=role_name)
                            if role:
                                mentions.append(role.mention)
                        if mentions:
                            await channel.send(
                                f"{' '.join(mentions)} withdraw #{updated['id']} still needs to be paid."
                            )

    @watch_task.before_loop
    async def before_watch(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="withdraw", description="Request money from the faction vault.")
    @app_commands.describe(amount="Amount such as 1,000,000, 1m, 250k, or all", timeout="30m, 60m, 1:30, 4h, or never")
    async def withdraw_command(self, interaction: discord.Interaction, amount: str, timeout: str = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await self.create_request(interaction, amount, timeout)

    @app_commands.command(name="balance", description="Show your faction vault balance.")
    async def balance_command(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not self.has_verified_role(interaction.user):
            return await interaction.followup.send("You need the verified role to check vault balance.", ephemeral=True)
        member = self.resolve_torn_member(interaction.user)
        if not member:
            return await interaction.followup.send("I can't match your Discord account to a faction member.", ephemeral=True)
        try:
            balance = await self.vault_balance(int(member.get("id") or member.get("user_id")))
        except Exception as exc:
            return await interaction.followup.send(f"Could not read the vault: {exc}", ephemeral=True)
        await interaction.followup.send(
            f"**{member.get('name')}** vault balance: **{format_money(balance)}**",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Banking(bot))
