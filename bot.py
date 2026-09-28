import os
import json
import logging
import random
import re
import sqlite3
import shutil
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])
KEYWORD = os.getenv("KEYWORD", "").strip().casefold()
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", "recalled.db"))
SEED_DATABASE_FILE = Path(os.getenv("SEED_DATABASE_FILE", "/app/seed/recalled.db"))
LOG_FILE = Path(os.getenv("LOG_FILE", "recalled.log"))
GUILD_ID = os.getenv("GUILD_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger("recalled")


class StoredMessage(TypedDict):
    id: str
    content: str
    guild_id: str | None
    channel_id: str | None
    attachment_urls: list[str]

if not KEYWORD:
    raise RuntimeError("KEYWORD must be set in .env")


def seed_database_if_needed() -> None:
    if not SEED_DATABASE_FILE.exists() or SEED_DATABASE_FILE == DATABASE_FILE:
        return

    should_seed = not DATABASE_FILE.exists()
    if DATABASE_FILE.exists():
        try:
            with sqlite3.connect(DATABASE_FILE) as connection:
                stored_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            should_seed = stored_count == 0
        except sqlite3.DatabaseError:
            should_seed = True

    if should_seed:
        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SEED_DATABASE_FILE, DATABASE_FILE)
        logger.info("Seeded database from %s.", SEED_DATABASE_FILE)


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            attachment_urls TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS pruned_messages (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            channel_id TEXT PRIMARY KEY,
            checkpoint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_newest_state (
            channel_id TEXT PRIMARY KEY,
            checkpoint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_replies (
            bot_message_id TEXT PRIMARY KEY,
            source_message_id TEXT NOT NULL,
            guild_id TEXT,
            channel_id TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    for column in ("guild_id", "channel_id", "attachment_urls"):
        try:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass
    connection.execute("UPDATE messages SET attachment_urls = '[]' WHERE attachment_urls IS NULL")
    connection.commit()
    return connection


def load_messages(connection: sqlite3.Connection) -> list[StoredMessage]:
    rows = connection.execute(
        "SELECT id, content, guild_id, channel_id, attachment_urls FROM messages ORDER BY rowid"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "guild_id": row["guild_id"],
            "channel_id": row["channel_id"],
            "attachment_urls": json.loads(row["attachment_urls"] or "[]"),
        }
        for row in rows
    ]


def load_pruned_ids(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT id FROM pruned_messages").fetchall()
    return {row["id"] for row in rows}


def load_sync_state(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT channel_id, checkpoint FROM sync_state").fetchall()
    return {row["channel_id"]: row["checkpoint"] for row in rows}


def load_newest_sync_state(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT channel_id, checkpoint FROM sync_newest_state").fetchall()
    return {row["channel_id"]: row["checkpoint"] for row in rows}


def save_sync_checkpoint(connection: sqlite3.Connection, channel_id: str, checkpoint: str) -> None:
    connection.execute(
        "INSERT INTO sync_state(channel_id, checkpoint) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET checkpoint = excluded.checkpoint",
        (channel_id, checkpoint),
    )
    connection.commit()


def save_newest_sync_checkpoint(connection: sqlite3.Connection, channel_id: str, checkpoint: str) -> None:
    connection.execute(
        "INSERT INTO sync_newest_state(channel_id, checkpoint) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET checkpoint = excluded.checkpoint",
        (channel_id, checkpoint),
    )
    connection.commit()


def insert_message(
    connection: sqlite3.Connection,
    message_id: str,
    content: str,
    guild_id: str,
    channel_id: str,
    attachment_urls: list[str],
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO messages "
        "(id, content, guild_id, channel_id, attachment_urls) VALUES (?, ?, ?, ?, ?)",
        (message_id, content, guild_id, channel_id, json.dumps(attachment_urls)),
    )


def insert_bot_reply(
    connection: sqlite3.Connection,
    bot_message_id: str,
    source_message_id: str,
    guild_id: str | None,
    channel_id: str,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO bot_replies "
        "(bot_message_id, source_message_id, guild_id, channel_id) VALUES (?, ?, ?, ?)",
        (bot_message_id, source_message_id, guild_id, channel_id),
    )
    connection.commit()


def load_bot_reply(connection: sqlite3.Connection, bot_message_id: str | None = None) -> sqlite3.Row | None:
    if bot_message_id:
        return connection.execute(
            "SELECT bot_message_id, source_message_id, guild_id, channel_id "
            "FROM bot_replies WHERE bot_message_id = ?",
            (bot_message_id,),
        ).fetchone()
    return connection.execute(
        "SELECT bot_message_id, source_message_id, guild_id, channel_id "
        "FROM bot_replies ORDER BY sent_at DESC, rowid DESC LIMIT 1"
    ).fetchone()


def delete_messages(connection: sqlite3.Connection, message_ids: set[str]) -> None:
    connection.executemany("DELETE FROM messages WHERE id = ?", ((message_id,) for message_id in message_ids))
    connection.executemany(
        "INSERT OR IGNORE INTO pruned_messages(id) VALUES (?)",
        ((message_id,) for message_id in message_ids),
    )
    connection.commit()


async def update_sync_progress(interaction: discord.Interaction, content: str) -> bool:
    try:
        await interaction.edit_original_response(content=content)
    except discord.HTTPException as error:
        logger.info("Could not update sync progress because the interaction expired: %s", error)
        return False
    return True


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


def contains_excluded_word(content: str) -> bool:
    return "jesse" in re.findall(r"[a-z0-9']+", content.casefold())


def image_attachment_urls(message: discord.Message) -> list[str]:
    return [
        attachment.url
        for attachment in message.attachments
        if attachment.content_type and attachment.content_type.startswith("image/")
    ]


def fuzzy_word_score(query_words: set[str], candidate_words: set[str]) -> float:
    if not query_words or not candidate_words:
        return 0.0

    best_matches = []
    for query_word in query_words:
        best_matches.append(
            max(SequenceMatcher(None, query_word, candidate_word).ratio() for candidate_word in candidate_words)
        )
    useful_matches = [score for score in best_matches if score >= 0.75]
    return sum(useful_matches) / len(useful_matches) if useful_matches else 0.0


def weighted_message(query: str) -> StoredMessage | None:
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
        return None

    weights: list[int] = []

    for message in eligible_messages:
        candidate_words = message_words(message["content"])
        matching_words = query_words & candidate_words
        fuzzy_score = fuzzy_word_score(query_words, candidate_words)
        exact_weight = min(len(matching_words), 3) * 3
        fuzzy_weight = round(fuzzy_score * 4)
        weights.append(1 + exact_weight + fuzzy_weight)

    return random.choices(eligible_messages, weights=weights, k=1)[0]


seed_database_if_needed()
database = connect_database()
stored_messages = load_messages(database)
pruned_ids = load_pruned_ids(database)
sync_state = load_sync_state(database)
newest_sync_state = load_newest_sync_state(database)
active_sync_channels: set[int] = set()
sync_stop_requests: set[int] = set()
response_filter = {"min_length": 0, "contains_text": ""}
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook() -> None:
    if GUILD_ID:
        logger.info("Registering slash commands for guild %s.", GUILD_ID)
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        logger.info("Slash commands registered for guild %s.", GUILD_ID)
    else:
        await bot.tree.sync()
        logger.info("Slash commands registered globally.")


@bot.event
async def on_ready() -> None:
    if not rotate_status.is_running():
        rotate_status.start()
    logger.info(
        "Logged in as %s with %s stored messages and %s active sync(s).",
        bot.user,
        len(stored_messages),
        len(active_sync_channels),
    )


@tasks.loop(minutes=2)
async def rotate_status() -> None:
    status_messages = [
        message
        for message in stored_messages
        if len(message["content"]) < 26
        and not re.search(r"https?://\S+|www\.\S+", message["content"], re.IGNORECASE)
    ]
    if not status_messages:
        logger.info("Status rotation skipped: no stored message is longer than 40 characters.")
        return
    status_text = random.choice(status_messages)["content"].replace("\n", " ").strip()
    await bot.change_presence(activity=discord.Game(name=status_text[:128]))
    logger.info("Status rotated using one of %s qualifying stored messages.", len(status_messages))


@bot.tree.command(name="sync", description="Import your messages from this channel")
@discord.app_commands.guild_only()
@discord.app_commands.describe(
    since_date="Scan backward from now to this date, YYYY-MM-DD",
    max_messages="Maximum messages to scan in this run; the next run resumes here",
)
async def sync(
    interaction: discord.Interaction,
    since_date: str | None = None,
    max_messages: discord.app_commands.Range[int, 100, 50000] = 40000,
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
    logger.info(
        "Starting backward sync in channel %s: since_date=%s, max_messages=%s.",
        channel_id,
        since_date or "none",
        max_messages,
    )
    if channel_id in active_sync_channels:
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
    start_note = (
        " Resumed from the saved checkpoint."
        if checkpoint_time is not None
        else " Started a new history scan."
    )

    active_sync_channels.add(channel_id)
    stopped = False
    reached_limit = False
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
            if scanned == 1:
                newest_sync_state[channel_state_key] = str(message.id)
                save_newest_sync_checkpoint(database, channel_state_key, str(message.id))
            message_id = str(message.id)
            attachment_urls = image_attachment_urls(message)
            if (
                message.author.id == OWNER_USER_ID
                and (message.content or attachment_urls)
                and not contains_excluded_word(message.content)
                and message_id not in known_ids
            ):
                stored_messages.append(
                    {
                        "id": message_id,
                        "content": message.content,
                        "guild_id": str(message.guild.id) if message.guild else "",
                        "channel_id": str(message.channel.id),
                        "attachment_urls": attachment_urls,
                    }
                )
                insert_message(
                    database,
                    message_id,
                    message.content,
                    str(message.guild.id) if message.guild else "",
                    str(message.channel.id),
                    attachment_urls,
                )
                known_ids.add(message_id)
                imported += 1
            if scanned % 500 == 0:
                save_sync_checkpoint(database, channel_state_key, sync_state[channel_state_key])
                database.commit()
                logger.info(
                    "Backward sync in channel %s: scanned=%s, imported=%s.",
                    channel_id,
                    scanned,
                    imported,
                )
                await update_sync_progress(
                    interaction,
                    f"Still scanning history... {scanned:,} messages checked, {imported:,} imported.",
                )
            if scanned >= max_messages:
                reached_limit = True
                break
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot read this channel's history. Grant me View Channel and Read Message History, then try again."
        )
        return
    finally:
        active_sync_channels.discard(channel_id)

    save_sync_checkpoint(database, channel_state_key, sync_state.get(channel_state_key, ""))
    database.commit()
    logger.info(
        "Backward sync finished in channel %s: scanned=%s, imported=%s, stopped=%s.",
        channel_id,
        scanned,
        imported,
        stopped,
    )
    resume_note = " This channel will resume from this point next time." if stopped or reached_limit or not since_date else ""
    try:
        await interaction.followup.send(
            f"{'Stopped after' if stopped else 'Checked'} {scanned:,} message(s), imported {imported:,}, "
            f"and now have {len(stored_messages):,} stored messages."
            f"{start_note}"
            f"{resume_note}"
        )
    except discord.HTTPException:
        logger.info(
            "Sync completed after %s messages; the interaction expired before the final response.",
            f"{scanned:,}",
        )


@bot.tree.command(name="sync_new", description="Import newer messages since the last sync")
@discord.app_commands.guild_only()
async def sync_new(interaction: discord.Interaction) -> None:
    """Import messages newer than this channel's newest sync checkpoint."""
    global stored_messages

    await interaction.response.defer()
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("This command must be run in a text channel.")
        return

    channel_id = interaction.channel.id
    channel_state_key = str(channel_id)
    logger.info("Starting newer-message sync in channel %s.", channel_id)
    if channel_id in active_sync_channels:
        await interaction.followup.send("A sync is already running. Stop it before starting another one.")
        return

    newest_id = newest_sync_state.get(channel_state_key)
    after = discord.Object(id=int(newest_id)) if newest_id else None
    known_ids = {message["id"] for message in stored_messages} | pruned_ids
    imported = 0
    scanned = 0
    active_sync_channels.add(channel_id)
    try:
        async for message in interaction.channel.history(
            limit=None,
            after=after,
            before=datetime.now(timezone.utc),
            oldest_first=True,
        ):
            if channel_id in sync_stop_requests:
                sync_stop_requests.remove(channel_id)
                break
            scanned += 1
            newest_sync_state[channel_state_key] = str(message.id)
            message_id = str(message.id)
            attachment_urls = image_attachment_urls(message)
            if (
                message.author.id == OWNER_USER_ID
                and (message.content or attachment_urls)
                and not contains_excluded_word(message.content)
                and message_id not in known_ids
            ):
                stored_messages.append(
                    {
                        "id": message_id,
                        "content": message.content,
                        "guild_id": str(message.guild.id) if message.guild else "",
                        "channel_id": str(message.channel.id),
                        "attachment_urls": attachment_urls,
                    }
                )
                insert_message(
                    database,
                    message_id,
                    message.content,
                    str(message.guild.id) if message.guild else "",
                    str(message.channel.id),
                    attachment_urls,
                )
                known_ids.add(message_id)
                imported += 1
            if scanned % 500 == 0:
                save_newest_sync_checkpoint(database, channel_state_key, newest_sync_state[channel_state_key])
                logger.info(
                    "Newer-message sync in channel %s: scanned=%s, imported=%s.",
                    channel_id,
                    scanned,
                    imported,
                )
                await update_sync_progress(
                    interaction,
                    f"Checking newer messages... {scanned:,} checked, {imported:,} imported.",
                )
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot read this channel's history. Grant me View Channel and Read Message History, then try again."
        )
        return
    finally:
        active_sync_channels.discard(channel_id)

    if newest_sync_state.get(channel_state_key):
        save_newest_sync_checkpoint(database, channel_state_key, newest_sync_state[channel_state_key])
    database.commit()
    logger.info(
        "Newer-message sync finished in channel %s: scanned=%s, imported=%s.",
        channel_id,
        scanned,
        imported,
    )
    await interaction.followup.send(
        f"Checked {scanned:,} newer message(s), imported {imported:,}, and now have "
        f"{len(stored_messages):,} stored messages."
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
    kept_messages: list[StoredMessage] = []
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
    global stored_messages, pruned_ids, sync_state, newest_sync_state

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
    newest_sync_state = {}
    database.execute("DELETE FROM messages")
    database.execute("DELETE FROM pruned_messages")
    database.execute("DELETE FROM sync_state")
    database.execute("DELETE FROM sync_newest_state")
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
    response_filter["min_length"] = 0
    response_filter["contains_text"] = ""
    await interaction.response.send_message("Temporary response filter cleared.", ephemeral=True)


@bot.tree.command(name="stats", description="Show how many messages are stored")
async def stats(interaction: discord.Interaction) -> None:
    """Show how many messages are available for replies."""
    eligible_messages = [
        message
        for message in stored_messages
        if len(message["content"]) >= response_filter["min_length"]
        and (
            not response_filter["contains_text"]
            or response_filter["contains_text"] in message["content"].casefold()
        )
    ]
    total_characters = sum(len(message["content"]) for message in stored_messages)
    bot_reply_count = database.execute("SELECT COUNT(*) FROM bot_replies").fetchone()[0]
    oldest_date = "unknown"
    newest_date = "unknown"
    if stored_messages:
        try:
            oldest_id = min(int(message["id"]) for message in stored_messages)
            newest_id = max(int(message["id"]) for message in stored_messages)
            oldest_date = discord.utils.snowflake_time(oldest_id).strftime("%Y-%m-%d")
            newest_date = discord.utils.snowflake_time(newest_id).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            pass

    await interaction.response.send_message(
        f"Keyword: `{KEYWORD}`\n"
        f"Stored messages: {len(stored_messages):,}\n"
        f"Eligible with current filter: {len(eligible_messages):,}\n"
        f"Total stored characters: {total_characters:,}\n"
        f"Recorded bot replies: {bot_reply_count:,}\n"
        f"Message date range: {oldest_date} to {newest_date}\n"
        f"Matching: exact and fuzzy word overlap"
    )


@bot.tree.command(name="locate", description="Find the original message for a bot reply")
@discord.app_commands.describe(message_id="Optional ID of a message sent by the bot")
async def locate(interaction: discord.Interaction, message_id: str | None = None) -> None:
    """Link to the original stored message used for a bot reply."""
    reply_record = load_bot_reply(database, message_id)
    if reply_record is None:
        target = f"message `{message_id}`" if message_id else "the most recent bot reply"
        await interaction.response.send_message(f"I could not locate {target}.", ephemeral=True)
        return

    guild_id = reply_record["guild_id"]
    if not guild_id:
        await interaction.response.send_message(
            "That reply does not have a guild channel link available.", ephemeral=True
        )
        return

    original_link = (
        f"https://discord.com/channels/{guild_id}/"
        f"{reply_record['channel_id']}/{reply_record['source_message_id']}"
    )
    await interaction.response.send_message(f"Original message: {original_link}")


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.application_command:
        return

    command_name = interaction.data.get("name", "unknown") if interaction.data else "unknown"
    command_options = interaction.data.get("options", []) if interaction.data else []
    logger.info(
        "Command received: /%s from user %s in channel %s with options %s.",
        command_name,
        interaction.user.id,
        interaction.channel.id if interaction.channel else "unknown",
        command_options,
    )


FOLLOW_UP_TIMEOUT_SECONDS = 10.0
last_bot_reply_at: dict[int, float] = {}


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.author == bot.user:
        return

    logger.info(
        "Message received from user %s in channel %s.",
        message.author.id,
        message.channel.id,
    )

    has_keyword = KEYWORD in message.content.casefold()
    now = time.monotonic()
    last_reply_time = last_bot_reply_at.get(message.channel.id)
    is_follow_up = last_reply_time is not None and (now - last_reply_time) <= FOLLOW_UP_TIMEOUT_SECONDS

    # Respond if keyword is present OR if sent within 10 seconds of previous bot response
    if not has_keyword and not is_follow_up:
        return

    if has_keyword and is_follow_up:
        logger.info(
            "Keyword matched in channel %s within follow-up window (single response).",
            message.channel.id,
        )
    elif has_keyword:
        logger.info("Keyword matched in channel %s.", message.channel.id)
    else:
        logger.info(
            "Auto-replying to follow-up message within %.1fs window in channel %s.",
            FOLLOW_UP_TIMEOUT_SECONDS,
            message.channel.id,
        )

    if not stored_messages:
        logger.info("No stored messages available for a reply.")
        return

    source_message = weighted_message(message.content)
    if not source_message:
        logger.info("No stored messages passed the active response filter.")
        return

    reply_parts = [source_message["content"], *source_message.get("attachment_urls", [])]
    sent_message = await message.channel.send("\n".join(part for part in reply_parts if part))
    last_bot_reply_at[message.channel.id] = time.monotonic()

    insert_bot_reply(
        database,
        str(sent_message.id),
        source_message["id"],
        source_message.get("guild_id") or (str(message.guild.id) if message.guild else None),
        source_message.get("channel_id") or str(message.channel.id),
    )
    logger.info("Reply sent as message %s in channel %s.", sent_message.id, message.channel.id)


bot.run(TOKEN, log_handler=None)