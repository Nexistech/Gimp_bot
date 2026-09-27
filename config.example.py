import os
import discord

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DEV_GUILD_ID = 0
ALLOWED_ROLE_NAME = "Concierge"
SPAM_CHANNEL_ID = 0

INTENTS = discord.Intents.default()
INTENTS.members = True

DB_NAME = "strike_management.db"
ROLES_TO_TAG = ["Leadership", "Concierge"]

TORN_API_KEY = os.getenv("TORN_API_KEY", "")
FACTION_MEMBERS_URL = "https://api.torn.com/v2/faction/members"
FACTION_CRIMES_URL = "https://api.torn.com/v2/faction/crimes"
FACTION_MEMBER_JSON_URL = "https://api.torn.com/v2/faction/members?striptags=true"
FACTION_ACTIVITY_URL = "https://api.torn.com/v2/faction/basic,crimes,members?cat=available,completed&striptags=true"

FREELOADER_CHANNEL_ID = 0
FREELOADER_EXEMPT_IDS = []

SHOPLIFTING_CHANNEL_ID = 0
