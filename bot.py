import os
import random
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])
KEYWORD = os.getenv("KEYWORD", "").strip().casefold()
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", "recalled.db"))
GUILD_ID = os.getenv("GUILD_ID")

if not KEYWORD:
    raise RuntimeError("KEYWORD must be set in .env")


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pruned_messages (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            channel_id TEXT PRIMARY KEY,
            checkpoint TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def load_messages(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute("SELECT id, content FROM messages ORDER BY rowid").fetchall()
    return [{"id": row["id"], "content": row["content"]} for row in rows]


def load_pruned_ids(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT id FROM pruned_messages").fetchall()
    return {row["id"] for row in rows}


def load_sync_state(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT channel_id, checkpoint FROM sync_state").fetchall()
    return {row["channel_id"]: row["checkpoint"] for row in rows}


def save_sync_checkpoint(connection: sqlite3.Connection, channel_id: str, checkpoint: str) -> None:
    connection.execute(
        "INSERT INTO sync_state(channel_id, checkpoint) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET checkpoint = excluded.checkpoint",
        (channel_id, checkpoint),
    )
    connection.commit()


def insert_message(connection: sqlite3.Connection, message_id: str, content: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO messages(id, content) VALUES (?, ?)",
        (message_id, content),
    )


def delete_messages(connection: sqlite3.Connection, message_ids: set[str]) -> None:
    connection.executemany("DELETE FROM messages WHERE id = ?", ((message_id,) for message_id in message_ids))
    connection.executemany(
        "INSERT OR IGNORE INTO pruned_messages(id) VALUES (?)",
        ((message_id,) for message_id in message_ids),
    )
    connection.commit()


NON_NOUN_WORDS = {
    "jesse", "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any",
    "are", "aren't", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "can't", "cannot", "could", "couldn't", "did",
    "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during", "each", "few",
    "for", "from", "further", "had", "hadn't", "has", "hasn't", "have", "haven't", "having",
    "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", "him", "himself",
    "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't",
    "it", "it's", "its", "itself", "just", "me", "more", "most", "mustn't", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours",
    "ourselves", "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's",
    "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the", "their",
    "theirs", "them", "themselves", "then", "there", "there's", "these", "they", "they'd",
    "they'll", "they're", "they've", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't",
    "what", "what's", "when", "when's", "where", "where's", "which", "while", "who", "who's",
    "whom", "why", "why's", "will", "with", "won't", "would", "wouldn't", "you", "you'd",
    "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves",
}


def message_words(content: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9']+", content.casefold()))
    return {word for word in words if len(word) > 1 and word not in NON_NOUN_WORDS}


def weighted_reply(query: str) -> str:
    query_words = message_words(query)
    eligible_messages = [
        message
        for message in stored_messages
        if len(message["content"]) >= response_filter["min_length"]
        and (
            not response_filter["contains_text"]
            or response_filter["contains_text"] in message["content"].casefold()
        )
    ]
    if not eligible_messages:
        return ""

    weights: list[int] = []

    for message in eligible_messages:
        matching_words = query_words & message_words(message["content"])
        weights.append(1 + min(len(matching_words), 3))

    return random.choices(eligible_messages, weights=weights, k=1)[0]["content"]


database = connect_database()
stored_messages = load_messages(database)
pruned_ids = load_pruned_ids(database)
sync_state = load_sync_state(database)
active_sync_channels: set[int] = set()
sync_stop_requests: set[int] = set()
response_filter = {"min_length": 0, "contains_text": ""}
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook() -> None:
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    else:
        await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    if not rotate_status.is_running():
        rotate_status.start()
    print(f"Logged in as {bot.user} with {len(stored_messages)} stored messages.")


@tasks.loop(minutes=2)
async def rotate_status() -> None:
    if not stored_messages:
        return
    status_text = random.choice(stored_messages)["content"].replace("\n", " ").strip()
    await bot.change_presence(activity=discord.Game(name=status_text[:128]))


@bot.tree.command(name="sync", description="Import your messages from this channel")
@discord.app_commands.guild_only()
@discord.app_commands.describe(
    since_date="Scan backward from now to this date, YYYY-MM-DD",
)
async def sync(
    interaction: discord.Interaction,
    since_date: str | None = None,
) -> None:
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
        after = (
            datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if since_date
            else None
        )
    except ValueError:
        await interaction.followup.send("Dates must use YYYY-MM-DD format, for example 2025-01-31.")
        return

    channel_state_key = str(interaction.channel.id)
    channel_id = interaction.channel.id
    if active_sync_channels:
        await interaction.followup.send("A sync is already running. Stop it before starting another one.")
        return

    checkpoint = None if since_date else sync_state.get(channel_state_key)
    if checkpoint:
        try:
            checkpoint_time = datetime.fromisoformat(checkpoint)
        except ValueError:
            checkpoint_time = None
    else:
        checkpoint_time = None

    active_sync_channels.add(channel_id)
    stopped = False
    try:
        async for message in interaction.channel.history(
            limit=None,
            before=checkpoint_time or datetime.now(timezone.utc),
            after=after,
            oldest_first=False,
        ):
            if channel_id in sync_stop_requests:
                sync_stop_requests.remove(channel_id)
                stopped = True
                break

            scanned += 1
            sync_state[channel_state_key] = message.created_at.isoformat()
            message_id = str(message.id)
            if message.author.id == OWNER_USER_ID and message.content and message_id not in known_ids:
                stored_messages.append({"id": message_id, "content": message.content})
                insert_message(database, message_id, message.content)
                known_ids.add(message_id)
                imported += 1
            if scanned % 500 == 0:
                save_sync_checkpoint(database, channel_state_key, sync_state[channel_state_key])
                database.commit()
                await interaction.edit_original_response(
                    content=f"Still scanning history... {scanned:,} messages checked, {imported:,} imported."
                )
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot read this channel's history. Grant me View Channel and Read Message History, then try again."
        )
        return
    finally:
        active_sync_channels.discard(channel_id)

    save_sync_checkpoint(database, channel_state_key, sync_state.get(channel_state_key, ""))
    database.commit()
    resume_note = " This channel will resume from this point next time." if stopped or not since_date else ""
    await interaction.followup.send(
        f"{'Stopped after' if stopped else 'Checked'} {scanned:,} message(s), imported {imported:,}, "
        f"and now have {len(stored_messages):,} stored messages."
        f"{resume_note}"
    )


@bot.tree.command(name="stop_sync", description="Stop the active sync in this channel")
@discord.app_commands.guild_only()
async def stop_sync(interaction: discord.Interaction) -> None:
    """Request that the active channel sync stop at its next batch."""
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can stop a sync.", ephemeral=True)
        return
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("This command must be run in a text channel.", ephemeral=True)
        return
    if interaction.channel.id not in active_sync_channels:
        await interaction.response.send_message("There is no active sync in this channel.", ephemeral=True)
        return

    sync_stop_requests.add(interaction.channel.id)
    await interaction.response.send_message("Stopping the sync after the current batch...", ephemeral=True)


@bot.tree.command(name="prune", description="Remove unwanted stored messages")
@discord.app_commands.describe(
    contains_text="Remove messages containing this text",
    min_length="Remove messages shorter than this many characters",
    remove_commands="Remove messages beginning with ! or /",
    remove_duplicates="Keep only the first copy of duplicate text",
    dry_run="Preview the number removed without changing the file",
)
async def prune(
    interaction: discord.Interaction,
    contains_text: str | None = None,
    min_length: discord.app_commands.Range[int, 0, 500] = 0,
    remove_commands: bool = False,
    remove_duplicates: bool = False,
    dry_run: bool = True,
) -> None:
    """Remove selected message types from the local response pool."""
    global stored_messages

    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can prune messages.", ephemeral=True)
        return
    if not any((contains_text, min_length, remove_commands, remove_duplicates)):
        await interaction.response.send_message("Choose at least one pruning rule.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    kept_messages: list[dict[str, str]] = []
    removed_ids: set[str] = set()
    seen_content: set[str] = set()

    for message in stored_messages:
        content = message["content"]
        should_remove = (
            (contains_text is not None and contains_text.casefold() in content.casefold())
            or
            len(content) < min_length
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
        delete_messages(database, removed_ids)

    action = "would remove" if dry_run else "removed"
    await interaction.followup.send(
        f"{action.capitalize()} {len(removed_ids):,} message(s). "
        f"{len(kept_messages):,} message(s) would remain."
    )


@bot.tree.command(name="delete_db", description="Delete all locally stored bot data")
@discord.app_commands.describe(confirm="Must be true to permanently delete local data")
async def delete_db(interaction: discord.Interaction, confirm: bool = False) -> None:
    """Delete the local message store, prune records, and sync checkpoints."""
    global stored_messages, pruned_ids, sync_state

    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can delete stored data.", ephemeral=True)
        return
    if not confirm:
        await interaction.response.send_message(
            "This permanently deletes all locally stored messages and sync checkpoints. "
            "Run `/delete_db confirm:true` to continue.",
            ephemeral=True,
        )
        return
    if active_sync_channels:
        await interaction.response.send_message(
            "Stop all active syncs before deleting stored data.",
            ephemeral=True,
        )
        return

    stored_messages = []
    pruned_ids = set()
    sync_state = {}
    database.execute("DELETE FROM messages")
    database.execute("DELETE FROM pruned_messages")
    database.execute("DELETE FROM sync_state")
    database.commit()

    await interaction.response.send_message(
        "Deleted all locally stored messages, prune records, and sync checkpoints.",
        ephemeral=True,
    )


@bot.tree.command(name="set_filter", description="Temporarily limit which stored replies can be used")
@discord.app_commands.describe(
    min_length="Only use replies at least this many characters long",
    contains_text="Only use replies containing this text or phrase",
)
async def set_filter(
    interaction: discord.Interaction,
    min_length: discord.app_commands.Range[int, 0, 2000] = 0,
    contains_text: str | None = None,
) -> None:
    """Set an in-memory response filter without deleting stored messages."""
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can set response filters.", ephemeral=True)
        return
    if min_length == 0 and not contains_text:
        await interaction.response.send_message(
            "Choose a min_length or contains_text filter, or use `/clear_filter`.",
            ephemeral=True,
        )
        return

    response_filter["min_length"] = min_length
    response_filter["contains_text"] = contains_text.casefold() if contains_text else ""
    details = []
    if min_length:
        details.append(f"at least {min_length} characters")
    if contains_text:
        details.append(f"containing `{contains_text}`")
    await interaction.response.send_message(
        f"Temporary response filter set: {' and '.join(details)}. Stored data was not changed.",
        ephemeral=True,
    )


@bot.tree.command(name="clear_filter", description="Remove the temporary response filter")
async def clear_filter(interaction: discord.Interaction) -> None:
    """Restore the full stored message pool for replies."""
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Only the configured owner can clear response filters.", ephemeral=True)
        return

    response_filter["min_length"] = 0
    response_filter["contains_text"] = ""
    await interaction.response.send_message("Temporary response filter cleared.", ephemeral=True)


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
        reply = weighted_reply(message.content)
        if reply:
            await message.channel.send(reply)

bot.run(TOKEN)