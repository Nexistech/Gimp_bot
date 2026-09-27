import discord
from discord.ext import commands
import os
import sys
import importlib
import atexit
from config import TOKEN, DEV_GUILD_ID

# Define the PID file path
PID_FILE = "bot.pid"

def create_pid_file():
    """Writes the current process ID to a file."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def remove_pid_file():
    """Removes the PID file if it exists."""
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

# Register the cleanup to run on script exit
atexit.register(remove_pid_file)

class StrikeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        plugins_dir = "./plugins"
        for folder in os.listdir(plugins_dir):
            if os.path.isdir(os.path.join(plugins_dir, folder)):
                extension_path = f"plugins.{folder}.cog"
                try:
                    await self.load_extension(extension_path)
                    print(f"Loaded plugin: {folder}")
                except Exception as e:
                    print(f"Failed to load plugin {folder}: {e}")

    async def sync_guild_commands(self):
        """Copies global commands to DEV_GUILD_ID and purges old global duplicates."""
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            
            # 1. Sync commands directly to your DEV_GUILD_ID (Instant)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            
            # 2. Clear global commands from Discord's cache to prevent duplicate / commands
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=None)
            
            print(f"✅ Synced {len(synced)} command(s) instantly to DEV_GUILD_ID ({DEV_GUILD_ID}) & cleared global duplicates.")
        else:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} command(s) globally.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.sync_guild_commands()

bot = StrikeBot()

@bot.tree.command(name="reload", description="Reload all plugins without restarting.")
async def reload(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("You are not the owner.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    plugins_dir = "./plugins"
    reloaded = []
    for folder in os.listdir(plugins_dir):
        if os.path.isdir(os.path.join(plugins_dir, folder)):
            extension = f"plugins.{folder}.cog"
            try:
                await bot.reload_extension(extension)
                reloaded.append(folder)
            except Exception as e:
                return await interaction.followup.send(f"Failed to reload {folder}: {e}")
    
    try:
        await bot.sync_guild_commands()
        await interaction.followup.send(f"Reloaded plugins ({', '.join(reloaded)}) and re-synced command tree!")
    except Exception as e:
        await interaction.followup.send(f"Plugins reloaded, but command sync failed: {e}")

@bot.tree.command(name="reboot", description="Restart the bot process.")
async def reboot(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("You are not the owner.", ephemeral=True)

    await interaction.response.send_message("Rebooting bot...", ephemeral=True)
    remove_pid_file()
    os.execl(sys.executable, sys.executable, *sys.argv)

if __name__ == "__main__":
    create_pid_file()
    bot.run(TOKEN)
