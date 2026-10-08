"""Armory deposit payouts.

Polls faction armory news once a minute. A line is kept only when someone
deposited items. Used, loaned, given, and returned lines are ignored.

The first deposit of an item in 24 hours loads the public item market,
averages the cheapest 10 listing prices, and caches that average. Later
deposits of the same item reuse it until the cache is a day old.

/payout shows the caller's own unpaid total unless they have a payout role.
Those roles can look up any member, mark the balance paid, or press Done to
check faction payday logs immediately. Only a "was paid ... from the faction"
line counts, not a vault "was given" withdrawal. A separate clear role can
zero a balance with /payout member:name clear:True.
"""

import datetime
import os
import re
import sqlite3
import sys
import asyncio

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import load_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "armory_payout.db")
OC_TOOLS_DB = os.path.join(os.getcwd(), "plugins", "oc_tools", "oc_tools.db")
PRICE_TTL_SECONDS = 24 * 3600
VERIFY_AFTER_SECONDS = 20 * 60
NEWS_LOOKBACK_SECONDS = 5 * 60
AMOUNT_TOLERANCE = 0.10
DEPOSIT_RE = re.compile(r"deposited\s+([\d,]+)\s*x\s+(.+?)(?:\s+into\b.*)?$", re.I)
SKIP_RE = re.compile(r"\b(used|loaned|gave|given|returned)\b", re.I)
MONEY_RE = re.compile(r"\$([\d,]+)")
TAG_RE = re.compile(r"<[^>]+>")
XID_RE = re.compile(r"XID=(\d+)", re.I)
PAYDAY_RE = re.compile(
    r"^(?P<name>.+?) was paid \$(?P<amount>[\d,]+) for a total of \$[\d,]+ from the faction by .+$",
    re.I,
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def format_money(amount):
    return f"${int(amount):,}"


class ArmoryPayout(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "digest_channel_id",
            "type": "int",
            "label": "Daily owed digest channel (0 disables it)",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "run_hour_utc",
            "type": "int",
            "label": "Digest hour (0-23 UTC)",
            "default": 16,
        },
        {
            "key": "run_minute_utc",
            "type": "int",
            "label": "Digest minute (0-59 UTC)",
            "default": 0,
        },
        {
            "key": "payout_roles",
            "type": "str_list",
            "label": "Roles that can look up any member and mark a payout paid",
            "default": [],
            "widget": "discord_roles",
        },
        {
            "key": "clear_roles",
            "type": "str_list",
            "label": "Roles that can zero a member's unpaid balance",
            "default": [],
            "widget": "discord_roles",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.item_names = {}
        self._pay_lock = asyncio.Lock()
        self.initialize_db()
        self.load_item_names()
        self.digest_task.change_interval(time=self.digest_time())
        self.watch_task.start()
        self.digest_task.start()

    def cog_unload(self):
        self.watch_task.cancel()
        self.digest_task.cancel()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.digest_task.change_interval(time=self.digest_time())

    def digest_time(self):
        hour = max(0, min(23, int(self.settings.get("run_hour_utc") or 16)))
        minute = max(0, min(59, int(self.settings.get("run_minute_utc") or 0)))
        return datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc)

    def db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize_db(self):
        conn = self.db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS prices (
                item_id INTEGER PRIMARY KEY,
                item_name TEXT,
                average INTEGER NOT NULL,
                sampled_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY,
                news_id TEXT UNIQUE,
                torn_id INTEGER,
                torn_name TEXT,
                item_name TEXT,
                item_id INTEGER,
                quantity INTEGER,
                unit_price INTEGER,
                total INTEGER,
                deposited_at INTEGER,
                status TEXT,
                payout_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY,
                torn_id INTEGER,
                torn_name TEXT,
                amount INTEGER,
                marked_by INTEGER,
                marked_at INTEGER,
                verify_after INTEGER,
                status TEXT,
                news_text TEXT
            );
            """
        )
        conn.commit()
        conn.close()

    def meta_get(self, key):
        conn = self.db()
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        conn.close()
        return None if row is None else row["value"]

    def meta_set(self, key, value):
        conn = self.db()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

    def load_item_names(self):
        if not os.path.isfile(OC_TOOLS_DB):
            return
        try:
            conn = sqlite3.connect(OC_TOOLS_DB)
            rows = conn.execute("SELECT item_id, name FROM torn_items").fetchall()
            conn.close()
        except sqlite3.Error:
            return
        self.item_names = {int(item_id): name for item_id, name in rows}

    def catalog(self):
        tools = self.bot.get_cog("OCToolsMonitor")
        shared = getattr(tools, "item_names", None) if tools else None
        if shared:
            return dict(shared)
        return dict(self.item_names)

    def match_item_id(self, item_name):
        wanted = " ".join(item_name.strip().lower().split())
        catalog = self.catalog()
        for item_id, name in catalog.items():
            if name.lower() == wanted:
                return int(item_id), name
        contains = [(item_id, name) for item_id, name in catalog.items() if wanted and wanted in name.lower()]
        if len(contains) == 1:
            return int(contains[0][0]), contains[0][1]
        return None, item_name.strip()

    def get_roster(self):
        return self.bot.get_cog("FactionRoster")

    def member_for_discord(self, discord_user):
        roster = self.get_roster()
        if not roster:
            return None
        for member in roster.all_members():
            if str(member.get("discord_id") or "") == str(discord_user.id):
                return member
        return None

    def member_for_query(self, query):
        roster = self.get_roster()
        if not roster or not query:
            return None
        text = str(query).strip()
        if text.isdigit():
            return roster.get_member(int(text))
        return roster.get_member_by_name(text)

    def role_names(self, key):
        return {str(name) for name in (self.settings.get(key) or [])}

    def has_role(self, member, key):
        names = self.role_names(key)
        return any(role.name in names for role in getattr(member, "roles", []))

    def is_payout_staff(self, member):
        return self.has_role(member, "payout_roles")

    def is_clear_staff(self, member):
        return self.has_role(member, "clear_roles")

    def cached_price(self, item_id):
        conn = self.db()
        row = conn.execute("SELECT average, sampled_at FROM prices WHERE item_id = ?", (int(item_id),)).fetchone()
        conn.close()
        if not row:
            return None
        if int(utc_now().timestamp()) - int(row["sampled_at"]) > PRICE_TTL_SECONDS:
            return None
        return int(row["average"])

    def store_price(self, item_id, item_name, average):
        conn = self.db()
        conn.execute(
            "INSERT OR REPLACE INTO prices (item_id, item_name, average, sampled_at) VALUES (?, ?, ?, ?)",
            (int(item_id), item_name, int(average), int(utc_now().timestamp())),
        )
        conn.commit()
        conn.close()

    async def torn_get(self, url):
        headers = {"User-Agent": "GimpBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])
        return data

    def listing_costs(self, payload):
        raw = None
        if isinstance(payload, dict):
            itemmarket = payload.get("itemmarket") or payload.get("itemMarket") or {}
            if isinstance(itemmarket, dict):
                raw = itemmarket.get("listings")
            if raw is None:
                raw = payload.get("listings") or payload.get("itemmarketlistings") or itemmarket
        if raw is None:
            raw = payload
        if isinstance(raw, dict):
            raw = list(raw.values())
        costs = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            cost = row.get("price")
            if cost is None:
                cost = row.get("cost")
            if cost is None:
                continue
            try:
                costs.append(int(cost))
            except (TypeError, ValueError):
                continue
        return costs

    async def market_average(self, item_id):
        url = f"https://api.torn.com/v2/market/{int(item_id)}/itemmarket?key={TORN_API_KEY}&comment=GimpBot&offset=0"
        payload = await self.torn_get(url)
        costs = sorted(cost for cost in self.listing_costs(payload) if cost > 0)[:10]
        if not costs:
            return None
        return int(round(sum(costs) / len(costs)))

    async def price_for(self, item_id, item_name):
        cached = self.cached_price(item_id)
        if cached is not None:
            return cached
        average = await self.market_average(item_id)
        if average is None:
            return None
        self.store_price(item_id, item_name, average)
        return average

    def parse_deposit(self, text):
        plain = TAG_RE.sub("", text or "")
        plain = " ".join(plain.replace("&", "&").split())
        linked_id = None
        xid = XID_RE.search(text or "")
        if xid:
            linked_id = int(xid.group(1))
        if not plain or SKIP_RE.search(plain) or "deposited" not in plain.lower():
            return None
        match = DEPOSIT_RE.search(plain)
        if not match:
            return None
        quantity = int(match.group(1).replace(",", ""))
        item_name = match.group(2).strip(" .")
        donor = plain[: match.start()].strip(" .")
        donor = re.sub(r"\s+has$", "", donor, flags=re.I).strip()
        if quantity < 1 or not item_name or not (donor or linked_id):
            return None
        return donor, quantity, item_name, linked_id

    async def refresh_item_catalog(self):
        if not TORN_API_KEY or getattr(self, "_catalog_fetched", False):
            return
        self._catalog_fetched = True
        url = f"https://api.torn.com/torn/?selections=items&key={TORN_API_KEY}&comment=GimpBot"
        try:
            payload = await self.torn_get(url)
        except Exception as exc:
            print(f"[ArmoryPayout] Item catalog fetch failed: {exc}")
            return
        mapping = {}
        for raw_id, info in (payload.get("items") or {}).items():
            name = (info or {}).get("name") if isinstance(info, dict) else None
            if not name:
                continue
            try:
                mapping[int(raw_id)] = name
            except (TypeError, ValueError):
                continue
        if mapping:
            self.item_names.update(mapping)
            print(f"[ArmoryPayout] Cached {len(mapping)} item names.")

    async def poll_armory_news(self):
        if not TORN_API_KEY:
            return
        now_ts = int(utc_now().timestamp())
        from_ts = now_ts - NEWS_LOOKBACK_SECONDS
        url = (
            "https://api.torn.com/faction/?selections=armorynews&striptags=true"
            f"&from={from_ts}&to={now_ts}&key={TORN_API_KEY}&comment=GimpBot"
        )
        try:
            payload = await self.torn_get(url)
        except Exception as exc:
            print(f"[ArmoryPayout] armorynews failed: {exc}")
            return
        news = payload.get("armorynews") or payload.get("news") or {}
        entries = []
        if isinstance(news, dict):
            for news_id, row in news.items():
                if isinstance(row, dict):
                    entries.append((str(news_id), row))
        elif isinstance(news, list):
            for index, row in enumerate(news):
                if isinstance(row, dict):
                    entries.append((str(row.get("id") or index), row))
        entries.sort(key=lambda pair: int((pair[1].get("timestamp") or 0)))
        for news_id, row in entries:
            timestamp = int(row.get("timestamp") or 0)
            if timestamp and timestamp < from_ts:
                continue
            text = str(row.get("news") or row.get("text") or "")
            parsed = self.parse_deposit(text)
            if not parsed:
                continue
            donor, quantity, item_name, linked_id = parsed
            if self.deposit_complete(news_id):
                continue
            roster = self.get_roster()
            member = roster.get_member(linked_id) if roster and linked_id else None
            if not member:
                member = self.member_for_query(donor)
            torn_id = linked_id
            torn_name = donor or str(linked_id or "Unknown")
            if member:
                torn_id = int(member.get("id") or member.get("user_id") or linked_id)
                torn_name = member.get("name") or torn_name
            item_id, clean_name = self.match_item_id(item_name)
            if not item_id:
                await self.refresh_item_catalog()
                item_id, clean_name = self.match_item_id(item_name)
            unit_price = 0
            if item_id:
                try:
                    priced = await self.price_for(item_id, clean_name)
                except Exception as exc:
                    print(f"[ArmoryPayout] Price lookup failed for {clean_name}: {exc}")
                    priced = None
                if priced is None:
                    print(f"[ArmoryPayout] No market price for {clean_name} [{item_id}]. Deposit saved at $0.")
                else:
                    unit_price = int(priced)
            self.save_deposit(
                news_id, torn_id, torn_name, clean_name, item_id, quantity, unit_price, timestamp
            )
            print(
                f"[ArmoryPayout] {torn_name} [{torn_id}] deposited {quantity}x {clean_name} "
                f"at {format_money(unit_price)} each."
            )

    def deposit_complete(self, news_id):
        conn = self.db()
        row = conn.execute(
            "SELECT torn_id, total, status FROM deposits WHERE news_id = ?",
            (str(news_id),),
        ).fetchone()
        conn.close()
        if not row:
            return False
        if row["status"] in {"paid", "pending"}:
            return True
        return bool(row["torn_id"]) and int(row["total"] or 0) > 0

    def save_deposit(self, news_id, torn_id, torn_name, item_name, item_id, quantity, unit_price, timestamp):
        conn = self.db()
        existing = conn.execute("SELECT id, status FROM deposits WHERE news_id = ?", (str(news_id),)).fetchone()
        total = int(quantity) * int(unit_price)
        if existing:
            if existing["status"] == "unpaid":
                conn.execute(
                    """
                    UPDATE deposits
                    SET torn_id = ?, torn_name = ?, item_name = ?, item_id = ?, quantity = ?,
                        unit_price = ?, total = ?
                    WHERE news_id = ? AND status = 'unpaid'
                    """,
                    (torn_id, torn_name, item_name, item_id, int(quantity), int(unit_price), total, str(news_id)),
                )
        else:
            conn.execute(
                """
                INSERT INTO deposits (
                    news_id, torn_id, torn_name, item_name, item_id, quantity,
                    unit_price, total, deposited_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid')
                """,
                (
                    str(news_id),
                    torn_id,
                    torn_name,
                    item_name,
                    item_id,
                    int(quantity),
                    int(unit_price),
                    total,
                    int(timestamp),
                ),
            )
        conn.commit()
        conn.close()

    def rows_for(self, torn_id=None, torn_name=None, status="unpaid"):
        conn = self.db()
        if torn_id:
            rows = conn.execute(
                "SELECT * FROM deposits WHERE torn_id = ? AND status = ? ORDER BY deposited_at",
                (int(torn_id), status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deposits WHERE lower(torn_name) = lower(?) AND status = ? ORDER BY deposited_at",
                (torn_name or "", status),
            ).fetchall()
        conn.close()
        return rows

    def unpaid_summary(self):
        conn = self.db()
        rows = conn.execute(
            """
            SELECT torn_id, torn_name, SUM(total) AS owed, COUNT(*) AS deposits
            FROM deposits
            WHERE status = 'unpaid'
            GROUP BY torn_id, torn_name
            ORDER BY owed DESC, torn_name
            """
        ).fetchall()
        conn.close()
        return rows

    def payment_matches(self, text, torn_name, amount):
        match = PAYDAY_RE.match(" ".join(str(text or "").split()))
        if not match:
            return False
        if match.group("name").strip().casefold() != str(torn_name or "").strip().casefold():
            return False
        paid = int(match.group("amount").replace(",", ""))
        return abs(paid - int(amount)) <= max(1, int(int(amount) * AMOUNT_TOLERANCE))

    def used_payday_ids(self):
        conn = self.db()
        rows = conn.execute(
            "SELECT news_text FROM payouts WHERE status = 'paid' AND news_text IS NOT NULL"
        ).fetchall()
        conn.close()
        return {str(row["news_text"]).split("\n", 1)[0] for row in rows}

    async def fetch_give_funds(self):
        url = (
            "https://api.torn.com/v2/faction/news?cat=giveFunds&striptags=true&limit=100"
            f"&key={TORN_API_KEY}&comment=GimpBot"
        )
        payload = await self.torn_get(url)
        news = payload.get("news") or []
        return news if isinstance(news, list) else []

    async def find_payday(self, torn_name, amount, not_before=0):
        used = self.used_payday_ids()
        for item in await self.fetch_give_funds():
            if not isinstance(item, dict):
                continue
            news_id = str(item.get("id") or "")
            if news_id and news_id in used:
                continue
            timestamp = int(item.get("timestamp") or 0)
            if not_before and timestamp and timestamp < int(not_before):
                continue
            text = str(item.get("text") or item.get("news") or "")
            if self.payment_matches(text, torn_name, amount):
                return {"id": news_id, "text": text, "timestamp": timestamp}
        return None

    def mark_rows_paid(self, torn_id, torn_name, amount, payday, existing_payout_id=None):
        note = f"{payday.get('id') or ''}\n{payday.get('text') or ''}"[:500]
        conn = self.db()
        if existing_payout_id:
            conn.execute(
                "UPDATE payouts SET status = 'paid', news_text = ? WHERE id = ?",
                (note, int(existing_payout_id)),
            )
            conn.execute(
                "UPDATE deposits SET status = 'paid' WHERE payout_id = ? AND status = 'pending'",
                (int(existing_payout_id),),
            )
        else:
            now_ts = int(utc_now().timestamp())
            cursor = conn.execute(
                """
                INSERT INTO payouts (torn_id, torn_name, amount, marked_by, marked_at, verify_after, status, news_text)
                VALUES (?, ?, ?, NULL, ?, ?, 'paid', ?)
                """,
                (int(torn_id), torn_name, int(amount), now_ts, now_ts, note),
            )
            conn.execute(
                """
                UPDATE deposits
                SET status = 'paid', payout_id = ?
                WHERE torn_id = ? AND status = 'unpaid'
                """,
                (cursor.lastrowid, int(torn_id)),
            )
        conn.commit()
        conn.close()

    def clear_balance(self, torn_id):
        conn = self.db()
        row = conn.execute(
            """
            SELECT COALESCE(SUM(total), 0) AS owed, COUNT(*) AS deposits
            FROM deposits
            WHERE torn_id = ? AND status IN ('unpaid', 'pending')
            """,
            (int(torn_id),),
        ).fetchone()
        conn.execute(
            """
            UPDATE deposits SET status = 'cleared', payout_id = NULL
            WHERE torn_id = ? AND status IN ('unpaid', 'pending')
            """,
            (int(torn_id),),
        )
        conn.execute(
            "UPDATE payouts SET status = 'cleared' WHERE torn_id = ? AND status = 'pending'",
            (int(torn_id),),
        )
        conn.commit()
        conn.close()
        return int(row["owed"] or 0), int(row["deposits"] or 0)

    async def confirm_done(self, interaction, torn_id):
        if not self.is_payout_staff(interaction.user):
            if not interaction.response.is_done():
                await interaction.response.send_message("You cannot confirm payouts.", ephemeral=True)
            return
        async with self._pay_lock:
            if interaction.response.is_done():
                return
            member = self.member_for_query(str(torn_id))
            name = (member or {}).get("name") or str(torn_id)
            pending = self.rows_for(torn_id=torn_id, status="pending")
            unpaid = self.rows_for(torn_id=torn_id, status="unpaid")
            rows = pending or unpaid
            amount = sum(int(row["total"] or 0) for row in rows)
            if amount <= 0:
                await interaction.response.send_message("Nothing owed to check.", ephemeral=True)
                return
            not_before = min(int(row["deposited_at"] or 0) for row in rows)
            try:
                payday = await self.find_payday(name, amount, not_before)
            except Exception as exc:
                await interaction.response.send_message(f"Could not read faction pay logs: {exc}", ephemeral=True)
                return
            if not payday:
                await interaction.response.send_message(
                    f"No payday log for **{name}** near {format_money(amount)}. "
                    "A vault line that says \"was given\" does not count.",
                    ephemeral=True,
                )
                return
            payout_id = pending[0]["payout_id"] if pending else None
            self.mark_rows_paid(torn_id, name, amount, payday, payout_id)
            await interaction.response.send_message(
                f"Payday confirmed for **{name}**: {payday['text']}",
                ephemeral=True,
            )

    async def verify_due_payouts(self):
        now_ts = int(utc_now().timestamp())
        conn = self.db()
        due = conn.execute(
            "SELECT * FROM payouts WHERE status = 'pending' AND verify_after <= ?",
            (now_ts,),
        ).fetchall()
        conn.close()
        if not due or not TORN_API_KEY:
            return
        try:
            await self.fetch_give_funds()
        except Exception as exc:
            print(f"[ArmoryPayout] giveFunds failed, leaving payouts pending: {exc}")
            return
        for payout in due:
            try:
                matched = await self.find_payday(payout["torn_name"], payout["amount"], payout["marked_at"])
            except Exception as exc:
                print(f"[ArmoryPayout] Payday check failed for {payout['torn_name']}: {exc}")
                continue
            conn = self.db()
            if matched:
                note = f"{matched.get('id') or ''}\n{matched.get('text') or ''}"[:500]
                conn.execute(
                    "UPDATE deposits SET status = 'paid' WHERE payout_id = ? AND status = 'pending'",
                    (payout["id"],),
                )
                conn.execute(
                    "UPDATE payouts SET status = 'paid', news_text = ? WHERE id = ?",
                    (note, payout["id"]),
                )
                print(f"[ArmoryPayout] Confirmed payday for {payout['torn_name']}.")
            else:
                conn.execute(
                    "UPDATE deposits SET status = 'unpaid', payout_id = NULL WHERE payout_id = ? AND status = 'pending'",
                    (payout["id"],),
                )
                conn.execute("UPDATE payouts SET status = 'reverted' WHERE id = ?", (payout["id"],))
                print(f"[ArmoryPayout] No payday log for {payout['torn_name']}. Payout returned to unpaid.")
            conn.commit()
            conn.close()

    def describe_member(self, member, rows, pending_rows):
        name = member.get("name") or "Unknown"
        torn_id = member.get("id") or member.get("user_id")
        owed = sum(int(row["total"] or 0) for row in rows)
        lines = []
        for row in rows[-15:]:
            when = datetime.datetime.fromtimestamp(int(row["deposited_at"]), datetime.timezone.utc).strftime("%Y-%m-%d")
            lines.append(
                f"• {when} — {row['quantity']}x {row['item_name']} at {format_money(row['unit_price'])} "
                f"= {format_money(row['total'])}"
            )
        if len(rows) > 15:
            lines.insert(0, f"Showing the latest 15 of {len(rows)} unpaid deposits.")
        pending_total = sum(int(row["total"] or 0) for row in pending_rows)
        header = f"**{name}** [{torn_id}] is owed **{format_money(owed)}**."
        if pending_total:
            header += f"\n**{format_money(pending_total)}** is waiting on a vault payment check."
        if not lines:
            body = "No unpaid armory deposits."
        else:
            body = "\n".join(lines)
        return header + "\n" + body, owed

    async def mark_paid(self, interaction, torn_id):
        if not self.is_payout_staff(interaction.user):
            if not interaction.response.is_done():
                await interaction.response.send_message("You cannot mark payouts paid.", ephemeral=True)
            return
        async with self._pay_lock:
            if interaction.response.is_done():
                return
            member = self.member_for_query(str(torn_id))
            if not member:
                message = "That member is not on the roster."
            else:
                rows = self.rows_for(torn_id=torn_id, status="unpaid")
                amount = sum(int(row["total"] or 0) for row in rows)
                if amount <= 0:
                    message = "Nothing unpaid to mark."
                else:
                    now_ts = int(utc_now().timestamp())
                    conn = self.db()
                    cursor = conn.execute(
                        """
                        INSERT INTO payouts (torn_id, torn_name, amount, marked_by, marked_at, verify_after, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            int(torn_id),
                            member.get("name"),
                            amount,
                            interaction.user.id,
                            now_ts,
                            now_ts + VERIFY_AFTER_SECONDS,
                        ),
                    )
                    payout_id = cursor.lastrowid
                    conn.execute(
                        "UPDATE deposits SET status = 'pending', payout_id = ? WHERE torn_id = ? AND status = 'unpaid'",
                        (payout_id, int(torn_id)),
                    )
                    conn.commit()
                    conn.close()
                    message = (
                        f"Marked **{format_money(amount)}** for **{member.get('name')}** as waiting on payment. "
                        "I will check faction funds news in 20 minutes. "
                        "If nothing within 10% was paid to them, this goes back to unpaid."
                    )
            await interaction.response.send_message(message, ephemeral=True)

    async def reprice_unpriced(self):
        conn = self.db()
        rows = conn.execute(
            """
            SELECT id, item_id, item_name, quantity
            FROM deposits
            WHERE status = 'unpaid' AND IFNULL(total, 0) = 0
            """
        ).fetchall()
        conn.close()
        for row in rows:
            item_id = row["item_id"]
            item_name = row["item_name"] or ""
            if not item_id:
                item_id, item_name = self.match_item_id(item_name)
                if not item_id:
                    await self.refresh_item_catalog()
                    item_id, item_name = self.match_item_id(row["item_name"] or "")
            if not item_id:
                continue
            try:
                priced = await self.price_for(item_id, item_name)
            except Exception as exc:
                print(f"[ArmoryPayout] Reprice failed for {item_name}: {exc}")
                continue
            if not priced:
                continue
            total = int(row["quantity"] or 0) * int(priced)
            conn = self.db()
            conn.execute(
                """
                UPDATE deposits
                SET item_id = ?, item_name = ?, unit_price = ?, total = ?
                WHERE id = ? AND status = 'unpaid'
                """,
                (int(item_id), item_name, int(priced), total, row["id"]),
            )
            conn.commit()
            conn.close()
            print(f"[ArmoryPayout] Repriced {item_name} to {format_money(priced)} each.")

    @tasks.loop(minutes=1)
    async def watch_task(self):
        try:
            await self.poll_armory_news()
        except Exception as exc:
            print(f"[ArmoryPayout] News poll failed: {exc}")
        try:
            await self.reprice_unpriced()
        except Exception as exc:
            print(f"[ArmoryPayout] Reprice pass failed: {exc}")
        try:
            await self.verify_due_payouts()
        except Exception as exc:
            print(f"[ArmoryPayout] Payout check failed: {exc}")

    @watch_task.before_loop
    async def before_watch(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=16, tzinfo=datetime.timezone.utc))
    async def digest_task(self):
        channel_id = int(self.settings.get("digest_channel_id") or 0)
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            print(f"[ArmoryPayout] Digest channel {channel_id} was not found.")
            return
        rows = self.unpaid_summary()
        if not rows:
            description = "No unpaid armory deposits."
        else:
            lines = [
                f"• **{row['torn_name']}** [{row['torn_id'] or '?'}] — {format_money(row['owed'])} ({row['deposits']} deposit(s))"
                for row in rows[:40]
            ]
            extra = len(rows) - len(lines)
            description = "\n".join(lines)
            if extra > 0:
                description += f"\n…and {extra} more."
        embed = discord.Embed(title="Armory deposits owed", description=description[:4000], color=discord.Color.gold())
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            print(f"[ArmoryPayout] Digest send failed: {exc}")

    @digest_task.before_loop
    async def before_digest(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="payout", description="Show armory deposit money owed.")
    @app_commands.describe(
        member="Torn name or ID. Ignored unless you have a payout role.",
        clear="Zero this member's unpaid balance. Clear role only.",
    )
    async def payout_command(self, interaction: discord.Interaction, member: str = None, clear: bool = False):
        await interaction.response.defer(ephemeral=True)
        staff = self.is_payout_staff(interaction.user)
        if clear and not self.is_clear_staff(interaction.user):
            if staff:
                return await interaction.followup.send(
                    "You can mark a payout, but you cannot clear a balance.",
                    ephemeral=True,
                )
            clear = False
        if clear:
            if not member:
                return await interaction.followup.send("Choose a member to clear.", ephemeral=True)
            target = self.member_for_query(member)
            if not target:
                return await interaction.followup.send(f"No roster member matched `{member}`.", ephemeral=True)
            torn_id = int(target.get("id") or target.get("user_id"))
            owed, count = self.clear_balance(torn_id)
            return await interaction.followup.send(
                f"Cleared {count} deposit(s) for **{target.get('name')}** [{torn_id}], "
                f"removing {format_money(owed)}.",
                ephemeral=True,
            )
        if staff and member:
            target = self.member_for_query(member)
            if not target:
                return await interaction.followup.send(f"No roster member matched `{member}`.", ephemeral=True)
        elif staff and not member:
            rows = [row for row in self.unpaid_summary() if int(row["owed"] or 0) > 0]
            self_member = self.member_for_discord(interaction.user)
            self_id = None
            if self_member:
                self_id = int(self_member.get("id") or self_member.get("user_id") or 0)
            mine = next((row for row in rows if self_id and int(row["torn_id"] or 0) == self_id), None)
            others = [row for row in rows if not (self_id and int(row["torn_id"] or 0) == self_id)]
            lines = []
            if mine:
                lines.append(
                    f"**You:** **{mine['torn_name']}** [{mine['torn_id']}] — {format_money(mine['owed'])}"
                )
            elif not self_member:
                lines.append("Your Torn account is not linked on the roster, so your own total is not shown.")
            if others:
                if lines:
                    lines.append("")
                lines.append("**Others owed**")
                lines.extend(
                    f"• **{row['torn_name']}** [{row['torn_id'] or '?'}] — {format_money(row['owed'])}"
                    for row in others[:40]
                )
            if not mine and not others:
                return await interaction.followup.send("Nobody is owed a payout.", ephemeral=True)
            lines.append("\nUse `/payout member:` to see the items and mark one paid.")
            return await interaction.followup.send("\n".join(lines), ephemeral=True)
        else:
            target = self.member_for_discord(interaction.user)
            if not target:
                return await interaction.followup.send(
                    "I don't have your Torn account linked on the roster, so I can't look up what you are owed.",
                    ephemeral=True,
                )
        torn_id = int(target.get("id") or target.get("user_id"))
        unpaid = self.rows_for(torn_id=torn_id, status="unpaid")
        pending = self.rows_for(torn_id=torn_id, status="pending")
        text, owed = self.describe_member(target, unpaid, pending)
        view = None
        if staff and (owed > 0 or pending):
            view = ui.View(timeout=None)

            async def mark(button_interaction, torn_id=torn_id):
                await self.mark_paid(button_interaction, torn_id)

            async def done(button_interaction, torn_id=torn_id):
                await self.confirm_done(button_interaction, torn_id)

            if owed > 0 and not pending:
                mark_button = ui.Button(
                    label="Mark paid",
                    style=discord.ButtonStyle.green,
                    custom_id=f"armorypay:paid:{torn_id}",
                )
                mark_button.callback = mark
                view.add_item(mark_button)
            done_button = ui.Button(
                label="Done",
                style=discord.ButtonStyle.blurple,
                custom_id=f"armorypay:done:{torn_id}",
            )
            done_button.callback = done
            view.add_item(done_button)
        if view is None:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(text, view=view, ephemeral=True)

    @payout_command.autocomplete("member")
    async def payout_member(self, interaction: discord.Interaction, current: str):
        roster = self.get_roster()
        if not roster:
            return []
        query = current.lower().strip()
        choices = []
        for member in roster.all_members():
            name = member.get("name") or ""
            if query and query not in name.lower() and query not in str(member.get("id") or ""):
                continue
            choices.append(app_commands.Choice(name=name[:100], value=str(member.get("id") or member.get("user_id"))))
            if len(choices) >= 25:
                break
        return choices

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if interaction.response.is_done():
            return
        try:
            torn_id = int(custom_id.rsplit(":", 1)[-1])
        except ValueError:
            return
        if custom_id.startswith("armorypay:paid:"):
            await self.mark_paid(interaction, torn_id)
        elif custom_id.startswith("armorypay:done:"):
            await self.confirm_done(interaction, torn_id)

    def web_tables(self):
        rows = []
        for row in self.unpaid_summary():
            rows.append(
                {
                    "Member": f"{row['torn_name']} ({row['torn_id'] or '?'})",
                    "Owed": format_money(row["owed"]),
                    "Deposits": row["deposits"],
                }
            )
        return [{"title": "Unpaid armory deposits", "columns": ["Member", "Owed", "Deposits"], "rows": rows}]


async def setup(bot: commands.Bot):
    await bot.add_cog(ArmoryPayout(bot))
