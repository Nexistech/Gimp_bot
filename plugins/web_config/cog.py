import html
import hmac
import json
import logging
import os
import secrets
import sys
from logging.handlers import RotatingFileHandler

import aiohttp
from aiohttp import web
from discord.ext import commands

sys.path.append(os.getcwd())
from plugin_settings import (
    ROOT_SCHEMA,
    coerce_value,
    enabled_plugins,
    load_root_settings,
    load_settings,
    plugin_folders,
    save_settings,
    schema_defaults,
)

PLUGIN_DIR = os.path.dirname(__file__)
ROOT_DIR = os.getcwd()
WEB_LOG_FILE = os.path.join(PLUGIN_DIR, "web_config.log")


def setup_web_logging():
    logger = logging.getLogger("aiohttp.access")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(WEB_LOG_FILE, maxBytes=500_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    server = logging.getLogger("aiohttp.server")
    server.setLevel(logging.WARNING)
    server.propagate = False
    if not server.handlers:
        handler = RotatingFileHandler(WEB_LOG_FILE, maxBytes=500_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        server.addHandler(handler)
    return logger

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strike bot config</title>
<style>
:root { color-scheme: dark; }
body { margin:0; font-family: system-ui, sans-serif; background:#101218; color:#e8eaed; }
.layout { display:flex; min-height:100vh; }
nav { width:240px; background:#1a1d27; padding:20px 0; border-right:1px solid #2a2e3b; }
nav h1 { font-size:16px; margin:0 20px 16px; }
nav a { display:block; padding:10px 20px; color:#c5c9d3; text-decoration:none; }
nav a.active, nav a:hover { background:#2a3148; color:#fff; }
nav .nav-label { margin:18px 20px 8px; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#8b93a7; }
main { flex:1; padding:28px 32px; max-width:820px; }
label { display:block; margin:16px 0 6px; font-weight:600; }
input, textarea { width:100%; box-sizing:border-box; padding:8px 10px; border-radius:6px;
  border:1px solid #3b4154; background:#0d0f14; color:#fff; }
.help { color:#9aa3b5; font-size:13px; margin-top:4px; }
button { margin-top:20px; background:#4c6fff; color:#fff; border:0; padding:10px 16px; border-radius:6px; cursor:pointer; }
table { width:100%; border-collapse:collapse; margin-top:12px; font-size:14px; }
th, td { text-align:left; padding:8px; border-bottom:1px solid #2a2e3b; }
.row-btn { margin:0; background:#5a3148; padding:4px 8px; font-size:12px; }
h3 { margin-top:36px; }
.flash { background:#1e3a2f; border:1px solid #2e8b57; padding:10px 12px; border-radius:6px; margin-bottom:16px; }
.error { background:#3a1e1e; border-color:#8b2e2e; }
.login { max-width:360px; margin:12vh auto; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }
.chip { display:flex; align-items:center; gap:8px; background:#2a3148; border-radius:999px;
  padding:6px 8px 6px 12px; font-size:14px; }
.chip button { margin:0; background:#5a3148; padding:4px 8px; font-size:12px; }
.search-wrap { position:relative; }
.suggest { position:absolute; left:0; right:0; top:100%; background:#1a1d27; border:1px solid #3b4154;
  border-radius:6px; z-index:5; max-height:220px; overflow:auto; display:none; }
.suggest button { display:block; width:100%; text-align:left; margin:0; background:transparent; border-radius:0; }
.suggest button:hover { background:#2a3148; }
</style>
</head>
<body>
__BODY__
</body>
</html>
"""


def render_page(body):
    return PAGE.replace("__BODY__", body)


class WebConfig(commands.Cog):
    """Generic settings editor. Each cog exposes SETTINGS_SCHEMA; values live in that folder's settings.json."""

    def __init__(self, bot):
        self.bot = bot
        self.site = None
        self.runner = None
        self.session_token = secrets.token_hex(16)
        self.settings = load_root_settings()

    def cog_unload(self):
        if self.bot.loop.is_running():
            self.bot.loop.create_task(self.stop_site())

    def discover_plugins(self):
        items = [{"id": "bot", "label": "Bot", "schema": ROOT_SCHEMA, "dir": ROOT_DIR}]
        plugins_dir = os.path.join(ROOT_DIR, "plugins")
        allowed = set(enabled_plugins())
        if not os.path.isdir(plugins_dir):
            return items
        for folder in sorted(os.listdir(plugins_dir)):
            cog_path = os.path.join(plugins_dir, folder, "cog.py")
            if not os.path.isfile(cog_path):
                continue
            if folder != "web_config" and folder not in allowed:
                continue
            cog = self._cog_for_folder(folder)
            schema = getattr(cog, "SETTINGS_SCHEMA", None) if cog else None
            has_tables = bool(cog and hasattr(cog, "web_tables"))
            if not schema and not has_tables:
                continue
            schema = schema or []
            items.append(
                {
                    "id": folder,
                    "label": folder.replace("_", " ").title(),
                    "schema": schema,
                    "dir": os.path.join(plugins_dir, folder),
                    "cog": cog,
                }
            )
        return items

    def _cog_for_folder(self, folder):
        for cog in self.bot.cogs.values():
            cog_file = getattr(cog, "__module__", "")
            if cog_file == f"plugins.{folder}.cog":
                return cog
        return None

    def load_plugin_settings(self, item):
        return load_settings(item["dir"], schema_defaults(item["schema"]))

    def authorized(self, request):
        cookie = request.cookies.get("cfg_session", "")
        return hmac.compare_digest(cookie, self.session_token)

    async def handle_login(self, request):
        if request.method == "POST":
            data = await request.post()
            password = data.get("password", "")
            expected = str(self.settings.get("WEB_PASSWORD") or "")
            if expected and hmac.compare_digest(password, expected):
                response = web.HTTPFound("/")
                response.set_cookie("cfg_session", self.session_token, httponly=True, samesite="Lax")
                return response
            body = self.login_body(error="Wrong password.")
            return web.Response(text=render_page(body), content_type="text/html", status=401)
        return web.Response(text=render_page(self.login_body()), content_type="text/html")

    def login_body(self, error=None):
        flash = f'<div class="flash error">{error}</div>' if error else ""
        return f"""
        <div class="login">
          <h1>Bot config</h1>
          {flash}
          <form method="post" action="/login">
            <label>Password</label>
            <input type="password" name="password" autofocus>
            <button type="submit">Sign in</button>
          </form>
        </div>
        """

    def nav_html(self, active):
        links = [
            f'<a class="{"active" if active == "bot" else ""}" href="/plugin/bot">Bot</a>',
            f'<a class="{"active" if active == "plugins" else ""}" href="/plugins">Plugins</a>',
        ]
        links.append('<div class="nav-label">Enabled plugins</div>')
        for item in self.discover_plugins():
            if item["id"] == "bot":
                continue
            cls = "active" if item["id"] == active else ""
            links.append(f'<a class="{cls}" href="/plugin/{item["id"]}">{item["label"]}</a>')
        return "<nav><h1>Strike bot</h1>" + "".join(links) + "</nav>"

    def roster(self):
        return self.bot.get_cog("FactionRoster")

    def member_label(self, user_id):
        roster = self.roster()
        member = roster.get_member(user_id) if roster else None
        if member:
            name = html.escape(str(member.get("name") or "Unknown"))
            return f"{name} ({int(user_id)})"
        return f"Unknown ({int(user_id)})"

    def search_roster(self, query, limit=8):
        roster = self.roster()
        if not roster:
            return []
        query = (query or "").strip().lower()
        if not query:
            return []
        matches = []
        if query.isdigit():
            member = roster.get_member(int(query))
            if member:
                matches.append({"id": int(member.get("id") or member.get("user_id")), "name": member.get("name")})
        for member in roster.all_members():
            name = str(member.get("name") or "")
            uid = member.get("id") or member.get("user_id")
            if query in name.lower() or query == str(uid):
                item = {"id": int(uid), "name": name}
                if item not in matches:
                    matches.append(item)
            if len(matches) >= limit:
                break
        return matches

    def torn_ids_html(self, key, label, value, help_html):
        chips = []
        ids = []
        for raw in value or []:
            try:
                user_id = int(raw)
            except (TypeError, ValueError):
                continue
            ids.append(str(user_id))
            chips.append(
                f'<span class="chip" data-id="{user_id}">'
                f'{self.member_label(user_id)}'
                f'<button type="button" class="remove-id" data-field="{key}" data-id="{user_id}">Remove</button>'
                f"</span>"
            )
        hidden = html.escape(", ".join(ids))
        return f"""
        <label>{label}</label>
        <div class="member-picker" data-field="{key}">
          <input type="hidden" name="{key}" id="{key}" value="{hidden}">
          <div class="chips" id="chips-{key}">{''.join(chips)}</div>
          <div class="search-wrap">
            <input type="text" class="member-search" data-field="{key}" data-source="roster" data-mode="multi"
                   placeholder="Search roster by name or Torn ID" autocomplete="off">
            <div class="suggest" id="suggest-{key}"></div>
          </div>
        </div>
        {help_html}
        """

    def config_guild(self):
        root = load_root_settings()
        guild_id = int(root.get("DEV_GUILD_ID") or 0)
        if guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild:
                return guild
        channel_id = int(root.get("SPAM_CHANNEL_ID") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel and getattr(channel, "guild", None):
            return channel.guild
        if self.bot.guilds:
            return self.bot.guilds[0]
        return None

    def search_roles(self, query, limit=12):
        guild = self.config_guild()
        if not guild:
            return []
        query = (query or "").strip().lower()
        roles = [role for role in guild.roles if role.name != "@everyone"]
        if query:
            roles = [role for role in roles if query in role.name.lower() or query == str(role.id)]
        roles = sorted(roles, key=lambda r: r.name.lower())[:limit]
        return [{"id": role.id, "name": role.name} for role in roles]

    def search_channels(self, query, limit=12):
        guild = self.config_guild()
        results = [{"id": 0, "name": "Disabled"}]
        if not guild:
            return results[:limit]
        query = (query or "").strip().lower()
        channels = [ch for ch in guild.text_channels]
        if query:
            channels = [
                ch
                for ch in channels
                if query in ch.name.lower() or query == str(ch.id) or query in {"off", "disable", "disabled"}
            ]
        channels = sorted(channels, key=lambda c: (c.position, c.name.lower()))[: limit - 1]
        results.extend({"id": ch.id, "name": f"#{ch.name}"} for ch in channels)
        return results

    def discord_channel_html(self, key, label, value, help_html):
        try:
            channel_id = int(value or 0)
        except (TypeError, ValueError):
            channel_id = 0
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            shown = f"#{channel.name}" if channel else str(channel_id)
        else:
            shown = "Disabled"
        chip = (
            f'<span class="chip" data-id="{channel_id}">'
            f"{html.escape(shown)}"
            f'<button type="button" class="remove-id" data-field="{key}" data-id="{channel_id}">Remove</button>'
            f"</span>"
        )
        return f"""
        <label>{label}</label>
        <div class="member-picker" data-field="{key}">
          <input type="hidden" name="{key}" id="{key}" value="{channel_id}">
          <div class="chips" id="chips-{key}">{chip}</div>
          <div class="search-wrap">
            <input type="text" class="member-search" data-field="{key}" data-source="channels" data-mode="single"
                   placeholder="Search channels or type disabled" autocomplete="off">
            <div class="suggest" id="suggest-{key}"></div>
          </div>
        </div>
        {help_html}
        """

    def discord_roles_html(self, key, label, value, help_html, single=False):
        names = []
        if isinstance(value, str):
            names = [value] if value else []
        else:
            names = [str(v) for v in (value or []) if v]
        chips = []
        for name in names:
            safe = html.escape(name)
            chips.append(
                f'<span class="chip" data-id="{safe}">'
                f"{safe}"
                f'<button type="button" class="remove-id" data-field="{key}" data-id="{safe}">Remove</button>'
                f"</span>"
            )
        hidden = html.escape(", ".join(names))
        mode = "single" if single else "multi"
        return f"""
        <label>{label}</label>
        <div class="member-picker" data-field="{key}">
          <input type="hidden" name="{key}" id="{key}" value="{hidden}">
          <div class="chips" id="chips-{key}">{''.join(chips)}</div>
          <div class="search-wrap">
            <input type="text" class="member-search" data-field="{key}" data-source="roles" data-mode="{mode}"
                   placeholder="Search Discord roles" autocomplete="off">
            <div class="suggest" id="suggest-{key}"></div>
          </div>
        </div>
        {help_html}
        """

    def field_html(self, field, value):
        key = field["key"]
        ftype = field.get("type", "str")
        help_text = field.get("help", "")
        help_html = f'<div class="help">{help_text}</div>' if help_text else ""
        if ftype == "bool":
            checked = "checked" if value else ""
            return (
                f'<label><input type="checkbox" name="{key}" value="true" {checked}> {field["label"]}</label>'
                f"{help_html}"
            )
        if field.get("widget") == "torn_ids" or key in {"exempt_ids"}:
            return self.torn_ids_html(key, field["label"], value or [], help_html)
        if field.get("widget") == "discord_role":
            return self.discord_roles_html(key, field["label"], [value] if value else [], help_html, single=True)
        if field.get("widget") == "discord_roles" or key in {"ROLES_TO_TAG"}:
            return self.discord_roles_html(key, field["label"], value or [], help_html, single=False)
        if field.get("widget") == "discord_channel" or key in {"channel_id", "CHANNEL_ID"}:
            return self.discord_channel_html(key, field["label"], value, help_html)
        if ftype in {"int_list", "str_list"}:
            display = ", ".join(str(v) for v in (value or []))
            return (
                f'<label for="{key}">{field["label"]}</label>'
                f'<textarea id="{key}" name="{key}" rows="3">{display}</textarea>{help_html}'
            )
        input_type = "password" if "PASSWORD" in key.upper() else "text"
        return (
            f'<label for="{key}">{field["label"]}</label>'
            f'<input id="{key}" name="{key}" type="{input_type}" value="{value if value is not None else ""}">'
            f"{help_html}"
        )

    def form_body(self, item, flash=None):
        settings = self.load_plugin_settings(item)
        fields = "".join(self.field_html(field, settings.get(field["key"])) for field in item["schema"])
        settings_form = ""
        if item["schema"]:
            settings_form = f"""
            <form method="post" action="/plugin/{item["id"]}">
              {fields}
              <button type="submit">Save</button>
            </form>
            """
        extra = self.tables_html(item)
        banner = f'<div class="flash">{flash}</div>' if flash else ""
        return f"""
        <div class="layout">
          {self.nav_html(item["id"])}
          <main>
            <h2>{item["label"]} settings</h2>
            {banner}
            {settings_form}
            {extra}
            <script>
            function hiddenInput(field) {{ return document.getElementById(field); }}
            function parseIds(field) {{
              return (hiddenInput(field).value || "").split(/[,\\s]+/).filter(Boolean);
            }}
            function setIds(field, ids) {{
              hiddenInput(field).value = ids.join(", ");
            }}
            function addChoice(field, id, label, mode) {{
              let ids = parseIds(field);
              if (mode === "single") ids = [];
              if (ids.includes(String(id))) return;
              ids.push(String(id));
              setIds(field, ids);
              const chips = document.getElementById("chips-" + field);
              if (mode === "single") chips.innerHTML = "";
              const chip = document.createElement("span");
              chip.className = "chip";
              chip.dataset.id = id;
              chip.innerHTML = label + ' <button type="button" class="remove-id" data-field="' + field + '" data-id="' + id + '">Remove</button>';
              chips.appendChild(chip);
            }}
            document.addEventListener("click", (ev) => {{
              const btn = ev.target.closest(".remove-id");
              if (!btn) return;
              const field = btn.dataset.field;
              const id = btn.dataset.id;
              setIds(field, parseIds(field).filter((x) => x !== String(id)));
              btn.closest(".chip").remove();
            }});
            document.querySelectorAll("table form").forEach((form) => {{
              form.addEventListener("submit", async (ev) => {{
                ev.preventDefault();
                const body = new URLSearchParams(new FormData(form));
                const res = await fetch(form.getAttribute("action"), {{
                  method: "POST",
                  credentials: "same-origin",
                  headers: {{ "X-Requested-With": "fetch" }},
                  body
                }});
                const data = await res.json().catch(() => ({{ ok: false, message: "Request failed" }}));
                let flash = document.querySelector(".flash");
                if (!flash) {{
                  flash = document.createElement("div");
                  flash.className = "flash";
                  document.querySelector("main").prepend(flash);
                }}
                flash.textContent = data.message || "Saved";
                flash.className = data.ok === false ? "flash error" : "flash";
                const row = form.closest("tr");
                const action = body.get("action");
                if (data.ok !== false && row && ["track_item", "untrack_item", "remove_subscription", "remove_strike"].includes(action)) {{
                  row.remove();
                }}
              }});
            }});
            let timer = null;
            document.querySelectorAll(".member-search").forEach((input) => {{
              input.addEventListener("input", () => {{
                const field = input.dataset.field;
                const source = input.dataset.source || "roster";
                const mode = input.dataset.mode || "multi";
                const box = document.getElementById("suggest-" + field);
                clearTimeout(timer);
                const q = input.value.trim();
                if (!q) {{ box.style.display = "none"; box.innerHTML = ""; return; }}
                timer = setTimeout(async () => {{
                  const url = source === "roles" || source === "rolenames" ? (source === "rolenames" ? "/api/role-names?q=" : "/api/roles?q=")
                    : source === "ranks" ? "/api/ranks?q="
                    : source === "channels" ? "/api/channels?q="
                    : "/api/roster?q=";
                  const res = await fetch(url + encodeURIComponent(q));
                  const rows = await res.json();
                  if (!rows.length) {{
                    box.style.display = "block";
                    box.innerHTML = "<button type='button' disabled>No match</button>";
                    return;
                  }}
                  box.innerHTML = rows.map((row) => {{
                    const label = (source === "roles" || source === "rolenames" || source === "ranks") ? row.name : (row.name + " (" + row.id + ")");
                    const stored = (source === "roles" || source === "rolenames" || source === "ranks") ? row.name : String(row.id);
                    return "<button type='button' data-id='" + stored.replace(/'/g, "&#39;") + "' data-label='" + label.replace(/'/g, "&#39;") + "'>" + label + "</button>";
                  }}).join("");
                  box.style.display = "block";
                  box.querySelectorAll("button").forEach((choice) => {{
                    choice.addEventListener("click", () => {{
                      addChoice(field, choice.dataset.id, choice.dataset.label, mode);
                      input.value = "";
                      box.style.display = "none";
                    }});
                  }});
                }}, 200);
              }});
            }});
            </script>
          </main>
        </div>
        """

    async def handle_plugin(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        plugin_id = request.match_info["plugin_id"]
        item = next((row for row in self.discover_plugins() if row["id"] == plugin_id), None)
        if not item:
            raise web.HTTPNotFound()
        flash = None
        if request.method == "POST":
            posted = await request.post()
            current = self.load_plugin_settings(item)
            updated = dict(current)
            for field in item["schema"]:
                key = field["key"]
                if field.get("type") == "bool":
                    updated[key] = key in posted
                else:
                    updated[key] = coerce_value(field, posted.get(key, ""))
            save_settings(item["dir"], updated)
            if plugin_id == "bot":
                self.settings = updated
            cog = item.get("cog")
            if cog and hasattr(cog, "reload_settings"):
                try:
                    cog.reload_settings(updated)
                except Exception as exc:
                    print(f"[WebConfig] reload_settings failed for {plugin_id}: {exc}")
            flash = "Saved. Some changes apply immediately; host/port need a reboot."
        return web.Response(text=render_page(self.form_body(item, flash)), content_type="text/html")

    def tables_html(self, item):
        cog = item.get("cog")
        if not cog or not hasattr(cog, "web_tables"):
            return ""
        try:
            tables = cog.web_tables() or []
        except Exception as exc:
            return f'<p class="help">Could not load extra tables: {html.escape(str(exc))}</p>'
        blocks = []
        for table in tables:
            title = html.escape(table.get("title") or "Records")
            columns = table.get("columns") or []
            rows = table.get("rows") or []
            action = table.get("remove_action")
            row_actions = table.get("row_actions") or []
            if action and not row_actions:
                row_actions = [{"action": action, "label": "Remove"}]
            add_form = table.get("add_form")
            head = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
            if row_actions:
                head += "<th></th>"
            body_rows = []
            for row in rows:
                cells = "".join(f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in columns)
                if row_actions:
                    rid = html.escape(str(row.get("id", "")))
                    name = html.escape(str(row.get("Item") or row.get("name") or ""))
                    current_min = html.escape(str(row.get("Min") or row.get("min_qty") or "0"))
                    buttons = []
                    for spec in row_actions:
                        fields = (
                            f'<input type="hidden" name="action" value="{html.escape(spec["action"])}">'
                            f'<input type="hidden" name="id" value="{rid}">'
                            f'<input type="hidden" name="item_name" value="{name}">'
                        )
                        if spec.get("include_min"):
                            fields += (
                                f'<input name="min_qty" value="{current_min}" '
                                f'style="width:70px" title="Minimum quantity">'
                            )
                        buttons.append(
                            f'<form method="post" action="/plugin/{item["id"]}/action" style="display:inline">'
                            f"{fields}"
                            f'<button class="row-btn" type="submit">{html.escape(spec.get("label") or "Go")}</button>'
                            f"</form>"
                        )
                    cells += f'<td>{" ".join(buttons)}</td>'
                body_rows.append(f"<tr>{cells}</tr>")
            body = "".join(body_rows) or f'<tr><td colspan="{len(columns)+1}">None</td></tr>'
            extra_form = ""
            if add_form:
                extra_form = add_form.replace("__PLUGIN__", item["id"])
            blocks.append(f"<h3>{title}</h3>{extra_form}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        return "".join(blocks)

    async def handle_action(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        plugin_id = request.match_info["plugin_id"]
        item = next((row for row in self.discover_plugins() if row["id"] == plugin_id), None)
        if not item or not item.get("cog") or not hasattr(item["cog"], "web_action"):
            raise web.HTTPNotFound()
        posted = await request.post()
        ok = True
        try:
            message = item["cog"].web_action(dict(posted))
        except Exception as exc:
            ok = False
            message = f"Action failed: {exc}"
        if request.headers.get("X-Requested-With") == "fetch":
            return web.json_response({"ok": ok, "message": message, "action": posted.get("action"), "id": posted.get("id")})
        return web.Response(text=render_page(self.form_body(item, message)), content_type="text/html")

    async def handle_roster_search(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        query = request.query.get("q", "")
        return web.json_response(self.search_roster(query))

    async def handle_role_search(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        query = request.query.get("q", "")
        return web.json_response(self.search_roles(query))

    async def handle_rank_search(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        query = (request.query.get("q") or "").strip().lower()
        roster = self.roster()
        names = {"Recruit", "Member", "Fluffer", "Talent", "Freeloader"}
        if roster:
            for member in roster.all_members():
                pos = str(member.get("position") or "").strip()
                if pos:
                    names.add(pos)
        rows = [{"id": name, "name": name} for name in sorted(names, key=str.lower)]
        if query:
            rows = [row for row in rows if query in row["name"].lower()]
        return web.json_response(rows[:20])

    async def handle_role_name_search(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        rows = []
        for role in self.search_roles(request.query.get("q", ""), limit=20):
            rows.append({"id": role["name"], "name": role["name"]})
        return web.json_response(rows)

    async def handle_channel_search(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        query = request.query.get("q", "")
        return web.json_response(self.search_channels(query))

    async def handle_plugins(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        folders = plugin_folders()
        current_enabled = enabled_plugins()
        flash = None
        if request.method == "POST":
            posted = await request.post()
            selected = [folder for folder in folders if posted.get(f"plugin_{folder}")]
            if "web_config" in current_enabled and "web_config" not in selected:
                confirm = str(posted.get("confirm_disable_web") or "").strip().upper()
                if confirm != "YES":
                    flash = "Type YES to disable the web config plugin. It will stay enabled."
                    selected = list(current_enabled) if current_enabled else folders
                else:
                    flash = (
                        "Web config disabled in settings.json. After this page unloads, "
                        "re-enable it by adding \"web_config\" to ENABLED_PLUGINS and restarting."
                    )
            root = load_root_settings()
            root["ENABLED_PLUGINS"] = selected
            save_settings(ROOT_DIR, root)
            results = []
            if hasattr(self.bot, "apply_plugin_enabled"):
                results = await self.bot.apply_plugin_enabled(selected)
            if not flash:
                flash = "Plugin list saved. " + (", ".join(results) if results else "No load changes.")
            current_enabled = selected
        rows = []
        for folder in folders:
            checked = "checked" if folder in current_enabled else ""
            extra = " <span class=\"help\">required to use this UI</span>" if folder == "web_config" else ""
            rows.append(
                f'<label><input type="checkbox" name="plugin_{folder}" value="1" {checked}> '
                f"{folder.replace('_', ' ')}{extra}</label>"
            )
        body = f"""
        <div class="layout">
          {self.nav_html("plugins")}
          <main>
            <h2>Enabled plugins</h2>
            {f'<div class="flash">{html.escape(flash)}</div>' if flash else ""}
            <p class="help">Unchecked plugins are not loaded on startup. Toggling anything except web config reloads that cog now.</p>
            <form method="post" action="/plugins" id="plugin-form">
              {''.join(rows)}
              <label>Type YES to confirm disabling web config</label>
              <input name="confirm_disable_web" placeholder="YES" autocomplete="off">
              <button type="submit">Save plugin list</button>
            </form>
            <script>
            document.getElementById("plugin-form").addEventListener("submit", (ev) => {{
              const box = document.querySelector('input[name="plugin_web_config"]');
              if (box && !box.checked) {{
                const typed = (document.querySelector('input[name="confirm_disable_web"]').value || "").trim().toUpperCase();
                if (typed !== "YES") {{
                  ev.preventDefault();
                  alert("Unchecking web config turns off this entire UI. Type YES in the confirm box, then save.");
                }}
              }}
            }});
            </script>
          </main>
        </div>
        """
        return web.Response(text=render_page(body), content_type="text/html")

    async def handle_home(self, request):
        if not self.authorized(request):
            raise web.HTTPFound("/login")
        raise web.HTTPFound("/plugin/bot")

    async def start_site(self):
        app = web.Application()
        app.add_routes(
            [
                web.get("/plugins", self.handle_plugins),
                web.post("/plugins", self.handle_plugins),
                web.get("/", self.handle_home),
                web.get("/login", self.handle_login),
                web.post("/login", self.handle_login),
                web.get("/plugin/{plugin_id}", self.handle_plugin),
                web.post("/plugin/{plugin_id}", self.handle_plugin),
                web.post("/plugin/{plugin_id}/action", self.handle_action),
                web.get("/api/roster", self.handle_roster_search),
                web.get("/api/roles", self.handle_role_search),
                web.get("/api/channels", self.handle_channel_search),
                web.get("/api/ranks", self.handle_rank_search),
                web.get("/api/role-names", self.handle_role_name_search),
            ]
        )
        host = str(self.settings.get("WEB_HOST") or "127.0.0.1")
        port = int(self.settings.get("WEB_PORT") or 8080)
        access_logger = setup_web_logging()
        self.runner = web.AppRunner(app, access_log=access_logger)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        print(f"[WebConfig] Config UI on http://{host}:{port}")

    async def stop_site(self):
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.site is None:
            try:
                await self.start_site()
            except Exception as exc:
                print(f"[WebConfig] Failed to start: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(WebConfig(bot))
