import asyncio
import os
import json
import logging
import random
import re
import sqlite3
import shutil
import tempfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import array
import time
from collections import defaultdict
from collections.abc import Callable

try:
    import audioop
    AUDIOOP_AVAILABLE = True
except ImportError:
    AUDIOOP_AVAILABLE = False

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    import davey
    DAVEY_AVAILABLE = True
except ImportError:
    DAVEY_AVAILABLE = False

try:
    from discord.ext import voice_recv
    from discord.ext.voice_recv import opus as voice_recv_opus

    _orig_decode_packet = voice_recv_opus.PacketDecoder._decode_packet
    _decode_error_count = 0

    def _safe_decode_packet(self, packet):
        global _decode_error_count
        try:
            if packet and packet.decrypted_data:
                raw_data = packet.decrypted_data
                if DAVEY_AVAILABLE:
                    vc = getattr(self.sink, "voice_client", None)
                    connection = getattr(vc, "_connection", None)
                    dave_session = getattr(connection, "dave_session", None)
                    user_id = self._cached_id
                    if not user_id and vc and hasattr(vc, "_get_id_from_ssrc"):
                        user_id = vc._get_id_from_ssrc(self.ssrc)

                    if dave_session and user_id:
                        try:
                            raw_data = dave_session.decrypt(user_id, davey.MediaType.audio, raw_data)
                        except Exception:
                            pass

                pcm = self._decoder.decode(raw_data, fec=False)
                return packet, pcm
            return _orig_decode_packet(self, packet)
        except discord.opus.OpusError as err:
            _decode_error_count += 1
            if _decode_error_count <= 5:
                logging.getLogger("recalled").warning("Packet decode OpusError (first %s): %s", _decode_error_count, err)
            silence = b"\x00" * 3840
            return packet, silence
        except Exception as err:
            _decode_error_count += 1
            if _decode_error_count <= 5:
                logging.getLogger("recalled").warning("Packet decode exception: %s", err)
            silence = b"\x00" * 3840
            return packet, silence

    voice_recv_opus.PacketDecoder._decode_packet = _safe_decode_packet
    VOICE_RECV_AVAILABLE = True
except ImportError:
    VOICE_RECV_AVAILABLE = False
except Exception:
    VOICE_RECV_AVAILABLE = True

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False


load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])
KEYWORD = os.getenv("KEYWORD", "").strip().casefold()
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", "recalled.db"))
SEED_DATABASE_FILE = Path(os.getenv("SEED_DATABASE_FILE", "/app/seed/recalled.db"))
LOG_FILE = Path(os.getenv("LOG_FILE", "recalled.log"))
GUILD_ID = os.getenv("GUILD_ID")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
TTS_RATE = os.getenv("TTS_RATE", "+10%").strip()
TTS_TEMP_DIR = Path(tempfile.gettempdir()) / "cordbot_tts"
STT_ENGINE = os.getenv("STT_ENGINE", "google").strip().lower()

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
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus("libopus.so.0")
    except Exception:
        pass


@bot.event
async def setup_hook() -> None:
    if GUILD_ID:
        logger.info("Registering slash commands for guild %s.", GUILD_ID)
        guild = discord.Object(id=int(GUILD_ID))
        try:
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            logger.info("Slash commands registered for guild %s.", GUILD_ID)
        except discord.Forbidden as error:
            logger.error(
                "Could not sync slash commands to guild %s: %s. "
                "Ensure the bot was invited with the 'applications.commands' scope and is in the server.",
                GUILD_ID,
                error,
            )
    else:
        try:
            await bot.tree.sync()
            logger.info("Slash commands registered globally.")
        except discord.Forbidden as error:
            logger.error(
                "Could not sync global slash commands: %s. Ensure the bot has 'applications.commands' scope.",
                error,
            )


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


# ---------------------------------------------------------------------------
# Voice & TTS System (Independent of Text Chat Replies)
# ---------------------------------------------------------------------------

def clean_text_for_speech(text: str) -> str:
    """Clean Discord messages (stripping URLs, emojis, markdown) for speech synthesis."""
    cleaned = re.sub(r"https?://\S+|www\.\S+", "", text)
    cleaned = re.sub(r"```[\s\S]*?```|`[^`]*`", "", cleaned)
    cleaned = re.sub(r"<@[!&]?\d+>|<#\d+>|<a?:\w+:\d+>", "", cleaned)
    cleaned = cleaned.replace("||", "")
    cleaned = re.sub(r"[*_~`#>]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def select_voice_response(query: str = "") -> tuple[StoredMessage, str] | None:
    """
    Select an eligible recalled message suitable for speech.
    Responds at random from eligible stored messages without doing heavy DB/query matching.
    """
    if not stored_messages:
        return None

    min_len = response_filter.get("min_length", 1)
    contains = response_filter.get("contains_text")

    # If query is explicitly provided (e.g. from /vc_test phrase="..."), try finding a matching candidate
    if query:
        query_words = message_words(query)
        if query_words:
            candidates: list[tuple[StoredMessage, str]] = []
            for msg in stored_messages:
                content = msg["content"]
                if len(content) < min_len or (contains and contains not in content.casefold()):
                    continue
                if any(qw in content.casefold() for qw in query_words):
                    cleaned = clean_text_for_speech(content)
                    if len(cleaned) >= 2 and any(c.isalnum() for c in cleaned):
                        candidates.append((msg, cleaned))
                        if len(candidates) >= 50:
                            break
            if candidates:
                return random.choice(candidates)

    # Instant random selection: pick random messages until an eligible one with speakable content is found
    for _ in range(50):
        msg = random.choice(stored_messages)
        content = msg["content"]
        if len(content) < min_len:
            continue
        if contains and contains not in content.casefold():
            continue
        cleaned = clean_text_for_speech(content)
        if len(cleaned) >= 2 and any(c.isalnum() for c in cleaned):
            return msg, cleaned

    return None


async def synthesize_speech(text: str, voice: str = TTS_VOICE, rate: str = TTS_RATE) -> Path:
    """Synthesize text into a temporary MP3 file using edge-tts."""
    if not EDGE_TTS_AVAILABLE:
        raise RuntimeError("The `edge-tts` package is not installed. Run `pip install edge-tts`.")

    TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp3", dir=TTS_TEMP_DIR, delete=False) as temp_file:
        temp_path = Path(temp_file.name)

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(temp_path))
    return temp_path


class VoiceQueueItem:
    def __init__(self, audio_path: Path, text: str) -> None:
        self.audio_path = audio_path
        self.text = text


class GuildVoiceManager:
    """Manages sequential audio playback and queueing for a single guild."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.queue: asyncio.Queue[VoiceQueueItem] = asyncio.Queue()
        self.current_task: asyncio.Task | None = None
        self.is_processing: bool = False

    async def add(self, item: VoiceQueueItem, voice_client: discord.VoiceClient) -> None:
        await self.queue.put(item)
        if not self.is_processing:
            self.current_task = asyncio.create_task(self._process_queue(voice_client))

    async def _process_queue(self, voice_client: discord.VoiceClient) -> None:
        self.is_processing = True
        try:
            while not self.queue.empty():
                if not voice_client.is_connected():
                    break
                item = await self.queue.get()
                play_done = asyncio.Event()

                def after_playback(error: Exception | None) -> None:
                    if error:
                        logger.error("Voice playback error in guild %s: %s", self.guild_id, error)
                    bot.loop.call_soon_threadsafe(play_done.set)

                try:
                    source = discord.FFmpegPCMAudio(
                        str(item.audio_path),
                        before_options="-nostdin -probesize 32768",
                    )
                    voice_client.play(source, after=after_playback)
                    await play_done.wait()
                except Exception as error:
                    logger.error("Error playing audio item in guild %s: %s", self.guild_id, error)
                finally:
                    try:
                        if item.audio_path.exists():
                            item.audio_path.unlink()
                    except OSError:
                        pass
                    self.queue.task_done()
        finally:
            self.is_processing = False

    def clear(self) -> None:
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
                if item.audio_path.exists():
                    item.audio_path.unlink()
            except Exception:
                pass
        self.is_processing = False


voice_managers: dict[int, GuildVoiceManager] = {}


def get_voice_manager(guild_id: int) -> GuildVoiceManager:
    if guild_id not in voice_managers:
        voice_managers[guild_id] = GuildVoiceManager(guild_id)
    return voice_managers[guild_id]


def clear_voice_queue(guild_id: int) -> None:
    if guild_id in voice_managers:
        voice_managers[guild_id].clear()
        del voice_managers[guild_id]


async def play_tts(guild: discord.Guild, text: str) -> bool:
    """Synthesize text and enqueue it for playback in the guild's voice channel."""
    voice_client: discord.VoiceClient | None = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        logger.warning("Cannot play TTS in guild %s: bot is not connected to a voice channel.", guild.id)
        return False

    cleaned = clean_text_for_speech(text)
    if not cleaned:
        logger.warning("TTS skipped: text has no speakable content after cleaning.")
        return False

    try:
        audio_path = await synthesize_speech(cleaned)
    except Exception as error:
        logger.error("Failed to synthesize speech for %r: %s", cleaned, error)
        return False

    manager = get_voice_manager(guild.id)
    await manager.add(VoiceQueueItem(audio_path=audio_path, text=cleaned), voice_client)
    logger.info("Enqueued TTS for playback in guild %s: %r", guild.id, cleaned)
    return True


async def speak_response(guild: discord.Guild, query: str = "") -> str | None:
    """
    Select an appropriate stored message and speak it in the voice channel.
    Designed for use with speech recognition (STT) when the trigger is spoken in VC.
    Does NOT output anything to text channels.
    """
    voice_client: discord.VoiceClient | None = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        logger.warning("speak_response called for guild %s, but bot is not in a voice channel.", guild.id)
        return None

    selection = select_voice_response(query)
    if not selection:
        logger.info("No suitable message found to speak in voice.")
        return None

    msg, speech_text = selection
    logger.info("Selected voice response (source message %s): %r", msg["id"], speech_text)
    success = await play_tts(guild, speech_text)
    if success:
        return speech_text
    return None


# ---------------------------------------------------------------------------
# Speech-to-Text (STT) Recognition System
# ---------------------------------------------------------------------------

if SPEECH_RECOGNITION_AVAILABLE and VOICE_RECV_AVAILABLE:

    class DiscordSRAudioSource(sr.AudioSource):
        """Audio source bridging raw PCM from Discord into speech_recognition."""

        little_endian = True
        SAMPLE_RATE = 48_000
        SAMPLE_WIDTH = 2
        CHANNELS = 2
        CHUNK = 960

        def __init__(self, buffer: array.array) -> None:
            self.buffer = buffer
            self._entered = False

        @property
        def stream(self):
            return self

        def __enter__(self):
            self._entered = True
            return self

        def __exit__(self, *exc) -> None:
            self._entered = False

        def read(self, size: int) -> bytes:
            bytes_needed = size * self.CHANNELS * self.SAMPLE_WIDTH
            silence_mono = b"\x00" * (size * self.SAMPLE_WIDTH)

            if len(self.buffer) < bytes_needed:
                time.sleep(0.005)

            if len(self.buffer) < bytes_needed:
                # Return silence frames when no voice data is pending to prevent EOF in SpeechRecognition
                return silence_mono

            audiochunk = self.buffer[:bytes_needed].tobytes()
            del self.buffer[: min(bytes_needed, len(audiochunk))]

            if AUDIOOP_AVAILABLE and len(audiochunk) >= self.CHANNELS * self.SAMPLE_WIDTH:
                try:
                    return audioop.tomono(audiochunk, self.SAMPLE_WIDTH, 0.5, 0.5)
                except Exception:
                    pass
            # Fallback to mono: take left 16-bit channel safely
            try:
                return array.array("h", audiochunk)[0::2].tobytes()
            except Exception:
                return audiochunk[0::2]

        def close(self) -> None:
            del self.buffer[:]

    class BotSpeechRecognitionSink(voice_recv.AudioSink):
        """Audio sink receiving PCM streams per user and transcribing with SpeechRecognition."""

        def __init__(
            self,
            *,
            text_cb: Callable[[discord.Member | int, str], None],
            default_recognizer: str = "google",
            phrase_time_limit: int = 8,
        ) -> None:
            super().__init__(None)
            self.text_cb = text_cb
            self.default_recognizer = default_recognizer
            self.phrase_time_limit = phrase_time_limit
            self._stream_data: dict[int, dict] = defaultdict(self._new_stream_data)

        def _new_stream_data(self) -> dict:
            rec = sr.Recognizer()
            rec.energy_threshold = 200
            rec.dynamic_energy_threshold = False
            rec.pause_threshold = 0.45
            rec.phrase_threshold = 0.2
            rec.non_speaking_duration = 0.25
            return {
                "stopper": None,
                "recognizer": rec,
                "buffer": array.array("B"),
            }

        def wants_opus(self) -> bool:
            return False

        def write(self, user: discord.Member | None, data: voice_recv.VoiceData) -> None:
            if isinstance(data.packet, voice_recv.SilencePacket):
                return

            voice_client = self.voice_client
            ssrc = data.packet.ssrc if data.packet else 0

            # If user is not provided by cache, try resolving from voice client SSRC or voice channel
            if user is None and voice_client:
                user_id = voice_client._get_id_from_ssrc(ssrc)
                if user_id:
                    user = voice_client.guild.get_member(user_id)
                    if not user and voice_client.channel:
                        for member in voice_client.channel.members:
                            if member.id == user_id:
                                user = member
                                break

            # If still None, look for any non-bot member in the current voice channel
            if user is None and voice_client and voice_client.channel:
                human_members = [m for m in voice_client.channel.members if not m.bot]
                if len(human_members) == 1:
                    user = human_members[0]

            # Ignore audio from bot accounts
            if user and user.bot:
                return

            stream_key = user.id if user else ssrc
            sdata = self._stream_data[stream_key]
            if data.pcm:
                sdata["buffer"].extend(data.pcm)

            if not sdata["stopper"]:
                speaker = user or stream_key
                logger.info("Initializing speech recognizer listener for %s (stream key %s)", speaker, stream_key)
                sdata["stopper"] = sdata["recognizer"].listen_in_background(
                    DiscordSRAudioSource(sdata["buffer"]),
                    self._background_listener(speaker),
                    phrase_time_limit=self.phrase_time_limit,
                )

        def _background_listener(self, user_or_key: discord.Member | int):
            speaker_name = user_or_key.display_name if isinstance(user_or_key, discord.Member) else f"User {user_or_key}"

            def callback(recognizer: sr.Recognizer, audio: sr.AudioData) -> None:
                text: str | None = None
                try:
                    recognize_func = getattr(
                        recognizer,
                        f"recognize_{self.default_recognizer}",
                        recognizer.recognize_google,
                    )
                    text = recognize_func(audio)
                except sr.UnknownValueError:
                    # Silence or unrecognizable sound
                    return
                except Exception as error:
                    logger.warning("Speech recognition API error for %s: %s", speaker_name, error)
                    return

                if text:
                    logger.info("Speech detected from %s: %r", speaker_name, text)
                    try:
                        self.text_cb(user_or_key, text)
                    except Exception as error:
                        logger.error("Error in speech recognition text callback: %s", error)

            return callback

        def cleanup(self) -> None:
            for user_id in tuple(self._stream_data.keys()):
                self._drop(user_id)

        def _drop(self, user_id: int) -> None:
            data = self._stream_data.pop(user_id, None)
            if not data:
                return
            stopper = data.get("stopper")
            if stopper:
                try:
                    stopper()
                except Exception:
                    pass
            buf = data.get("buffer")
            if buf:
                del buf[:]


def keyword_matches_vc(text: str) -> bool:
    """Check if the trigger keyword (or accepted variants like 'jessie') was spoken in voice chat."""
    if not text:
        return False

    text_lower = text.casefold()

    # 1. Direct substring check for KEYWORD and explicitly 'jessie'
    if KEYWORD in text_lower or "jessie" in text_lower:
        return True

    # 2. Known phonetic/homophone/spelling variants for common keywords
    known_variants: dict[str, set[str]] = {
        "jesse": {"jessie", "jessy", "jessee", "jess", "jessi"},
    }
    accepted_words = set(known_variants.get(KEYWORD, set()))
    accepted_words.add(KEYWORD)
    accepted_words.add("jessie")

    # 3. Word token matching
    words = [w.strip("'\".,!?") for w in re.findall(r"[a-z0-9']+", text_lower)]
    for word in words:
        if word in accepted_words:
            return True

    # 4. Fuzzy word similarity (ratio >= 0.82)
    # Threshold 0.82 matches 'jessie' (0.91), 'jess' (0.89) while avoiding false positives like 'jesus' (0.80)
    for word in words:
        if len(word) >= 3:
            for target in accepted_words:
                if SequenceMatcher(None, target, word).ratio() >= 0.82:
                    return True

    return False


def setup_stt_listener(voice_client: discord.VoiceClient) -> bool:
    """Attach the speech recognition sink to a VoiceRecvClient."""
    if not VOICE_RECV_AVAILABLE or not SPEECH_RECOGNITION_AVAILABLE:
        logger.warning("Voice receive or SpeechRecognition is not installed. Speech detection disabled.")
        return False

    if not isinstance(voice_client, voice_recv.VoiceRecvClient):
        logger.warning("Voice client is not a VoiceRecvClient; cannot listen for speech.")
        return False

    if voice_client.is_listening():
        return True

    def on_speech_recognized(user_or_key: discord.Member | int, text: str) -> None:
        if not text or not text.strip():
            return
        if isinstance(user_or_key, discord.Member) and user_or_key.bot:
            return
        if voice_client.is_playing():
            logger.debug("Speech ignored while bot is currently playing audio: %r", text)
            return

        speaker_name = user_or_key.display_name if isinstance(user_or_key, discord.Member) else f"User {user_or_key}"
        logger.info("Transcribed from %s: %r", speaker_name, text)

        # Check if the trigger keyword ("jesse", "jessie", or variants) was spoken
        if keyword_matches_vc(text):
            logger.info("Keyword matched in speech from %s (%r)! Triggering random voice response.", speaker_name, text)
            asyncio.run_coroutine_threadsafe(
                speak_response(voice_client.guild),
                bot.loop,
            )
        else:
            logger.info("Heard speech from %s: %r (keyword %r / 'jessie' not matched)", speaker_name, text, KEYWORD)

    sink = BotSpeechRecognitionSink(
        text_cb=on_speech_recognized,
        default_recognizer=STT_ENGINE,
    )
    voice_client.listen(sink)
    logger.info("Started speech recognition listener in guild %s using engine '%s'.", voice_client.guild.id, STT_ENGINE)
    return True


@bot.tree.command(name="vc", description="Make the bot join your current voice channel and listen for speech")
@discord.app_commands.guild_only()
async def vc_join(interaction: discord.Interaction) -> None:
    """Connect the bot to the voice channel the user is currently in."""
    if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "You need to be connected to a voice channel to use this command.",
            ephemeral=True,
        )
        return

    channel = interaction.user.voice.channel
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    permissions = channel.permissions_for(guild.me)
    if not permissions.connect or not permissions.speak:
        await interaction.response.send_message(
            f"I do not have permission to connect and speak in {channel.mention}.",
            ephemeral=True,
        )
        return

    connect_cls = voice_recv.VoiceRecvClient if VOICE_RECV_AVAILABLE else discord.VoiceClient
    voice_client: discord.VoiceClient | None = guild.voice_client

    if voice_client:
        if voice_client.channel.id == channel.id:
            await interaction.response.send_message(
                f"I'm already in {channel.mention}!",
                ephemeral=True,
            )
            return
        await voice_client.move_to(channel)
        await interaction.response.send_message(f"Moved to {channel.mention}.")
        logger.info("Moved to voice channel %s (%s) in guild %s.", channel.name, channel.id, guild.id)
    else:
        voice_client = await channel.connect(cls=connect_cls)
        await interaction.response.send_message(f"Joined {channel.mention}.")
        logger.info("Joined voice channel %s (%s) in guild %s.", channel.name, channel.id, guild.id)

    if VOICE_RECV_AVAILABLE and SPEECH_RECOGNITION_AVAILABLE:
        setup_stt_listener(voice_client)


@bot.tree.command(name="leave", description="Disconnect the bot from the voice channel")
@discord.app_commands.guild_only()
async def vc_leave(interaction: discord.Interaction) -> None:
    """Disconnect the bot from the current voice channel."""
    guild = interaction.guild
    if guild is None or not guild.voice_client:
        await interaction.response.send_message("I am not connected to any voice channel.", ephemeral=True)
        return

    voice_client = guild.voice_client
    if VOICE_RECV_AVAILABLE and isinstance(voice_client, voice_recv.VoiceRecvClient):
        if voice_client.is_listening():
            voice_client.stop_listening()

    clear_voice_queue(guild.id)
    await voice_client.disconnect()
    await interaction.response.send_message("Disconnected from the voice channel.")
    logger.info("Disconnected from voice in guild %s.", guild.id)


@bot.tree.command(name="stt", description="Check or toggle voice speech detection")
@discord.app_commands.describe(enabled="Enable or disable listening for speech in voice")
@discord.app_commands.guild_only()
async def stt_command(interaction: discord.Interaction, enabled: bool | None = None) -> None:
    """Check or toggle speech recognition in the connected voice channel."""
    guild = interaction.guild
    if guild is None or not guild.voice_client:
        await interaction.response.send_message("The bot is not currently connected to a voice channel.", ephemeral=True)
        return

    voice_client = guild.voice_client
    if not VOICE_RECV_AVAILABLE or not SPEECH_RECOGNITION_AVAILABLE:
        await interaction.response.send_message(
            "Speech detection is unavailable because `discord-ext-voice-recv` or `SpeechRecognition` is not installed.\n"
            "Run `pip install discord-ext-voice-recv SpeechRecognition` to enable it.",
            ephemeral=True,
        )
        return

    if not isinstance(voice_client, voice_recv.VoiceRecvClient):
        await interaction.response.send_message("Voice client was not connected with voice receiving support.", ephemeral=True)
        return

    if enabled is None:
        status = "Active and listening" if voice_client.is_listening() else "Stopped / Idle"
        await interaction.response.send_message(
            f"Speech detection status: **{status}**\n"
            f"Trigger keyword: `{KEYWORD}`\n"
            f"Engine: `{STT_ENGINE}`",
            ephemeral=True,
        )
        return

    if enabled:
        if voice_client.is_listening():
            await interaction.response.send_message("Speech detection is already active and listening.", ephemeral=True)
        else:
            setup_stt_listener(voice_client)
            await interaction.response.send_message("Speech detection enabled! Listening for speech in VC.", ephemeral=True)
    else:
        if voice_client.is_listening():
            voice_client.stop_listening()
            await interaction.response.send_message("Speech detection paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Speech detection is already paused.", ephemeral=True)


@bot.tree.command(name="vc_test", description="Test voice response in the connected voice channel")
@discord.app_commands.describe(
    phrase="Optional query/topic to test matching, or leave empty for a random response",
    custom_text="Optional custom text to speak directly instead of picking a recalled message",
)
@discord.app_commands.guild_only()
async def vc_test(
    interaction: discord.Interaction,
    phrase: str | None = None,
    custom_text: str | None = None,
) -> None:
    """Test the TTS and voice response system."""
    guild = interaction.guild
    if guild is None or not guild.voice_client or not guild.voice_client.is_connected():
        await interaction.response.send_message(
            "The bot is not currently in a voice channel. Use `/vc` first!",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    if custom_text:
        success = await play_tts(guild, custom_text)
        if success:
            await interaction.followup.send(f"Speaking custom text: \"{custom_text}\"", ephemeral=True)
        else:
            await interaction.followup.send("Failed to generate or play speech.", ephemeral=True)
        return

    spoken = await speak_response(guild, phrase or "")
    if spoken:
        await interaction.followup.send(f"Spoke response for \"{phrase or 'random'}\": \"{spoken}\"", ephemeral=True)
    else:
        await interaction.followup.send("Could not find a suitable stored response to speak.", ephemeral=True)


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    if member == bot.user and before.channel and not after.channel:
        clear_voice_queue(member.guild.id)
        logger.info("Bot was disconnected from voice channel in guild %s; cleared voice queue.", member.guild.id)


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


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    logger.info(
        "Message received from user %s in channel %s.",
        message.author.id,
        message.channel.id,
    )
    if KEYWORD not in message.content.casefold():
        return

    logger.info("Keyword matched in channel %s.", message.channel.id)
    if not stored_messages:
        logger.info("No stored messages available for a reply.")
        return

    source_message = weighted_message(message.content)
    if not source_message:
        logger.info("No stored messages passed the active response filter.")
        return

    reply_parts = [source_message["content"], *source_message.get("attachment_urls", [])]
    sent_message = await message.channel.send("\n".join(part for part in reply_parts if part))
    insert_bot_reply(
        database,
        str(sent_message.id),
        source_message["id"],
        source_message.get("guild_id") or (str(message.guild.id) if message.guild else None),
        source_message.get("channel_id") or str(message.channel.id),
    )
    logger.info("Reply sent as message %s in channel %s.", sent_message.id, message.channel.id)

bot.run(TOKEN, log_handler=None)