import discord
from discord.ext import commands
import os
import sys
import atexit
import logging
from logging.handlers import RotatingFileHandler

from config import TOKEN
from plugin_settings import load_root_settings

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

        folders = sorted(
            folder
            for folder in os.listdir(plugins_dir)
            if os.path.isdir(os.path.join(plugins_dir, folder))
            and not folder.startswith("__")
            and os.path.isfile(os.path.join(plugins_dir, folder, "cog.py"))
        )
        log.info("Found plugin folders with cog.py: %s", ", ".join(folders) or "(none)")

        for folder in folders:
            extension_path = f"plugins.{folder}.cog"
            try:
                await self.load_extension(extension_path)
                log.info("Loaded plugin: %s (%s)", folder, extension_path)
            except Exception:
                log.exception("Failed to load plugin %s", folder)

        loaded = ", ".join(sorted(self.cogs.keys())) or "(none)"
        log.info("Cogs now loaded: %s", loaded)

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
    plugins_dir = "./plugins"
    reloaded = []
    for folder in sorted(os.listdir(plugins_dir)):
        cog_file = os.path.join(plugins_dir, folder, "cog.py")
        if not os.path.isfile(cog_file):
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
            except Exception:
                log.exception("Failed to load %s during reload", folder)
                return await interaction.followup.send(f"Failed to load {folder}. See bot.log.")
        except Exception as e:
            log.exception("Failed to reload %s", folder)
            return await interaction.followup.send(f"Failed to reload {folder}: {e}")
    try:
        await bot.sync_guild_commands()
        await interaction.followup.send(
            f"Reloaded plugins ({', '.join(reloaded)}) and re-synced command tree!"
        )
    except Exception as e:
        log.exception("Reload command sync failed")
        await interaction.followup.send(f"Plugins reloaded, but command sync failed: {e}")


@bot.tree.command(name="reboot", description="Restart the bot process.")
async def reboot(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("You are not the owner.", ephemeral=True)
    await interaction.response.send_message("Rebooting bot...", ephemeral=True)
    log.info("Reboot requested by %s", interaction.user)
    remove_pid_file()
    os.execl(sys.executable, sys.executable, *sys.argv)


if __name__ == "__main__":
    create_pid_file()
    log.info("Starting bot")
    bot.run(TOKEN, log_handler=None)
