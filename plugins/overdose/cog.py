import datetime
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

NEWS_RE = re.compile(r"(?P<name>.+?)\s*(?:\[(?P<tid>\d+)\])?\s+overdosed on\s+(?P<drug>[^.]+)", re.I)


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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(od_counts)").fetchall()}
        if "lifetime_fetched" not in cols:
            conn.execute("ALTER TABLE od_counts ADD COLUMN lifetime_fetched INTEGER NOT NULL DEFAULT 0")
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
            return {}
        url = f"https://api.torn.com/faction/?selections=mainnews&key={TORN_API_KEY}&comment=StrikeBot"
        headers = {"User-Agent": "StrikeBot/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get("error"):
            print(f"[Overdose] API error: {data['error']}")
            return {}
        return data.get("mainnews") or data.get("news") or {}

    def parse_news_item(self, news_id, item):
        text = str(item.get("news") or item.get("text") or item)
        match = NEWS_RE.search(text)
        if not match:
            if "overdos" not in text.lower():
                return None
            tid = None
            name = "Unknown"
        else:
            name = match.group("name").strip()
            tid = match.group("tid")
            drug = match.group("drug").strip()
        if match:
            drug = match.group("drug").strip()
        else:
            drug = "a drug"
        if not tid:
            roster = self.bot.get_cog("FactionRoster")
            member = roster.get_member_by_name(name) if roster else None
            if member:
                tid = member.get("id") or member.get("user_id")
                name = member.get("name") or name
        if not tid:
            return None
        return {
            "news_id": str(news_id),
            "user_id": int(tid),
            "name": name,
            "drug": drug,
            "timestamp": item.get("timestamp") if isinstance(item, dict) else None,
        }

    async def post_od(self, entry, year_count, total):
        channel = self.enabled_channel()
        if not channel:
            return
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

    @tasks.loop(minutes=5)
    async def check_task(self):
        if not self.enabled_channel():
            return
        try:
            news = await self.fetch_news()
        except Exception as exc:
            print(f"[Overdose] Fetch failed: {exc}")
            return
        items = list(news.items()) if isinstance(news, dict) else list(enumerate(news))
        conn = self.db()
        seen_count = conn.execute("SELECT COUNT(*) FROM od_seen").fetchone()[0]
        seed_only = seen_count == 0
        for news_id, item in items:
            if not isinstance(item, dict):
                continue
            parsed = self.parse_news_item(news_id, item)
            if not parsed:
                continue
            seen = conn.execute("SELECT 1 FROM od_seen WHERE news_id = ?", (parsed["news_id"],)).fetchone()
            if seen:
                continue
            conn.execute(
                "INSERT INTO od_seen (news_id, user_id, seen_at) VALUES (?, ?, ?)",
                (parsed["news_id"], parsed["user_id"], utc_now().isoformat()),
            )
            conn.commit()
            if seed_only:
                continue
            year_count, total = self.bump_counts(parsed["user_id"], parsed["name"])
            try:
                await self.post_od(parsed, year_count, total)
            except discord.HTTPException as exc:
                print(f"[Overdose] Send failed: {exc}")
        conn.close()

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    def web_tables(self):
        conn = self.db()
        counts = conn.execute(
            "SELECT user_id, name, year_count, total, lifetime_fetched FROM od_counts ORDER BY total DESC, name"
        ).fetchall()
        comments = conn.execute("SELECT id, text FROM od_comments ORDER BY id").fetchall()
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
                    print(f"[Overdose] lifetime fetch failed for {user_id}: {exc}")
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
