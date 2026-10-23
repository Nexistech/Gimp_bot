import html
import os
import sys

from discord.ext import commands

sys.path.append(os.getcwd())

from plugin_log import get_logger
from plugin_settings import load_settings, save_settings, schema_defaults
from torn_api import CALLER_LABELS, DEFAULT_ORDER, get_scheduler

log = get_logger("api_queue")

PLUGIN_DIR = os.path.dirname(__file__)
DB_NAME = os.path.join(PLUGIN_DIR, "api_history.db")
WINDOWS = [
    (1, "1 hour"),
    (6, "6 hours"),
    (12, "12 hours"),
    (24, "24 hours"),
    (48, "48 hours"),
    (72, "3 days"),
    (168, "7 days"),
    (336, "14 days"),
]
COLORS = [
    "#4c6fff",
    "#3ddc97",
    "#f5a524",
    "#ef5b5b",
    "#c084fc",
    "#22d3ee",
    "#f472b6",
    "#a3e635",
    "#fb923c",
    "#94a3b8",
]


class TornApiQueue(commands.Cog):
    SETTINGS_SCHEMA = [
        {
            "key": "calls_per_minute",
            "type": "int",
            "label": "Max Torn calls per minute, shared by every cog",
            "default": 80,
            "help": "Torn's cap is 100. Leave some room so a burst does not soft-ban the key.",
        },
        {
            "key": "history_hours",
            "type": "int",
            "label": "Graph window (hours)",
            "default": 48,
            "help": "How much history the graph shows. 1 to 336 (2 weeks). At least 48 hours is always kept.",
        },
    ]

    def __init__(self, bot):
        self.bot = bot
        self.settings = load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        self.apply()

    def apply(self):
        order = self.settings.get("order") or list(DEFAULT_ORDER)
        get_scheduler().configure(
            self.settings.get("calls_per_minute") or 80,
            order,
            history_hours=self.settings.get("history_hours") or 48,
            db_path=DB_NAME,
        )
        self.settings["order"] = list(get_scheduler().order)

    def reload_settings(self, data=None):
        self.settings = data or load_settings(PLUGIN_DIR, schema_defaults(self.SETTINGS_SCHEMA))
        try:
            hours = int(self.settings.get("history_hours") or 48)
        except (TypeError, ValueError):
            hours = 48
        self.settings["history_hours"] = max(1, min(336, hours))
        self.apply()
        self.save_order()

    def save_order(self):
        self.settings["order"] = list(get_scheduler().order)
        save_settings(PLUGIN_DIR, self.settings)

    def cog_unload(self):
        worker = get_scheduler()._worker
        if worker and not worker.done():
            worker.cancel()

    def chart_svg(self, payload):
        points = payload["points"]
        if not points or not any(point["total"] or point["queue"] for point in points):
            return "<p class=\"help\">No Torn calls recorded in this window yet.</p>"
        cogs = payload["cogs"]
        colors = {cog: COLORS[index % len(COLORS)] for index, cog in enumerate(cogs)}
        width, height = 960, 280
        left, right, top, bottom = 46, 46, 12, 42
        plot_w = width - left - right
        plot_h = height - top - bottom
        max_calls = max(point["total"] for point in points) or 1
        max_queue = max(point["queue"] for point in points) or 1
        bar_w = plot_w / max(1, len(points))
        parts = [
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'style="background:#141824;border-radius:8px">'
        ]
        for tick in range(5):
            value = max_calls * tick / 4
            y = top + plot_h - (plot_h * tick / 4)
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#2a3142"/>'
                f'<text x="4" y="{y + 4:.1f}" fill="#9aa3b5" font-size="11">{int(value)}</text>'
            )
            q_value = max_queue * tick / 4
            parts.append(
                f'<text x="{width - 4}" y="{y + 4:.1f}" fill="#f8fafc" font-size="11" text-anchor="end">{int(q_value)}</text>'
            )
        step = max(1, len(points) // 8)
        for index, point in enumerate(points):
            x = left + index * bar_w
            y = top + plot_h
            title = html.escape(
                point["label"]
                + " UTC — "
                + ", ".join(
                    f"{CALLER_LABELS.get(cog, cog)} {point['calls'].get(cog, 0)}"
                    for cog in cogs
                    if point["calls"].get(cog, 0)
                )
                + f" — queue {point['queue']}"
            )
            for cog in cogs:
                count = point["calls"].get(cog, 0)
                bar_h = count / max_calls * plot_h
                y -= bar_h
                if bar_h <= 0:
                    continue
                parts.append(
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(bar_w - 0.8, 0.4):.2f}" '
                    f'height="{bar_h:.2f}" fill="{colors[cog]}"><title>{title}</title></rect>'
                )
            if index % step == 0:
                parts.append(
                    f'<text x="{x:.1f}" y="{height - 8}" fill="#9aa3b5" font-size="10">'
                    f'{html.escape(point["label"])}</text>'
                )
        coords = []
        for index, point in enumerate(points):
            x = left + index * bar_w + bar_w / 2
            y = top + plot_h - (point["queue"] / max_queue * plot_h)
            coords.append(f"{x:.1f},{y:.1f}")
        parts.append(
            f'<polyline fill="none" stroke="#f8fafc" stroke-width="1.6" points="{" ".join(coords)}">'
            f'<title>Queue depth</title></polyline></svg>'
        )
        legend = [
            '<span style="display:inline-block;margin:4px 10px 0 0;color:#f8fafc">White line: waiting queue</span>'
        ]
        for cog in cogs:
            label = html.escape(CALLER_LABELS.get(cog, cog))
            legend.append(
                f'<span style="display:inline-block;margin:4px 10px 0 0">'
                f'<i style="display:inline-block;width:10px;height:10px;background:{colors[cog]};'
                f'margin-right:4px"></i>{label} ({payload["totals"].get(cog, 0)})</span>'
            )
        return "".join(parts) + '<div style="margin-top:8px">' + "".join(legend) + "</div>"

    def web_tables(self):
        hours = int(self.settings.get("history_hours") or 48)
        payload = get_scheduler().history(hours)
        bucket = payload["bucket"]
        if bucket < 3600:
            bucket_label = f"{bucket // 60} min"
        else:
            bucket_label = f"{bucket // 3600} hour" if bucket == 3600 else f"{bucket // 3600} hours"
        choices = []
        known = {value for value, _label in WINDOWS}
        if hours not in known:
            choices.append(f'<option value="{hours}" selected>{hours} hours</option>')
        for value, label in WINDOWS:
            selected = " selected" if value == hours else ""
            choices.append(f'<option value="{value}"{selected}>{label}</option>')
        chart = f"""
        <form method="post" action="/plugin/__PLUGIN__/action" style="margin-bottom:12px">
          <input type="hidden" name="action" value="set_window">
          <label for="history_hours">History window</label>
          <select id="history_hours" name="history_hours">{''.join(choices)}</select>
          <button type="submit">Show</button>
        </form>
        <p class="help">Bars are Torn calls, stacked by cog. The white line is how many calls were waiting. Each bar is {bucket_label}. Times are UTC. At least 48 hours is stored, and the window can be opened out to 14 days.</p>
        {self.chart_svg(payload)}
        """
        rows = [
            {
                "Cog": CALLER_LABELS.get(cog, cog),
                "Calls": payload["totals"].get(cog, 0),
            }
            for cog in payload["cogs"]
        ]
        order = list(get_scheduler().order)
        options = []
        for cog_id in order:
            label = CALLER_LABELS.get(cog_id, cog_id)
            select = "".join(
                f'<option value="{number}"{" selected" if number == order.index(cog_id) + 1 else ""}>{number}</option>'
                for number in range(1, len(order) + 1)
            )
            options.append(f"<p><label>{label}</label><select name=\"priority_{cog_id}\">{select}</select></p>")
        stats = get_scheduler().snapshot()
        form = f"""
        <p class="help">Priority 1 is served first. Each number can belong to only one cog. Last minute: {stats["used"]}/{stats["limit"]} calls, {stats["waiting"]} waiting.</p>
        <form method="post" action="/plugin/__PLUGIN__/action">
          <input type="hidden" name="action" value="save_priorities">
          {''.join(options)}
          <button type="submit">Save priorities</button>
        </form>
        """
        return [
            {
                "title": "Call volume",
                "columns": ["Cog", "Calls"],
                "rows": rows,
                "add_form": chart,
            },
            {"title": "Call priority", "columns": [], "rows": [], "add_form": form},
        ]

    def web_action(self, data):
        action = data.get("action")
        if action == "set_window":
            try:
                hours = int(data.get("history_hours") or 0)
            except (TypeError, ValueError):
                return "History window was not a number."
            if hours < 1 or hours > 336:
                return "History window must be from 1 hour to 336 hours (2 weeks)."
            self.settings["history_hours"] = hours
            self.apply()
            self.save_order()
            return f"Graph window set to {hours} hours."
        if action != "save_priorities":
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
        get_scheduler().configure(
            self.settings.get("calls_per_minute") or 80,
            new_order,
            history_hours=self.settings.get("history_hours") or 48,
            db_path=DB_NAME,
        )
        self.save_order()
        log.info("[API] Priority saved: " + ", ".join(f"{index}. {cog_id}" for index, cog_id in enumerate(new_order, start=1)))
        return "Priorities saved."


async def setup(bot: commands.Bot):
    await bot.add_cog(TornApiQueue(bot))
