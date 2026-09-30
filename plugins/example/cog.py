"""
Example plugin â

Bot loads every folder under plugins/ that contains cog.py, unless
that folder is missing from ENABLED_PLUGINS in the root settings.json.

Required hook:
    async def setup(bot):
        await bot.add_cog(YourCog(bot))

Optional hooks the web UI looks for:
    SETTINGS_SCHEMA	-	list of field dicts ->  plugins/example/settings.json
    reload_settings	-	called after the config page is saved
    web_tables		-	extra HTML tables on that plugin's page
    web_action		-	POST handler for those tables
"""

import datetime
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Repo root is the process cwd. This lets "from config import ..." work
# even though this file lives in plugins/example/.
sys.path.append(os.getcwd())

from plugin_settings import load_root_settings, load_settings, save_settings, schema_defaults

try:
    from config import TORN_API_KEY
except ImportError:
    TORN_API_KEY = ""

PLUGIN_DIR = os.path.dirname(__file__)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class ExamplePlugin(commands.Cog):
    """One cog per plugin folder. Class name can be anything unique."""

    # Rendered by plugins/web_config on /plugin/example
    # Supported types: str, int, bool, int_list, str_list
    # Optional widgets: torn_ids, discord_role, discord_roles, discord_channel
    SETTINGS_SCHEMA = [
        {
            "key": "enabled",
            "type": "bool",
            "label": "Enable this example plugin's background task",
            "default": False,
        },
        {
            "key": "channel_id",
            "type": "int",
            "label": "Output channel (Disabled = 0)",
            "default": 0,
            "widget": "discord_channel",
        },
        {
            "key": "ping_roles",
            "type": "str_list",
            "label": "Roles to ping",
            "default": [],
            "widget": "discord_roles",
        },
        {
            "key": "check_minutes",
            "type": "int",
            "label": "How often the example task runs",
            "default": 30,
        },
        {
            "key": "note",
            "type": "str",
            "label": "Free-text note stored in this plugin's settings.json",
            "default": "Hello from the example plugin.",
        },
    ]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Creates plugins/example/settings.json from defaults on first run.
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.tick_count = 0
        # Do not start work that needs Discord to be ready here.
        # Wait for cog_load / before_loop / on_ready.
        self.example_task.start()

    def reload_settings(self, data=None):
        """Web config calls this after a successful Save on this plugin page."""
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        minutes = max(1, int(self.settings.get("check_minutes") or 30))
        self.example_task.change_interval(minutes=minutes)

    async def cog_load(self):
        """Runs on first load and on /reload. Good place to start a web server."""
        print("[Example] cog_load")

    def cog_unload(self):
        """Always cancel tasks here or they keep running after /reload."""
        self.example_task.cancel()

    def get_roster(self):
        """Shared member cache. Other plugins should not call Torn for the roster."""
        return self.bot.get_cog("FactionRoster")

    def output_channel(self):
        channel_id = int(self.settings.get("channel_id") or 0)
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)

    @tasks.loop(minutes=30)
    async def example_task(self):
        if not self.settings.get("enabled"):
            return
        self.tick_count += 1
        roster = self.get_roster()
        size = len(roster.members_by_id) if roster else 0
        channel = self.output_channel()
        if channel:
            await channel.send(f"Example tick #{self.tick_count}. Roster size {size}.")

    @example_task.before_loop
    async def before_example_task(self):
        await self.bot.wait_until_ready()
        roster = self.get_roster()
        if roster and hasattr(roster, "wait_until_populated"):
            await roster.wait_until_populated()

    @app_commands.command(name="example_ping", description="Example slash command.")
    async def example_ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Example plugin is loaded. Note: {self.settings.get('note')}",
            ephemeral=True,
        )

    def web_tables(self):
        """
        Extra blocks under the settings form.
        rows need an "id" plus one key per column name.
        __PLUGIN__ is replaced with this folder name by web_config.
        """
        roster = self.get_roster()
        sample = []
        if roster:
            for member in list(roster.all_members())[:5]:
                sample.append(
                    {
                        "id": member.get("id") or member.get("user_id"),
                        "Name": member.get("name"),
                        "Rank": member.get("position") or "",
                    }
                )
        add_form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="save_note">
          <label>Replace the stored note</label>
          <input name="note" placeholder="New note">
          <button type="submit">Save note</button>
        </form>
        """
        return [
            {
                "title": "First five roster members (read-only sample)",
                "columns": ["Name", "Rank"],
                "rows": sample,
                "add_form": add_form,
            }
        ]

    def web_action(self, data):
        """data is the POST body from a web_tables form."""
        if data.get("action") != "save_note":
            return "Unknown action."
        note = str(data.get("note") or "").strip()
        if not note:
            return "Note was empty."
        self.settings["note"] = note
        save_settings(PLUGIN_DIR, self.settings)
        return "Note saved."


async def setup(bot: commands.Bot):
    await bot.add_cog(ExamplePlugin(bot))
