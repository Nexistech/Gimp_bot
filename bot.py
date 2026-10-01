import discord
from discord.ext import commands
import os
import sys
import atexit
import logging
from logging.handlers import RotatingFileHandler

from config import TOKEN
from plugin_settings import enabled_plugins, load_root_settings, plugin_folders

PID_FILE = "bot.pid"
LOG_FILE = "bot.log"


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=3)
    file_handler.setFormatter(formatter)

    logger.handlers.clear()
    logger.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    return logging.getLogger("StrikeBot")


log = setup_logging()


def create_pid_file():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log.info("Wrote PID %s to %s", os.getpid(), PID_FILE)


def remove_pid_file():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
        log.info("Removed PID file")


atexit.register(remove_pid_file)


class StrikeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        plugins_dir = "./plugins"
        if not os.path.isdir(plugins_dir):
            log.error("Plugins directory not found: %s", os.path.abspath(plugins_dir))
            return

        folders = plugin_folders()
        enabled = enabled_plugins()
        skipped = [folder for folder in folders if folder not in enabled]
        log.info("Found plugin folders with cog.py: %s", ", ".join(folders) or "(none)")
        if skipped:
            log.info("Skipping disabled plugins: %s", ", ".join(skipped))

        for folder in enabled:
            extension_path = f"plugins.{folder}.cog"
            try:
                await self.load_extension(extension_path)
                log.info("Loaded plugin: %s (%s)", folder, extension_path)
            except Exception:
                log.exception("Failed to load plugin %s", folder)

        loaded = ", ".join(sorted(self.cogs.keys())) or "(none)"
        log.info("Cogs now loaded: %s", loaded)

    async def apply_plugin_enabled(self, enabled):
        enabled = set(enabled)
        results = []
        for folder in plugin_folders():
            extension = f"plugins.{folder}.cog"
            should = folder in enabled
            loaded = extension in self.extensions
            if should and loaded:
                continue
            if should and not loaded:
                try:
                    await self.load_extension(extension)
                    results.append(f"loaded {folder}")
                    log.info("Enabled plugin: %s", folder)
                except Exception as exc:
                    results.append(f"failed to load {folder}: {exc}")
                    log.exception("Failed to load plugin %s", folder)
            if not should and loaded:
                if folder == "web_config":
                    results.append("web_config disabled; unload after this request")
                    continue
                try:
                    await self.unload_extension(extension)
                    results.append(f"unloaded {folder}")
                    log.info("Disabled plugin: %s", folder)
                except Exception as exc:
                    results.append(f"failed to unload {folder}: {exc}")
                    log.exception("Failed to unload plugin %s", folder)
        try:
            await self.sync_guild_commands()
        except Exception:
            log.exception("Command sync after plugin toggle failed")
        return results

    async def sync_guild_commands(self):
        guild_id = int(load_root_settings().get("DEV_GUILD_ID") or 0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=None)
            log.info(
                "Synced %s command(s) to DEV_GUILD_ID %s and cleared global commands.",
                len(synced),
                guild_id,
            )
        else:
            synced = await self.tree.sync()
            log.info("Synced %s command(s) globally.", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Cogs at ready: %s", ", ".join(sorted(self.cogs.keys())) or "(none)")
        try:
            await self.sync_guild_commands()
        except Exception:
            log.exception("Command sync failed")


bot = StrikeBot()


@bot.tree.command(name="reload", description="Reload all plugins without restarting.")
async def reload(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("You are not the owner.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    reloaded = []
    failed = []
    enabled = set(enabled_plugins())
    for folder in plugin_folders():
        if enabled and folder not in enabled:
            continue
        extension = f"plugins.{folder}.cog"
        try:
            await bot.reload_extension(extension)
            reloaded.append(folder)
            log.info("Reloaded plugin: %s", folder)
        except commands.ExtensionNotLoaded:
            try:
                await bot.load_extension(extension)
                reloaded.append(f"{folder} (loaded new)")
                log.info("Loaded new plugin on reload: %s", folder)
            except Exception as e:
                log.exception("Failed to load %s during reload", folder)
                failed.append(f"{folder}: {e}")
        except Exception as e:
            log.exception("Failed to reload %s", folder)
            failed.append(f"{folder}: {e}")
    lines = []
    if reloaded:
        lines.append("Reloaded: " + ", ".join(reloaded))
    if failed:
        lines.append("Failed: " + "; ".join(failed))
    if not lines:
        lines.append("No plugins to reload.")
    try:
        await bot.sync_guild_commands()
        lines.append("Command tree re-synced.")
    except Exception as e:
        log.exception("Reload command sync failed")
        lines.append(f"Command sync failed: {e}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="check", description="Show bot and plugin health.")
async def check(interaction: discord.Interaction):
    watchdog = bot.get_cog("Watchdog")
    if watchdog and hasattr(watchdog, "send_status"):
        return await watchdog.send_status(interaction)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    cogs = ", ".join(sorted(bot.cogs.keys())) or "(none)"
    await interaction.followup.send(
        f"Watchdog is not loaded.\nLogged in as `{bot.user}`\nCogs: {cogs}\nPID `{os.getpid()}`",
        ephemeral=True,
    )


@bot.tree.command(name="reboot", description="Restart the bot process.")
async def reboot(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("You are not the owner.", ephemeral=True)
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message("Rebooting bot...", ephemeral=True)
        except discord.HTTPException:
            pass
    log.info("Reboot requested by %s", interaction.user)
    remove_pid_file()
    os.execl(sys.executable, sys.executable, *sys.argv)


if __name__ == "__main__":
    create_pid_file()
    log.info("Starting bot")
    bot.run(TOKEN, log_handler=None)
