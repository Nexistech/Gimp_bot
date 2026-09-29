# Gimp Bot

A self-hosted Discord bot for a Torn City faction. It keeps a shared faction roster, then layers monitors, alerts, banking, and verification on top of that cache so plugins are not each hammering the Torn API.

It is meant as a replacement for the Tornium features this faction actually uses. Configuration is a local web UI, not a pile of `nano` sessions.

## What it does

| Plugin | Job |
| --- | --- |
| **faction_roster** | Shared cache of members, ranks, OC status, Discord IDs |
| **rank_monitor** | Recruit wait → Fluffer / Talent prompts |
| **freeloader** | Idle OC participation reviews |
| **oc_nudge** | Ping people who have been in the faction long enough and are not in an OC |
| **oc_tools** | Missing OC tools / consumables |
| **oc_cpr** | CPR outside the per-level ranges you set |
| **daily_digest** | One summary of the above, with per-section excludes |
| **shoplifting** | Store-down alerts with user subscriptions |
| **strike_management** | Strike tracking and lookup |
| **armory** | Track minimum stock of items already in the armory |
| **banking** | `/withdraw` against vault balance, claim/fulfill flow |
| **overdose** | Faction OD alerts with yearly / all-time counts and optional snark |
| **chain_alert** | Warn before a chain timer dies, per milestone |
| **verification** | Torn-official Discord link → roles + nick. Dry-run first. |
| **web_config** | Browser UI for every plugin setting |

Plugins can be turned off from the **Plugins** page. Disabled cogs are not loaded on startup.

## Requirements

- Python 3.10+
- A Discord bot application with `bot` + `applications.commands`
- A Torn API key with **Limited Access** and **Faction API Access** (vault, news, armory, members)
- Suggested Discord permission integer: `7882367117519936`  
  View channel, send/embed, slash commands, manage roles, manage nicknames. Put the bot role **above** every role it assigns.

Python packages:

```text
discord.py
aiohttp
```

## Layout

```text
bot.py                 # loader, logging, /reload, /reboot
config.py              # TOKEN only (and anything you refuse to put in settings.json)
plugin_settings.py     # settings.json loader + ENABLED_PLUGINS
settings.json          # root bot settings (gitignored)
run_check.sh           # start / stop / cron watchdog
bot.log
plugins/<name>/
  cog.py
  settings.json        # created on first load
  *.db                 # plugin data
```

Secrets and live data stay off Git. Keep `config.py`, `settings.json`, `*.db`, `*.log`, and `bot.pid` in `.gitignore`.

## First-time setup

1. Clone the repo onto the box that will run the bot.
2. Create a venv if you want one, then `pip install discord.py aiohttp`.
3. `config.py` only needs:

   ```python
   import os
   TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
   TORN_API_KEY = os.getenv("TORN_API_KEY", "")
   ```

   Prefer environment variables over pasting keys into the file.
4. Invite the bot with the permission integer above and `scope=bot applications.commands`.
5. Start it:

   ```bash
   chmod +x run_check.sh
   ./run_check.sh start
   ```

6. SSH tunnel the web UI (it binds to `127.0.0.1:8080` by default):

   **PuTTY:** Connection → SSH → Tunnels → Source port `8080`, Destination `127.0.0.1:8080`, Add, then open the session.  
   Browser: `http://127.0.0.1:8080`

7. Log in with the password from root `settings.json` (`WEB_PASSWORD`, default `change-me` — change it).

## Config UI

- **Bot** — guild ID, spam channel, allowed role, web bind/password.
- **Plugins** — enable/disable cogs. Unchecking **web_config** requires typing `YES`. To get the UI back, put `"web_config"` in `ENABLED_PLUGINS` and restart.
- Each plugin page — that cog’s settings, role/channel/member pickers, and extra tables (armory stock, OD counts, rank → Discord roles, and so on).

Saving a non-web-config plugin reloads that cog. Host/port changes need a restart.

## Day-to-day

```bash
./run_check.sh status
./run_check.sh restart
./run_check.sh stop
```

Cron can call `./run_check.sh check` so a dead process comes back.

Slash commands (owner / staff / members depending on the cog):

- `/reload` `/reboot` — owner
- `/withdraw` `/balance` — verified members
- `/verify` `/verifyall` — members verify themselves; admins verify anyone and can run a server pass
- `/digest`, strike, shoplifting, and OC helper commands as each plugin defines them

`/verify` is rate-limited to once per 24 hours for regular members. Admins are not limited. Verification **does not** run Torn OAuth. People link Discord on [Torn’s official Discord](https://www.torn.com/discord) or Torn’s OAuth page; the bot only reads the public API after that.

Verification has a **test mode** (on by default) that reports role/nick changes without applying them. Map ranks, dry-run `/verifyall`, then turn test mode off.

## Git workflow

Edit on the VPS or pull from GitHub:

```bash
git pull
./run_check.sh restart
```

Do not commit tokens, `settings.json`, or SQLite files.

## Notes

- One process only. 
- Roster Discord IDs come from Torn’s user/discord lookup. A 403 usually means that player never linked Discord, not a broken key.
- Banking fulfill links use `giveMoneyTo` + `money` on the faction give-to-user hash. Claim the request so two bankers do not pay the same line.
- Logs: `bot.log` for the bot, `plugins/web_config/web_config.log` for HTTP access.

## Disclaimer

Unofficial. Not affiliated with Torn or Tornium. Use a faction key you are allowed to use, stay inside Torn’s API rules, and do not ask members for passwords or personal API keys.
