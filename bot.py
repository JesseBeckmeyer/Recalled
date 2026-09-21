import json
import os
import random
import re
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])
KEYWORD = os.getenv("KEYWORD", "").strip().casefold()
DATA_FILE = Path(os.getenv("DATA_FILE", "messages.json"))
PRUNED_FILE = DATA_FILE.with_name("pruned_ids.json")
GUILD_ID = os.getenv("GUILD_ID")

if not KEYWORD:
    raise RuntimeError("KEYWORD must be set in .env")


def load_messages() -> list[dict[str, str]]:
    if not DATA_FILE.exists():
        return []
    with DATA_FILE.open(encoding="utf-8") as file:
        messages = json.load(file)
    if not isinstance(messages, list):
        return []
    return [
        {"id": message.get("id", "legacy"), "content": message["content"]}
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str) and message["content"]
    ]


def save_messages(messages: list[dict[str, str]]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(messages, file, ensure_ascii=False, indent=2)


def load_pruned_ids() -> set[str]:
    if not PRUNED_FILE.exists():
        return set()
    with PRUNED_FILE.open(encoding="utf-8") as file:
        ids = json.load(file)
    return {str(message_id) for message_id in ids} if isinstance(ids, list) else set()


def save_pruned_ids(message_ids: set[str]) -> None:
    with PRUNED_FILE.open("w", encoding="utf-8") as file:
        json.dump(sorted(message_ids), file, indent=2)


stored_messages = load_messages()
pruned_ids = load_pruned_ids()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook() -> None:
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} with {len(stored_messages)} stored messages.")


@bot.tree.command(name="sync", description="Import your messages from this channel")
@discord.app_commands.guild_only()
async def sync(interaction: discord.Interaction) -> None:
    """Import the owner's messages from the channel where this command is used."""
    global stored_messages

    await interaction.response.defer()
    imported = 0
    scanned = 0
    known_ids = {message["id"] for message in stored_messages} | pruned_ids
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("This command must be run in a text channel.")
        return
    try:
        async for message in interaction.channel.history(limit=None, oldest_first=True):
            scanned += 1
            message_id = str(message.id)
            if message.author.id == OWNER_USER_ID and message.content and message_id not in known_ids:
                stored_messages.append({"id": message_id, "content": message.content})
                known_ids.add(message_id)
                imported += 1
            if scanned % 500 == 0:
                await interaction.edit_original_response(
                    content=f"Still scanning history... {scanned:,} messages checked, {imported:,} imported."
                )
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot read this channel's history. Grant me View Channel and Read Message History, then try again."
        )
        return

    save_messages(stored_messages)
    await interaction.followup.send(
        f"Checked {scanned:,} message(s), imported {imported:,}, and now have {len(stored_messages):,} stored messages."
    )


@bot.tree.command(name="prune", description="Remove unwanted stored messages")
@discord.app_commands.describe(
    contains_text="Remove messages containing this text",
    min_length="Remove messages shorter than this many characters",
    remove_links="Remove messages containing links",
    remove_commands="Remove messages beginning with ! or /",
    remove_duplicates="Keep only the first copy of duplicate text",
    dry_run="Preview the number removed without changing the file",
)
async def prune(
    interaction: discord.Interaction,
    contains_text: str | None = None,
    min_length: discord.app_commands.Range[int, 0, 500] = 0,
    remove_links: bool = False,
    remove_commands: bool = False,
    remove_duplicates: bool = False,
    dry_run: bool = True,
) -> None:
    """Remove selected message types from the local response pool."""
    global stored_messages

    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can prune messages.", ephemeral=True)
        return
    if not any((contains_text, min_length, remove_links, remove_commands, remove_duplicates)):
        await interaction.response.send_message("Choose at least one pruning rule.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    link_pattern = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
    kept_messages: list[dict[str, str]] = []
    removed_ids: set[str] = set()
    seen_content: set[str] = set()

    for message in stored_messages:
        content = message["content"]
        should_remove = (
            (contains_text is not None and contains_text.casefold() in content.casefold())
            or
            len(content) < min_length
            or (remove_links and link_pattern.search(content) is not None)
            or (remove_commands and content.lstrip().startswith(("!", "/")))
            or (remove_duplicates and content in seen_content)
        )
        if should_remove:
            removed_ids.add(message["id"])
        else:
            kept_messages.append(message)
            seen_content.add(content)

    if not dry_run:
        stored_messages = kept_messages
        pruned_ids.update(removed_ids)
        save_messages(stored_messages)
        save_pruned_ids(pruned_ids)

    action = "would remove" if dry_run else "removed"
    await interaction.followup.send(
        f"{action.capitalize()} {len(removed_ids):,} message(s). "
        f"{len(kept_messages):,} message(s) would remain."
    )


@bot.tree.command(name="stats", description="Show how many messages are stored")
async def stats(interaction: discord.Interaction) -> None:
    """Show how many messages are available for replies."""
    await interaction.response.send_message(
        f"I have {len(stored_messages)} stored message(s) for keyword `{KEYWORD}`."
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    if KEYWORD in message.content.casefold() and stored_messages:
        await message.channel.send(random.choice(stored_messages)["content"])

bot.run(TOKEN)