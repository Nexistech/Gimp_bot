import datetime
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import aiohttp
import discord
from discord.ext import commands, tasks

sys.path.append(os.getcwd())

from plugin_log import get_logger
log = get_logger("overdose")
from plugin_settings import load_root_settings, load_settings, save_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "overdose.db")

DEFAULT_COMMENTS = [
    "Again? The liver called. It's filing a complaint.",
    "That's not a lifestyle. That's a speedrun.",
    "Science is baffled. The bartenders are not.",
    "Achievement unlocked: still standing, somehow.",
    "The hospital staff have a punch card with their name on it.",
    "One more and they qualify for frequent flyer miles.",
    "Legend says the Xanax bottle flinched first.",
    "This is why we can't have nice factions.",
    "Their blood type is now 'questionable'.",
    "The tutorial on 'maybe don't' remains unplayed.",
    "Another one for the scrapbook of poor decisions.",
    "They collected the whole set. Again.",
    "Even the OD screen looks tired.",
    "Respect the commitment. Question the strategy.",
    "The faction upgrade did not cover this.",
    "Somewhere a nurse just sighed in Torn.",
    "Peak performance. Of something.",
    "They really said 'watch this' and meant it.",
    "That's a personality at this point.",
    "The cooldown is longer than the self-reflection.",
    "Bold of them to assume the pills were the problem.",
    "A cautionary tale with a loyalty bonus.",
    "The high score board just updated.",
    "Not the glow-up anyone asked for.",
    "Please clap. Or call a medic. Either works.",
    "They speedran the waiting room.",
    "Consistency is a virtue. Allegedly.",
    "The drug said no. They said yes anyway.",
    "New personal best. Old common sense.",
    "This is how side quests start.",
    "Faction chat will never let this go.",
    "They treated 'recommended dose' as a dare.",
    "The stat sheet needed more flavor text.",
    "Hospital gown season is year-round for them.",
    "A toast. Water only.",
    "The algorithm predicted this. So did everyone else.",
    "They put the 'over' in overdose.",
    "If this were a job they'd be employee of the month.",
    "The Xanax has a restraining order pending.",
    "Another day, another 'I'm fine'.",
    "That's not built different. That's built stubborn.",
    "The revive list just got a calendar invite.",
    "They mainlined the plot twist.",
    "Skill issue, medically speaking.",
    "The faction armory sent thoughts and prayers.",
    "Recorded for training and amusement purposes.",
    "They found the secret difficulty setting.",
    "A monument to optimism and bad math.",
    "The cooldown timer filed for overtime.",
    "This one goes on the fridge. Face down.",
]

DRUG_FIELDS = (
    ("cannabis", "Cannabis"),
    ("ecstasy", "Ecstasy"),
    ("ketamine", "Ketamine"),
    ("lsd", "LSD"),
    ("opium", "Opium"),
    ("pcp", "PCP"),
    ("shrooms", "Shrooms"),
    ("speed", "Speed"),
    ("vicodin", "Vicodin"),
    ("xanax", "Xanax"),
)
# Ketamine's minimum cooldown is 45 minutes, the shortest drug cooldown.
# A full roster pass inside 30 minutes means only one drug can have been used.
SWEEP_MINUTES = 30
NEWS_RE = re.compile(
    r"(?P<name>.+?)\s*(?:\[(?P<tid>\d+)\])?\s+overdosed on\s+(?P<drug>[^.]+)",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
XID_RE = re.compile(r"XID=(\d+)", re.I)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class OverdoseMonitor(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "channel_id",
            "type": "int",
            "label": "Overdose channel (Disabled turns the module off)",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "check_minutes",
            "type": "int",
            "label": "Check interval (minutes)",
            "default": 5,
        },
        {
            "key": "include_comment",
            "type": "bool",
            "label": "Append a random snarky comment",
            "default": True,
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.initialize_db()
        self._pulling = False
        self._pull_done = 0
        self._pull_total = 0
        self.check_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        minutes = max(1, int(self.settings.get("check_minutes") or 5))
        self.check_task.change_interval(minutes=minutes)

    def cog_unload(self):
        self.check_task.cancel()

    def db(self):
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize_db(self):
        conn = self.db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS od_counts (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                total INTEGER NOT NULL DEFAULT 0,
                year INTEGER NOT NULL DEFAULT 0,
                year_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS od_seen (
                news_id TEXT PRIMARY KEY,
                user_id INTEGER,
                seen_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS od_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS od_drug_odds (
                drug TEXT PRIMARY KEY,
                uses INTEGER NOT NULL DEFAULT 0,
                ods INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS od_user_odds (
                user_id INTEGER NOT NULL,
                drug TEXT NOT NULL,
                name TEXT,
                uses INTEGER NOT NULL DEFAULT 0,
                ods INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, drug)
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(od_counts)").fetchall()}
        if "lifetime_fetched" not in cols:
            conn.execute("ALTER TABLE od_counts ADD COLUMN lifetime_fetched INTEGER NOT NULL DEFAULT 0")
        if "api_total" not in cols:
            conn.execute("ALTER TABLE od_counts ADD COLUMN api_total INTEGER")
        if "stats_json" not in cols:
            conn.execute("ALTER TABLE od_counts ADD COLUMN stats_json TEXT")
        if "checked_at" not in cols:
            conn.execute("ALTER TABLE od_counts ADD COLUMN checked_at INTEGER")
        existing = conn.execute("SELECT COUNT(*) FROM od_comments").fetchone()[0]
        if existing == 0:
            conn.executemany("INSERT INTO od_comments (text) VALUES (?)", [(c,) for c in DEFAULT_COMMENTS])
        conn.commit()
        conn.close()

    def enabled_channel(self):
        channel_id = int(self.settings.get("channel_id") or 0)
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)

    def random_comment(self):
        conn = self.db()
        rows = conn.execute("SELECT text FROM od_comments").fetchall()
        conn.close()
        if not rows:
            return ""
        return random.choice([row["text"] for row in rows])

    def bump_counts(self, user_id, name):
        year = utc_now().year
        conn = self.db()
        row = conn.execute("SELECT * FROM od_counts WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO od_counts (user_id, name, total, year, year_count) VALUES (?, ?, 1, ?, 1)",
                (user_id, name, year),
            )
            total, year_count = 1, 1
        else:
            total = int(row["total"]) + 1
            year_count = int(row["year_count"]) + 1 if int(row["year"]) == year else 1
            conn.execute(
                "UPDATE od_counts SET name = ?, total = ?, year = ?, year_count = ? WHERE user_id = ?",
                (name or row["name"], total, year, year_count, user_id),
            )
        conn.commit()
        conn.close()
        return year_count, total

    async def fetch_news(self):
        if not TORN_API_KEY:
            return []
        url = (
            "https://api.torn.com/v2/faction/news?cat=main&striptags=false&limit=100"
            f"&key={TORN_API_KEY}&comment=GimpBot"
        )
        headers = {"User-Agent": "GimpBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            log.error(f"[Overdose] API error: {data['error']}")
            return []
        news = data.get("news") if isinstance(data, dict) else None
        if isinstance(news, list):
            return news
        if isinstance(news, dict):
            return [{"id": key, **value} if isinstance(value, dict) else {"id": key, "text": value} for key, value in news.items()]
        return []

    def parse_news_item(self, news_id, item):
        if not isinstance(item, dict):
            return None
        raw = str(item.get("news") or item.get("text") or "")
        plain = " ".join(TAG_RE.sub(" ", raw).split())
        match = NEWS_RE.search(plain)
        if not match:
            if "overdos" in plain.lower():
                log.warning(f"[Overdose] Could not parse: {plain[:180]}")
            return None
        name = match.group("name").strip(" -")
        drug = match.group("drug").strip()
        xid = XID_RE.search(raw)
        tid = match.group("tid") or (xid.group(1) if xid else None)
        if not tid:
            roster = self.bot.get_cog("FactionRoster")
            member = roster.get_member_by_name(name) if roster else None
            if member:
                tid = member.get("id") or member.get("user_id")
                name = member.get("name") or name
        if not tid:
            log.warning(f"[Overdose] No Torn ID for {plain[:180]}")
            return None
        return {
            "news_id": str(item.get("id") or news_id),
            "user_id": int(tid),
            "name": name,
            "drug": drug,
            "timestamp": int(item.get("timestamp") or 0),
        }

    async def post_od(self, channel, entry, year_count, total):
        profile = f"https://www.torn.com/profiles.php?XID={entry['user_id']}"
        year = utc_now().year
        desc = (
            f"**[{entry['name']}]({profile})** overdosed on **{entry['drug']}**.\n"
            f"That's **{year_count}** this year ({year}) and **{total}** all time."
        )
        if self.settings.get("include_comment"):
            comment = self.random_comment()
            if comment:
                desc += f"\n\n*{comment}*"
        embed = discord.Embed(title="Faction overdose", description=desc, color=discord.Color.purple(), timestamp=utc_now())
        embed.set_footer(text=f"Torn ID {entry['user_id']}")
        await channel.send(embed=embed)

    def drug_label(self, old_stats, new_stats):
        increases = []
        for key, label in DRUG_FIELDS:
            if key == "opium":
                continue
            delta = int(new_stats.get(key) or 0) - int((old_stats or {}).get(key) or 0)
            if delta > 0:
                increases.append(label if delta == 1 else f"{label} x{delta}")
        if len(increases) == 1:
            return increases[0]
        if increases:
            return " or ".join(increases)
        return "a drug"

    def note_drug_sample(self, conn, user_id, name, old_stats, new_stats, od_delta):
        """Count a window only when one drug was used exactly once."""
        if not old_stats or od_delta < 0 or od_delta > 1:
            return
        increases = []
        for key, label in DRUG_FIELDS:
            delta = int(new_stats.get(key) or 0) - int(old_stats.get(key) or 0)
            if delta > 0:
                increases.append((key, label, delta))
        if len(increases) != 1 or increases[0][2] != 1:
            return
        key, label, _delta = increases[0]
        if key == "opium" and od_delta:
            return
        ods = 1 if od_delta else 0
        conn.execute(
            """
            INSERT INTO od_drug_odds (drug, uses, ods) VALUES (?, 1, ?)
            ON CONFLICT(drug) DO UPDATE SET
                uses = uses + 1,
                ods = ods + excluded.ods
            """,
            (label, ods),
        )
        conn.execute(
            """
            INSERT INTO od_user_odds (user_id, drug, name, uses, ods) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(user_id, drug) DO UPDATE SET
                name = excluded.name,
                uses = uses + 1,
                ods = ods + excluded.ods
            """,
            (int(user_id), label, name, ods),
        )

    async def fetch_drug_stats(self, session, user_id):
        url = (
            "https://api.torn.com/v2/user/"
            f"{int(user_id)}/personalstats?cat=drugs&key={TORN_API_KEY}&comment=GimpBot"
        )
        async with session.get(url, timeout=20) as response:
            data = await response.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])
        drugs = (data.get("personalstats") or {}).get("drugs") or {}
        if "overdoses" not in drugs:
            raise RuntimeError("drugs.overdoses missing")
        return drugs

    async def pull_stats(self, force=False):
        if self._pulling:
            return
        self._pulling = True
        try:
            await self._run_pull(force)
        finally:
            self._pulling = False

    async def _run_pull(self, force=False):
        channel = self.enabled_channel()
        if channel is None:
            channel_id = int(self.settings.get("channel_id") or 0)
            if channel_id:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException as exc:
                    log.warning(f"[Overdose] Channel {channel_id} is not available: {exc}")
        roster = self.bot.get_cog("FactionRoster")
        if not roster or not roster.members_by_id:
            log.warning("[Overdose] Roster is empty, so no drug stats were pulled.")
            return
        members = []
        for member in roster.all_members():
            try:
                user_id = int(member.get("id") or member.get("user_id"))
            except (TypeError, ValueError):
                continue
            members.append((user_id, member.get("name") or str(user_id)))
        members.sort(key=lambda item: item[0])
        if not members:
            return
        interval = max(1, int(self.settings.get("check_minutes") or 5))
        batch = min(20, max(1, -(-len(members) * interval // SWEEP_MINUTES)))
        now_ts = int(utc_now().timestamp())
        due_before = now_ts - (SWEEP_MINUTES * 60)
        conn = self.db()
        known = {
            int(row["user_id"]): row["checked_at"]
            for row in conn.execute("SELECT user_id, checked_at FROM od_counts")
        }
        conn.close()
        due = []
        for user_id, name in members:
            checked_at = known.get(user_id)
            if force or checked_at is None or int(checked_at) <= due_before:
                due.append((int(checked_at or 0), user_id, name))
        due.sort()
        if force:
            picked = [(user_id, name) for _checked, user_id, name in due]
        else:
            picked = [(user_id, name) for _checked, user_id, name in due[:batch]]
        if not picked:
            return
        sweep_minutes = len(members) / batch * interval
        if sweep_minutes > 45:
            log.warning(
                f"[Overdose] Roster sweep is about {sweep_minutes:.0f} min, "
                "longer than the 45 min ketamine cooldown."
            )
        log.info(f"[Overdose] {len(due)} member(s) due, checking {len(picked)}")
        self._pull_total = len(picked)
        self._pull_done = 0
        headers = {"User-Agent": "GimpBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            for index, (user_id, name) in enumerate(picked, start=1):
                self._pull_done = index
                try:
                    stats = await self.fetch_drug_stats(session, user_id)
                except Exception as exc:
                    log.error(f"[Overdose] Stat lookup failed for {name} [{user_id}]: {exc}")
                    await asyncio.sleep(0.4)
                    continue
                count = int(stats.get("overdoses") or 0)
                conn = self.db()
                row = conn.execute("SELECT * FROM od_counts WHERE user_id = ?", (user_id,)).fetchone()
                previous = None if row is None or row["api_total"] is None else int(row["api_total"])
                old_stats = None
                if row and row["stats_json"]:
                    try:
                        old_stats = json.loads(row["stats_json"])
                    except (TypeError, ValueError):
                        old_stats = None
                pulled_at = int(utc_now().timestamp())
                blob = json.dumps({key: int(stats.get(key) or 0) for key, _label in DRUG_FIELDS})
                if previous is None or old_stats is None:
                    year = utc_now().year
                    year_count = int(row["year_count"]) if row else 0
                    stored_total = count if row is None else int(row["total"] or count)
                    conn.execute(
                        """
                        INSERT INTO od_counts
                            (user_id, name, total, year, year_count, lifetime_fetched, api_total, stats_json, checked_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            name=excluded.name,
                            api_total=excluded.api_total,
                            stats_json=excluded.stats_json,
                            checked_at=excluded.checked_at,
                            lifetime_fetched=1
                        """,
                        (user_id, name, stored_total, year, year_count, count, blob, pulled_at),
                    )
                    conn.commit()
                    conn.close()
                    await asyncio.sleep(0.4)
                    continue
                conn.execute(
                    "UPDATE od_counts SET name = ?, api_total = ?, stats_json = ?, checked_at = ? WHERE user_id = ?",
                    (name, count, blob, pulled_at, user_id),
                )
                if count < previous:
                    conn.commit()
                    conn.close()
                    await asyncio.sleep(0.4)
                    continue
                od_delta = count - previous
                self.note_drug_sample(conn, user_id, name, old_stats, stats, od_delta)
                if od_delta == 0:
                    conn.commit()
                    conn.close()
                    await asyncio.sleep(0.4)
                    continue
                delta = od_delta
                year = utc_now().year
                if int(row["year"]) == year:
                    year_count = int(row["year_count"]) + delta
                else:
                    year_count = delta
                conn.execute(
                    """
                    UPDATE od_counts
                    SET total = ?, year = ?, year_count = ?, lifetime_fetched = 1
                    WHERE user_id = ?
                    """,
                    (count, year, year_count, user_id),
                )
                conn.commit()
                conn.close()
                drug = self.drug_label(old_stats, stats)
                if delta > 1 and " or " not in drug and not drug.endswith("x" + str(delta)):
                    drug = f"{drug} x{delta}" if drug != "a drug" else f"a drug x{delta}"
                entry = {"user_id": user_id, "name": name, "drug": drug}
                if channel is None:
                    log.warning(f"[Overdose] {name} overdosed on {drug}, but no channel is configured.")
                else:
                    try:
                        await self.post_od(channel, entry, year_count, count)
                    except discord.HTTPException as exc:
                        log.error(f"[Overdose] Send failed: {exc}")
                await asyncio.sleep(0.4)
                self._pull_done = index

    @tasks.loop(minutes=5)
    async def check_task(self):
        await self.pull_stats(force=False)

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        roster = self.bot.get_cog("FactionRoster")
        if roster:
            await roster.wait_until_populated()

    def web_tables(self):
        conn = self.db()
        counts = conn.execute(
            "SELECT user_id, name, year_count, total, lifetime_fetched, api_total, stats_json, checked_at FROM od_counts ORDER BY name"
        ).fetchall()
        comments = conn.execute("SELECT id, text FROM od_comments ORDER BY id").fetchall()
        odds = {
            row["drug"]: row
            for row in conn.execute("SELECT drug, uses, ods FROM od_drug_odds")
        }
        user_odds = conn.execute(
            "SELECT user_id, name, drug, uses, ods FROM od_user_odds WHERE uses > 0 ORDER BY name, drug"
        ).fetchall()
        conn.close()
        count_rows = [
            {
                "id": row["user_id"],
                "Member": row["name"] or row["user_id"],
                "This year": row["year_count"],
                "All time": row["total"],
                "API seeded": "yes" if row["lifetime_fetched"] else "no",
            }
            for row in counts
        ]
        stored = {int(row["user_id"]): row for row in counts}
        roster = self.bot.get_cog("FactionRoster")
        people = []
        if roster:
            for member in roster.all_members():
                try:
                    user_id = int(member.get("id") or member.get("user_id"))
                except (TypeError, ValueError):
                    continue
                people.append((user_id, member.get("name") or str(user_id)))
        if not people:
            people = [(int(row["user_id"]), row["name"] or row["user_id"]) for row in counts]
        people.sort(key=lambda item: str(item[1]).lower())
        drug_columns = ["Member", "Overdoses"] + [label for _key, label in DRUG_FIELDS] + ["Total taken", "Last pull"]
        drug_rows = []
        for user_id, name in people:
            row = stored.get(user_id)
            try:
                stats = json.loads(row["stats_json"]) if row and row["stats_json"] else None
            except (TypeError, ValueError):
                stats = None
            entry = {"Member": name}
            if row is None or row["api_total"] is None:
                entry["Overdoses"] = ""
            else:
                entry["Overdoses"] = int(row["api_total"])
            taken = 0
            for key, label in DRUG_FIELDS:
                if not stats:
                    entry[label] = ""
                else:
                    entry[label] = int(stats.get(key) or 0)
                    taken += entry[label]
            entry["Total taken"] = "" if not stats else taken
            checked_at = row["checked_at"] if row else None
            if checked_at:
                entry["Last pull"] = datetime.datetime.fromtimestamp(
                    int(checked_at), datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
            else:
                entry["Last pull"] = ""
            drug_rows.append(entry)
        odds_rows = []
        for _key, label in DRUG_FIELDS:
            row = odds.get(label)
            uses = int(row["uses"]) if row else 0
            ods = int(row["ods"]) if row else 0
            odds_rows.append(
                {
                    "Drug": label,
                    "Uses": uses,
                    "Overdoses": ods,
                    "Chance %": "" if uses <= 0 else round(ods / uses * 100, 4),
                }
            )
        member_totals = {}
        member_drug_rows = []
        for row in user_odds:
            name = row["name"] or row["user_id"]
            uses = int(row["uses"] or 0)
            ods = int(row["ods"] or 0)
            member_drug_rows.append(
                {
                    "Member": name,
                    "Drug": row["drug"],
                    "Uses": uses,
                    "Overdoses": ods,
                    "Chance %": "" if uses <= 0 else round(ods / uses * 100, 4),
                }
            )
            bucket = member_totals.setdefault(name, {"uses": 0, "ods": 0})
            bucket["uses"] += uses
            bucket["ods"] += ods
        member_rows = [
            {
                "Member": name,
                "Uses": bucket["uses"],
                "Overdoses": bucket["ods"],
                "Chance %": "" if bucket["uses"] <= 0 else round(bucket["ods"] / bucket["uses"] * 100, 4),
            }
            for name, bucket in sorted(member_totals.items(), key=lambda item: item[0].lower())
        ]
        comment_rows = [{"id": row["id"], "Comment": row["text"]} for row in comments]
        count_form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="set_counts">
          <label>Torn ID</label><input name="user_id" placeholder="Torn ID">
          <label>All-time overdoses</label><input name="total" placeholder="327">
          <label>This year</label><input name="year_count" placeholder="37">
          <button type="submit">Save counts</button>
        </form>
        """
        comment_form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="add_comment">
          <label>New comment</label>
          <textarea name="text" rows="6" placeholder="One comment per line"></textarea>
          <button type="submit">Add comment</button>
        </form>
        """
        return [
            {
                "title": "Drug use",
                "note": "Everyone in the roster is listed. A dash means that member has not been pulled yet. Last pull is UTC. Click a heading to sort.",
                "sortable": True,
                "columns": drug_columns,
                "rows": drug_rows,
                "add_form": self.pull_form(),
            },
            {
                "title": "Overdose chance",
                "note": "Faction-wide, from windows where a member took exactly one dose of one drug. 1 overdose in 1,000 Xanax uses shows 0.1. History from before the bot was watching is not included.",
                "sortable": True,
                "columns": ["Drug", "Uses", "Overdoses", "Chance %"],
                "rows": odds_rows,
            },
            {
                "title": "Member overdose chance",
                "note": "Same single-dose windows, totaled per member across every drug.",
                "sortable": True,
                "columns": ["Member", "Uses", "Overdoses", "Chance %"],
                "rows": member_rows,
            },
            {
                "title": "Member chance by drug",
                "note": "Filter the Member or Drug column to narrow this down. Chance % is overdoses divided by uses.",
                "sortable": True,
                "columns": ["Member", "Drug", "Uses", "Overdoses", "Chance %"],
                "rows": member_drug_rows,
            },
            {
                "title": "Stored overdose counts",
                "columns": ["Member", "This year", "All time", "API seeded"],
                "rows": count_rows,
                "row_actions": [{"action": "set_counts_row", "label": "Save", "include_min": False}],
                "add_form": count_form
                + """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="fetch_lifetimes">
          <p class="help">Pulls public personalstats.overdosed for roster members who have never been seeded. Already-seeded IDs are skipped.</p>
          <button type="submit">Fetch all-time OD stats for unseeded members</button>
        </form>
        """,
            },
            {
                "title": "Snark comments",
                "columns": ["Comment"],
                "rows": comment_rows,
                "remove_action": "delete_comment",
                "add_form": comment_form,
            },
        ]

    def pull_form(self):
        nxt = getattr(self.check_task, "next_iteration", None)
        next_ts = int(nxt.timestamp()) if nxt else 0
        return f"""
        <p class="help">Next automatic pull <strong id="od-countdown" data-next="{next_ts}">...</strong></p>
        <div id="od-progress" class="progress-wrap" hidden>
          <div class="progress-bar"><span id="od-progress-fill"></span></div>
          <p class="help" id="od-progress-label">0/0</p>
        </div>
        <form id="od-pull-form" method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="pull_now">
          <button type="submit" id="od-pull-now">Pull now</button>
        </form>
        <script>
        (function () {{
          const el = document.getElementById("od-countdown");
          const next = Number((el && el.dataset.next) || 0);
          function tick() {{
            if (!el) return;
            if (!next) {{ el.textContent = "is not scheduled yet"; return; }}
            const sec = Math.max(0, next - Math.floor(Date.now() / 1000));
            if (sec <= 0) {{ el.textContent = "is due now"; return; }}
            const m = Math.floor(sec / 60);
            const s = String(sec % 60).padStart(2, "0");
            el.textContent = "in " + m + "m " + s + "s";
          }}
          tick();
          setInterval(tick, 1000);

          const form = document.getElementById("od-pull-form");
          const wrap = document.getElementById("od-progress");
          const fill = document.getElementById("od-progress-fill");
          const label = document.getElementById("od-progress-label");
          const button = document.getElementById("od-pull-now");
          let poll = null;
          function paint(data) {{
            const total = Number(data.total || 0);
            const done = Number(data.done || 0);
            const running = !!data.running;
            if (!running && !total) return;
            wrap.hidden = false;
            const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
            fill.style.width = pct + "%";
            label.textContent = done + "/" + total + (running ? "" : " done — refresh to load the new numbers");
            return running;
          }}
          async function refreshProgress() {{
            const res = await fetch("/api/overdose-pull", {{credentials: "same-origin"}});
            const data = await res.json();
            const running = paint(data);
            if (!running && poll) {{
              clearInterval(poll);
              poll = null;
              button.disabled = false;
            }}
          }}
          function startPoll() {{
            if (poll) return;
            poll = setInterval(refreshProgress, 1000);
            refreshProgress();
          }}
          form.addEventListener("submit", async (ev) => {{
            ev.preventDefault();
            button.disabled = true;
            wrap.hidden = false;
            label.textContent = "Starting…";
            fill.style.width = "0%";
            const body = new URLSearchParams(new FormData(form));
            await fetch(form.action, {{
              method: "POST",
              credentials: "same-origin",
              headers: {{"X-Requested-With": "fetch"}},
              body
            }});
            startPoll();
          }});
          fetch("/api/overdose-pull", {{credentials: "same-origin"}})
            .then((res) => res.json())
            .then((data) => {{ if (data.running) {{ button.disabled = true; startPoll(); }} }});
        }})();
        </script>
        """

    def fetch_lifetime(self, user_id):
        if not TORN_API_KEY:
            raise RuntimeError("TORN_API_KEY missing")
        qs = urllib.parse.urlencode(
            {
                "selections": "personalstats",
                "stat": "overdosed",
                "key": TORN_API_KEY,
                "comment": "StrikeBot",
            }
        )
        url = f"https://api.torn.com/user/{int(user_id)}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "StrikeBot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("error"):
            raise RuntimeError(data["error"])
        stats = data.get("personalstats") or {}
        if isinstance(stats, dict) and "overdosed" in stats:
            return int(stats.get("overdosed") or 0)
        if isinstance(stats, list):
            for item in stats:
                if str(item.get("name") or "").lower() == "overdosed":
                    return int(item.get("value") or 0)
        return int(stats.get("overdosed") or 0)

    def web_action(self, data):
        action = data.get("action")
        if action == "pull_now":
            if self._pulling:
                return "A pull is already running."
            asyncio.get_running_loop().create_task(self.pull_stats(force=True))
            return "Pull started for the whole roster. Refresh in a minute to see Last pull fill in."
        conn = self.db()
        if action == "fetch_lifetimes":
            conn.close()
            roster = self.bot.get_cog("FactionRoster")
            if not roster:
                return "Roster is not loaded."
            conn = self.db()
            fetched_ids = {
                int(row["user_id"])
                for row in conn.execute("SELECT user_id FROM od_counts WHERE lifetime_fetched = 1")
            }
            conn.close()
            pending = []
            for member in roster.all_members():
                user_id = int(member.get("id") or member.get("user_id"))
                if user_id in fetched_ids:
                    continue
                pending.append((user_id, member.get("name") or str(user_id)))
            updated, failed = 0, 0
            year = utc_now().year
            for user_id, name in pending:
                try:
                    total = self.fetch_lifetime(user_id)
                except Exception as exc:
                    log.error(f"[Overdose] lifetime fetch failed for {user_id}: {exc}")
                    failed += 1
                    time.sleep(0.4)
                    continue
                conn = self.db()
                row = conn.execute("SELECT year_count FROM od_counts WHERE user_id = ?", (user_id,)).fetchone()
                year_count = int(row["year_count"]) if row else 0
                conn.execute(
                    """
                    INSERT INTO od_counts (user_id, name, total, year, year_count, lifetime_fetched)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name=excluded.name,
                        total=excluded.total,
                        lifetime_fetched=1
                    """,
                    (user_id, name, total, year, year_count),
                )
                conn.commit()
                conn.close()
                updated += 1
                time.sleep(0.4)
            return f"Seeded all-time OD stats for {updated} member(s). Skipped {len(fetched_ids)} already seeded. Failed {failed}."
        if action == "set_counts":
            user_id = int(data.get("user_id"))
            total = int(data.get("total") or 0)
            year_count = int(data.get("year_count") or 0)
            roster = self.bot.get_cog("FactionRoster")
            member = roster.get_member(user_id) if roster else None
            name = member.get("name") if member else str(user_id)
            conn.execute(
                """
                INSERT INTO od_counts (user_id, name, total, year, year_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET total=excluded.total, year_count=excluded.year_count, year=excluded.year, name=excluded.name
                """,
                (user_id, name, total, utc_now().year, year_count),
            )
            conn.commit()
            conn.close()
            return f"Saved counts for {name}."
        if action == "delete_comment":
            conn.execute("DELETE FROM od_comments WHERE id = ?", (int(data.get("id")),))
            conn.commit()
            conn.close()
            return "Comment removed."
        if action == "add_comment":
            lines = [line.strip() for line in str(data.get("text") or "").splitlines() if line.strip()]
            if not lines:
                conn.close()
                return "Comment was empty."
            conn.executemany("INSERT INTO od_comments (text) VALUES (?)", [(line,) for line in lines])
            conn.commit()
            conn.close()
            return f"Added {len(lines)} comment(s)."
        conn.close()
        return "Unknown action."


async def setup(bot: commands.Bot):
    await bot.add_cog(OverdoseMonitor(bot))
