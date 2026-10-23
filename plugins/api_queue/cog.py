import os
import sys

from discord.ext import commands

sys.path.append(os.getcwd())

from plugin_log import get_logger
from plugin_settings import load_settings, save_settings, schema_defaults
from torn_api import CALLER_LABELS, DEFAULT_ORDER, get_scheduler

log = get_logger("api_queue")

PLUGIN_DIR = os.path.dirname(__file__)


class TornApiQueue(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "calls_per_minute",
            "type": "int",
            "label": "Max Torn calls per minute, shared by every cog",
            "default": 80,
            "help": "Torn's cap is 100. Leave some room so a burst does not soft-ban the key.",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.apply()

    def apply(self):
        order = self.settings.get("order") or list(DEFAULT_ORDER)
        get_scheduler().configure(self.settings.get("calls_per_minute") or 80, order)
        self.settings["order"] = list(get_scheduler().order)

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.apply()
        self.save_order()

    def save_order(self):
        self.settings["order"] = list(get_scheduler().order)
        save_settings(PLUGIN_DIR, self.settings)

    def cog_unload(self):
        worker = get_scheduler()._worker
        if worker and not worker.done():
            worker.cancel()

    def web_tables(self):
        order = list(get_scheduler().order)
        options = []
        for cog_id in order:
            label = CALLER_LABELS.get(cog_id, cog_id)
            choices = "".join(
                f'<option value="{number}"{" selected" if number == order.index(cog_id) + 1 else ""}>{number}</option>'
                for number in range(1, len(order) + 1)
            )
            options.append(
                f'<p><label>{label}</label>'
                f'<select name="priority_{cog_id}">{choices}</select></p>'
            )
        stats = get_scheduler().snapshot()
        form = f"""
        <p class="help">Priority 1 is served first. Each number can belong to only one cog. A lower cog waits behind a higher one, and the shared cap still applies to all of them. Last minute: {stats["used"]}/{stats["limit"]} calls, {stats["waiting"]} waiting.</p>
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="save_priorities">
          {''.join(options)}
          <button type="submit">Save priorities</button>
        </form>
        """
        return [{"title": "Call priority", "columns": [], "rows": [], "add_form": form}]

    def web_action(self, data):
        if data.get("action") != "save_priorities":
            return "Unknown action."
        order = list(get_scheduler().order)
        chosen = {}
        for cog_id in order:
            try:
                number = int(data.get(f"priority_{cog_id}") or 0)
            except (TypeError, ValueError):
                return "A priority was not a number."
            if number < 1 or number > len(order):
                return f"{CALLER_LABELS.get(cog_id, cog_id)} needs a priority from 1 to {len(order)}."
            if number in chosen:
                other = CALLER_LABELS.get(chosen[number], chosen[number])
                label = CALLER_LABELS.get(cog_id, cog_id)
                return f"{label} and {other} are both priority {number}. Each cog needs its own number."
            chosen[number] = cog_id
        new_order = [chosen[number] for number in range(1, len(order) + 1)]
        self.settings["order"] = new_order
        get_scheduler().configure(self.settings.get("calls_per_minute") or 80, new_order)
        self.save_order()
        log.info("[API] Priority saved: " + ", ".join(f"{index}. {cog_id}" for index, cog_id in enumerate(new_order, start=1)))
        return "Priorities saved."


async def setup(bot: commands.Bot):
    await bot.add_cog(TornApiQueue(bot))
