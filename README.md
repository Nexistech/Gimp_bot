# Gimp Bot
A self-hosted Discord bot for a Torn City faction. It keeps a shared faction roster, then layers monitors, alerts, banking, and verification on top of that cache so plugins are not each 
hammering the Torn API. It is meant as a replacement for the Tornium features this faction actually uses. Configuration is a local web UI, not a pile of `nano` sessions.
## Plugins
Turn each of these on or off from the web UI **Plugins** page. Disabled folders are not imported at startup. A plugin that errors on load is skipped; the rest of the bot still starts. 
`/reload` does the same and lists failures instead of stopping.
### Core
- **web_config** — Local aiohttp UI on `127.0.0.1:8080`. Left nav is Bot, Plugins, then every enabled cog that exposes settings or tables. Unchecking web_config requires typing YES. - 
**faction_roster** — Shared Torn member cache (name, rank, OC status, Discord ID). Other plugins should read this instead of calling the members API themselves. - **example** — 
Commented template only. Copy `plugins/example/cog.py` when you add a feature. Leave it disabled unless you are testing `/example_ping`.
### Roster and ranks
- **rank_monitor** — After the Torn 72-hour Recruit lock, flag Member → Fluffer (not in an OC) or Fluffer → Talent (joined an OC). Talent does not slide back. - **freeloader** 
— Members with no recent OC participation; restore alerts when a Freeloader joins an OC. - **oc_nudge** — Ping members who have been in the faction long enough and are still not in 
an OC. Ignore list is per-user.
### Organized crimes
- **oc_tools** — Alert when a *filled* OC slot is missing its listed tool. Empty slots are ignored. Item names come from a local catalog refreshed about weekly. - **oc_cpr** — 
Per-crime-level min/max CPR. One alert per crime that is out of range. Can mention the member when the roster has their Discord ID. - **chain_alert** — Warn before the chain timer 
dies. Separate seconds-before-timeout for 25 through 100000 hits.
### Faction ops
- **daily_digest** — One embed of rank / idle / tools / CPR sections. Per-section exclude lists. Run hour and minute are configurable; the embed itself has no clock. - **armory** — 
Track minimum qty for items that are already in the armory. Loans do not count as in stock. - **banking** — `/withdraw` with k/m/b/all, vault check, claim/fulfill, cancel, payout 
confirm via funds news. - **overdose** — Faction news ODs, yearly + all-time counts, optional snark from SQLite. One-time personalstats seed for all-time totals. - **shoplifting** 
— Torn store-down alerts. Users subscribe per store. - **strike_management** — Record / look up / clear strikes. Still informational unless you add punishment later.
### Discord access
- **verification** — Uses Torn’s official Discord link / OAuth only. This bot does not run that OAuth. Torn-User = linked account. Faction ranks map to Discord roles. Unverified = 
no Torn link. 24h lockout on member `/verify`. Admins can `/verify` anyone and run `/verifyall`. Test mode reports changes without applying them.
## Writing another plugin
1. Copy `plugins/example/cog.py` to `plugins/<name>/cog.py`. 2. Rename the class. Keep `async def setup(bot)`. 3. Put tunables in `SETTINGS_SCHEMA`. First load writes 
`plugins/<name>/settings.json`. 4. Read members from `self.bot.get_cog("FactionRoster")`. 5. Cancel tasks in `cog_unload` so `/reload` does not double-run them. 6. Optional: `web_tables()` 
and `web_action()` for extra forms on that plugin’s config page. 7. Leave the new folder disabled until it loads cleanly. A failed new module does not take the rest of the bot down.
## Requirements
- Python 3.10+ - A Discord bot application with `bot` + `applications.commands` - A Torn API key with **Limited Access** and **Faction API Access** (vault, news, armory, members) - 
Suggested Discord permission integer: `7882367117519936`
  View channel, send/embed, slash commands, manage roles, manage nicknames. Put the bot role **above** every role it assigns. Python packages: ```text discord.py aiohttp ```
## Layout
```text bot.py # loader, logging, /reload, /reboot config.py # TOKEN only (and anything you refuse to put in settings.json) plugin_settings.py # settings.json loader + ENABLED_PLUGINS 
settings.json # root bot settings (gitignored) run_check.sh # start / stop / cron watchdog bot.log plugins/<name>/
  cog.py settings.json # created on first load *.db # plugin data ``` Secrets and live data stay off Git. Keep `config.py`, `settings.json`, `*.db`, `*.log`, and `bot.pid` in `.gitignore`.
## First-time setup
1. Clone the repo onto the box that will run the bot. 2. Create a venv if you want one, then `pip install discord.py aiohttp`. 3. `config.py` only needs: ```python import os TOKEN = 
   os.getenv("DISCORD_BOT_TOKEN", "") TORN_API_KEY = os.getenv("TORN_API_KEY", "") ``` Prefer environment variables over pasting keys into the file.
4. Invite the bot with the permission integer above and `scope=bot applications.commands`. 5. Start it: ```bash chmod +x run_check.sh ./run_check.sh start ``` 6. SSH tunnel the web UI (it 
binds to `127.0.0.1:8080` by default):
   **PuTTY:** Connection → SSH → Tunnels → Source port `8080`, Destination `127.0.0.1:8080`, Add, then open the session. Browser: `http://127.0.0.1:8080` 7. Log in with the 
password from root `settings.json` (`WEB_PASSWORD`, default `change-me` — change it).
## Config UI
- **Bot** — guild ID, spam channel, allowed role, web bind/password. - **Plugins** — enable/disable cogs. Unchecking **web_config** requires typing `YES`. To get the UI back, put 
`"web_config"` in `ENABLED_PLUGINS` and restart. - Each plugin page — that cog’s settings, role/channel/member pickers, and extra tables (armory stock, OD counts, rank → 
Discord roles, and so on). Saving a non-web-config plugin reloads that cog. Host/port changes need a restart.
## Day-to-day
```bash ./run_check.sh status ./run_check.sh restart ./run_check.sh stop ``` Cron can call `./run_check.sh check` so a dead process comes back. Slash commands (owner / staff / members 
depending on the cog): - `/reload` `/reboot` — owner - `/withdraw` `/balance` — verified members - `/verify` `/verifyall` — members verify themselves; admins verify anyone and 
can run a server pass - `/digest`, strike, shoplifting, and OC helper commands as each plugin defines them `/verify` is rate-limited to once per 24 hours for regular members. Admins are 
not limited. Verification **does not** run Torn OAuth. People link Discord on [Torn’s official Discord](https://www.torn.com/discord) or Torn’s OAuth page; the bot only reads the 
public API after that. Verification has a **test mode** (on by default) that reports role/nick changes without applying them. Map ranks, dry-run `/verifyall`, then turn test mode off.
## Git workflow
Edit on the VPS or pull from GitHub: ```bash git pull ./run_check.sh restart ``` Do not commit tokens, `settings.json`, or SQLite files.
## Notes
- One process only. A leftover `python3 bot.py` will double-post banking and verify messages. `./run_check.sh stop` then `pkill -f bot.py` if `bot.pid` lies. - Roster Discord IDs come from 
Torn’s user/discord lookup. A 403 usually means that player never linked Discord, not a broken key. - Banking fulfill links use `giveMoneyTo` + `money` on the faction give-to-user 
hash. Claim the request so two bankers do not pay the same line. - Logs: `bot.log` for the bot, `plugins/web_config/web_config.log` for HTTP access.
## Disclaimer
Unofficial. Not affiliated with Torn or Tornium. Use a faction key you are allowed to use, stay inside Torn’s API rules, and do not ask members for passwords or personal API keys.
