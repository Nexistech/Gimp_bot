import random

import discord
from discord import app_commands
from discord.ext import commands

FEET = [
    "```\n   _,--._\n  /      \\\n |  o  o  |~\n  \\  __  /\n   `----'\n    feet\n```",
    "```\n  ___\n /   \\\n|  .  |.~\n \\___/\n  | |\n _| |_\n```",
    "```\n   .-.\n  (   )\n   `-'\n  _| |_\n / | | \\\n|  | |  |\n```",
    "```\n   ___   ___\n  /   \\ /   \\\n |  .  |  .  |\n  \\___/ \\___/\n    |     |\n   _|     |_\n```",
    "```\n    .-.\n   ( . )\n    `-'\n   /| |\n  / | |\n /__|_|__\n```",
    "```\n  (\\_/) \n  ( . .) sock\n  /|_|\\\n   | |\n  _| |_\n```",
    "```\n   ___\n  (o o)\n   \\_/\n   / \\\n  /   \\\n /_____\\\n  lefty\n```",
    "```\n      ___\n     ( o )\n      \\_/\n     / \\\n ___/   \\\n|_______|\n   righty\n```",
    "```\n  .---.   .---.\n (  .  ) (  .  )\n  `---'   `---'\n    |       |\n   /|\\     /|\\\n  / | \\   / | \\\n```",
    "```\n     ___\n ___/   \\\n|  .     |\n|    ___/\n|___|\n  |\n _|_\n```",
    "```\n   .-\"\"-.\n  /  . . \\\n  \\  \\_/ /\n   '-----'\n     | |\n  ___| |___\n |_________|\n```",
    "```\n   _\n _| |_\n(  .  )\n |___|\n   |\n  / \\\n /___\\\n```",
    "```\n   /\\_/\\  no\n  ( o.o )  those\n   > ^ <   are paws\n    / \\\n   /   \\\n  (_____) \n```",
    "```\n  _______\n /  . .  \\\n|    v    |\n \\_______/\n    ||\n   _||_\n  |____|\n   pair\n```",
    "```\n   __   __\n  /  \\ /  \\\n | .  |  . |\n  \\__/ \\__/\n   ||   ||\n  _||_ _||_\n |____|____|\n```",
]


class FindFeet(commands.Cog):
    @app_commands.command(name="findfeet", description="A very serious lookup.")
    async def findfeet(self, interaction: discord.Interaction):
        await interaction.response.send_message(random.choice(FEET), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(FindFeet(bot))
