"""Optional per-plugin log files.

Loaded: each plugin writes logs/<plugin>.log at the level chosen here.
Unloaded: plugin loggers go silent and only the main bot process logs.
"""

import os
import sys

from discord.ext import commands

sys.path.append(os.getcwd())
from plugin_log import LEVEL_NAMES, configure, get_logger, normalize_level, silence
from plugin_settings import load_settings, plugin_folders, save_settings, schema_defaults

PLUGIN_DIR = os.path.dirname(__file__)
log = get_logger("logging")


class PluginLogging(commands.Cog):
    SETTINGS_SCHEMA = []

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        if not isinstance(self.settings.get("levels"), dict):
            self.settings["levels"] = {}

    def cog_load(self):
        self.apply_levels()
        log.warning("Plugin logging enabled.")

    def cog_unload(self):
        silence()

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        if not isinstance(self.settings.get("levels"), dict):
            self.settings["levels"] = {}
        self.apply_levels()

    def level_for(self, folder):
        stored = (self.settings.get("levels") or {}).get(folder, "warning")
        return normalize_level(stored)

    def apply_levels(self):
        folders = plugin_folders()
        if "logging" not in folders:
            folders = ["logging"] + list(folders)
        for folder in folders:
            configure(folder, self.level_for(folder))

    def web_tables(self):
        folders = plugin_folders()
        if "logging" not in folders:
            folders = ["logging"] + list(folders)
        options = []
        for folder in folders:
            current = self.level_for(folder)
            choices = "".join(
                f'<option value="{level}"{" selected" if level == current else ""}>{level}</option>'
                for level in LEVEL_NAMES
            )
            options.append(
                f'<label for="level_{folder}">{folder}</label>'
                f'<select id="level_{folder}" name="level_{folder}">{choices}</select>'
            )
        form = """
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="save_levels">
          <p class="help">Files are written to logs/<plugin>.log. Warning is the default, so routine info from the roster stays out until you raise that plugin.</p>
          %s
          <button type="submit">Save log levels</button>
        </form>
        """ % "\n".join(options)
        rows = [
            {"id": folder, "Plugin": folder, "Level": self.level_for(folder), "File": f"logs/{folder}.log"}
            for folder in folders
        ]
        return [
            {
                "title": "Log level per plugin",
                "columns": ["Plugin", "Level", "File"],
                "rows": rows,
                "add_form": form,
            }
        ]

    def web_action(self, data):
        if data.get("action") != "save_levels":
            return "Unknown action."
        levels = {}
        for folder in plugin_folders():
            levels[folder] = normalize_level(data.get(f"level_{folder}"))
        if "logging" not in levels:
            levels["logging"] = normalize_level(data.get("level_logging"))
        self.settings["levels"] = levels
        save_settings(PLUGIN_DIR, self.settings)
        self.apply_levels()
        log.info("Updated plugin log levels.")
        return "Log levels saved."


async def setup(bot: commands.Bot):
    await bot.add_cog(PluginLogging(bot))
