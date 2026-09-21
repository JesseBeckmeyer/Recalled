# Recalled

This bot imports your own messages from a Discord channel and replies with one at random when a configured keyword is mentioned.

It stores message content locally. Do not commit collected messages or your Discord token.

## Setup

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the **Message Content Intent** under the bot's Privileged Gateway Intents.
3. Invite the bot with the `bot` and `applications.commands` scopes, plus permission to view channel history and send messages.
4. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

5. Copy `.env.example` to `.env` and set:
   - `DISCORD_TOKEN`: the bot token. Keep it private.
   - `OWNER_USER_ID`: your Discord user ID. Enable Developer Mode in Discord, then copy your user ID.
   - `KEYWORD`: the case-insensitive keyword that triggers a response.
   - `GUILD_ID` (optional): your server ID. Setting this registers slash commands immediately in that server; omit it to register globally.
6. Start the bot:

   ```bash
   .venv/bin/python bot.py
   ```

## Commands

- `/sync` imports your messages from the channel where the command is run. Run it once per channel you want to include.
- `/stats` reports how many messages are stored.
- `/prune` previews or removes messages using text, length, link, command, and duplicate filters. It is dry-run by default.

Messages are stored locally in `messages.json`, with excluded IDs in `pruned_ids.json`; both are ignored by Git. The bot filters history by `OWNER_USER_ID` and does not save other members' messages.

## Docker

Install Docker with Compose v2, then run:

```bash
docker compose up --build -d
docker compose logs -f discord-bot
```

Stop it with `docker compose down`. The named volume `messages-data` preserves collected messages across container rebuilds. Do not use `docker compose down --volumes` unless you intend to delete that data.

## Publishing safely

Before pushing, confirm `.env`, `messages.json`, and `pruned_ids.json` are not tracked:

```bash
git status --short
git ls-files .env messages.json pruned_ids.json
```

The second command should produce no output. If a token was ever committed, revoke it in the Discord Developer Portal and create a new one; adding it to `.gitignore` does not remove it from Git history.