"""Watchdog — process / cog / task health.

Slash group (only exists while this cog is loaded):
    /check overview   short summary
    /check plugins    plugin list, split if needed
    /check all        summary plus plugins, tasks, and problems, split across messages
"""

import datetime
import os
import sys
import traceback

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.append(os.getcwd())
from plugin_settings import enabled_plugins, load_root_settings, load_settings, plugin_folders, schema_defaults

PLUGIN_DIR = os.path.dirname(__file__)
STARTED_AT = datetime.datetime.now(datetime.timezone.utc)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def fmt_dt(value):
    if value is None:
        return "never"
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def fmt_age(value):
    if value is None:
        return "unknown"
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        seconds = int((utc_now() - value).total_seconds())
    else:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return str(value)
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


class Watchdog(commands.Cog):
    check = app_commands.Group(name="check", description="Bot and plugin health")

    SETTINGS_SCHEMA = [
        {
            "key": "stale_roster_minutes",
            "type": "int",
            "label": "Roster is stale after this many minutes",
            "default": 30,
        },
        {
            "key": "alert_channel_id",
            "type": "int",
            "label": "Channel for hang/crash alerts (Disabled = 0)",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "check_minutes",
            "type": "int",
            "label": "How often to scan tasks",
            "default": 5,
        },
        {
            "key": "restart_dead_tasks",
            "type": "bool",
            "label": "Try to restart a background task that has stopped",
            "default": False,
        },
    ]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.last_scan = None
        self.last_problems = []
        self.scan_task.start()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        minutes = max(1, int(self.settings.get("check_minutes") or 5))
        self.scan_task.change_interval(minutes=minutes)

    def cog_unload(self):
        self.scan_task.cancel()

    def collect_loops(self):
        found = []
        for cog_name, cog in self.bot.cogs.items():
            for attr, value in cog.__dict__.items():
                if isinstance(value, tasks.Loop):
                    found.append((cog_name, attr, value))
        return found

    def loop_status(self, loop):
        running = bool(loop.is_running())
        failed = bool(loop.failed())
        nxt = getattr(loop, "next_iteration", None)
        detail = "running" if running else "stopped"
        if failed:
            detail = "FAILED"
        if loop.is_being_cancelled():
            detail = "cancelling"
        extra = []
        if nxt:
            extra.append(f"next {fmt_dt(nxt)}")
        current = getattr(loop, "current_loop", None)
        if current:
            extra.append(f"ticks {current}")
        return {
            "ok": running and not failed,
            "detail": detail,
            "extra": ", ".join(extra),
        }

    def plugin_rows(self):
        expected = plugin_folders()
        enabled = set(enabled_plugins())
        loaded_ext = set(self.bot.extensions.keys())
        rows = []
        problems = []
        for folder in expected:
            ext = f"plugins.{folder}.cog"
            if folder not in enabled:
                state = "disabled"
            elif ext in loaded_ext:
                state = "loaded"
            else:
                state = "MISSING"
                problems.append(f"{folder} enabled but not loaded")
            rows.append((folder, state))
        return rows, problems

    def scan(self):
        problems = []
        loops = []
        for cog_name, attr, loop in self.collect_loops():
            if cog_name == "Watchdog" and attr == "scan_task":
                continue
            status = self.loop_status(loop)
            loops.append((cog_name, attr, status))
            if not status["ok"]:
                problems.append(f"{cog_name}.{attr} is {status['detail']}")
                if self.settings.get("restart_dead_tasks") and not loop.is_running():
                    try:
                        loop.restart()
                        problems.append(f"restarted {cog_name}.{attr}")
                    except Exception as exc:
                        problems.append(f"could not restart {cog_name}.{attr}: {exc}")

        roster = self.bot.get_cog("FactionRoster")
        roster_age = None
        roster_count = 0
        if roster:
            roster_age = getattr(roster, "last_member_refresh", None)
            roster_count = len(getattr(roster, "members_by_id", {}) or {})
            stale_min = max(1, int(self.settings.get("stale_roster_minutes") or 30))
            if roster_age is None:
                problems.append("roster has never refreshed")
            elif isinstance(roster_age, datetime.datetime):
                age = roster_age
                if age.tzinfo is None:
                    age = age.replace(tzinfo=datetime.timezone.utc)
                if (utc_now() - age).total_seconds() > stale_min * 60:
                    problems.append(f"roster is stale ({fmt_age(roster_age)})")
            if roster_count == 0:
                problems.append("roster is empty")
        else:
            problems.append("FactionRoster cog is not loaded")

        web = self.bot.get_cog("WebConfig")
        web_ok = bool(web and getattr(web, "site", None))
        if web and not web_ok:
            problems.append("web config server is not bound")

        latency_ms = int(self.bot.latency * 1000) if self.bot.latency is not None else -1
        if latency_ms < 0 or latency_ms > 1500:
            problems.append(f"Discord latency {latency_ms}ms")

        plugin_rows, plugin_problems = self.plugin_rows()
        problems.extend(plugin_problems)

        self.last_scan = utc_now()
        self.last_problems = problems
        return {
            "problems": problems,
            "loops": loops,
            "plugins": plugin_rows,
            "roster_age": roster_age,
            "roster_count": roster_count,
            "web_ok": web_ok,
            "latency_ms": latency_ms,
        }

    def overview_embed(self, data):
        ok = not data["problems"]
        loaded = sum(1 for _, state in data["plugins"] if state == "loaded")
        missing = sum(1 for _, state in data["plugins"] if state == "MISSING")
        dead = sum(1 for _, _, status in data["loops"] if not status["ok"])
        embed = discord.Embed(
            title="Gimp Bot status",
            color=discord.Color.green() if ok else discord.Color.red(),
        )
        embed.add_field(
            name="Process",
            value=(
                f"User: `{self.bot.user}`\n"
                f"Uptime: {fmt_age(STARTED_AT)}\n"
                f"Gateway: {data['latency_ms']}ms\n"
                f"PID: `{os.getpid()}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Roster",
            value=(
                f"Members: **{data['roster_count']}**\n"
                f"Refreshed: {fmt_age(data['roster_age'])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Web UI",
            value="up" if data["web_ok"] else "down or unloaded",
            inline=True,
        )
        embed.add_field(
            name="Counts",
            value=(
                f"Plugins loaded: **{loaded}**\n"
                f"Plugins missing: **{missing}**\n"
                f"Tasks not running: **{dead}** / {len(data['loops'])}\n"
                f"Problems: **{len(data['problems'])}**"
            ),
            inline=False,
        )
        if data["problems"]:
            preview = "\n".join(f"• {item}" for item in data["problems"][:8])
            if len(data["problems"]) > 8:
                preview += f"\n…and {len(data['problems']) - 8} more. Use `/check all`."
            embed.add_field(name="Problems", value=preview[:1024], inline=False)
        else:
            embed.set_footer(text="No hangs or dead tasks detected.")
        return embed

    def plugin_lines(self, data):
        return [f"`{name}` {state}" for name, state in data["plugins"]] or ["(no plugin folders)"]

    def task_lines(self, data):
        lines = []
        for cog_name, attr, status in data["loops"]:
            mark = "ok" if status["ok"] else "DEAD"
            extra = f" — {status['extra']}" if status["extra"] else ""
            lines.append(f"`{cog_name}.{attr}` {mark} ({status['detail']}){extra}")
        return lines or ["(no background tasks)"]

    def text_chunks(self, lines, limit=1700):
        batches = []
        current = ""
        for line in lines:
            piece = line if not current else f"{current}\n{line}"
            if len(piece) > limit:
                if current:
                    batches.append(current)
                current = line[:limit]
            else:
                current = piece
        if current:
            batches.append(current)
        return batches or ["(none)"]

    async def send_parts(self, interaction, parts):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(parts[0], ephemeral=True)
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True)

    @check.command(name="overview", description="Short process, roster, and problem summary.")
    async def check_overview(self, interaction: discord.Interaction):
        data = self.scan()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=self.overview_embed(data), ephemeral=True)

    @check.command(name="plugins", description="List every plugin folder and whether it loaded.")
    async def check_plugins(self, interaction: discord.Interaction):
        data = self.scan()
        chunks = self.text_chunks(self.plugin_lines(data))
        parts = [f"**Plugins** ({index}/{len(chunks)})\n{body}" for index, body in enumerate(chunks, start=1)]
        await self.send_parts(interaction, parts)

    @check.command(name="all", description="Full health report, split across several messages.")
    async def check_all(self, interaction: discord.Interaction):
        data = self.scan()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=self.overview_embed(data), ephemeral=True)
        for index, body in enumerate(self.text_chunks(self.plugin_lines(data)), start=1):
            await interaction.followup.send(f"**Plugins {index}**\n{body}", ephemeral=True)
        for index, body in enumerate(self.text_chunks(self.task_lines(data)), start=1):
            await interaction.followup.send(f"**Tasks {index}**\n{body}", ephemeral=True)
        problem_lines = [f"• {item}" for item in data["problems"]] or ["None."]
        for index, body in enumerate(self.text_chunks(problem_lines), start=1):
            await interaction.followup.send(f"**Problems {index}**\n{body}", ephemeral=True)

    @tasks.loop(minutes=5)
    async def scan_task(self):
        data = self.scan()
        if not data["problems"]:
            return
        channel_id = int(self.settings.get("alert_channel_id") or 0)
        if not channel_id:
            print("[Watchdog] " + "; ".join(data["problems"]))
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        try:
            await channel.send(embed=self.overview_embed(data))
        except discord.HTTPException as exc:
            print(f"[Watchdog] Alert send failed: {exc}")

    @scan_task.before_loop
    async def before_scan(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Watchdog(bot))
