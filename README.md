# Wormhole Governance Discord Bot

A Discord bot that monitors Wormhole governance proposals and sends alerts to a designated channel.

## Features
- 🔍 Monitors Wormhole governance proposals in real-time
- 📢 Sends automatic alerts for new proposals
- 📊 Shows proposal status, voting stats, and time remaining
- 🔗 Direct links to view proposals on Tally
- 👤 Tally API integration for proposal details
- 💾 Database tracking to prevent re-announcing proposals on restart
- 📝 Discord embeds that update themselves as the proposal progresses

## Requirements
- Python 3.13 or higher
- Discord Bot Token
- Discord Server with appropriate permissions
- Tally API Key

## Installation

### 1. Discord Bot Setup
1. Create a Discord application at https://discord.com/developers/applications
2. Navigate to the "Bot" section and create a bot
3. Copy the bot token (you'll need this for the `.env` file)
4. Under "Privileged Gateway Intents", enable:
   - Message Content Intent (if you want to use the bot's commands)
5. Generate an invite link with these permissions:
   - Send Messages
   - Embed Links
   - Read Message History
6. Invite the bot to your server using the generated link
7. Create a channel for governance alerts and copy its ID

### 2. Project Setup
1. Clone the repository:
```bash
git clone <repository-url>
cd w-governance-alert
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

5. Configure your `.env` file:
```
# Discord Bot Configuration (Required)
DISCORD_TOKEN=your_discord_bot_token_here
DISCORD_GUILD_ID=your_discord_guild_id_here
PROPOSALS_CHANNEL_ID=your_channel_id_here

# Tally API Configuration (Required)
# Get your API key from https://www.tally.xyz/user/settings
TALLY_API_KEY=your_tally_api_key_here

# Bot Configuration (Optional)
# How often to check for new proposals (in minutes, default: 5)
SYNC_INTERVAL_MINUTES=5
```

### 3. Run the Bot
```bash
python tally_bot.py
```

The bot will:
- Create an `announced_proposals.db` file to track proposals (if `LIVE_MODE=false`)
- Start checking for new governance proposals every `SYNC_INTERVAL_MINUTES`
- Post alerts to your configured channel when proposals become active
- Update existing alerts as proposal statuses change

## Discord Commands
- `/clear_db` - Clear the announced proposals database (User must have Administrator permissions)

## Database
The bot uses a SQLite database (`announced_proposals.db`) to track which proposals have been announced. This database is automatically created on first run.

## Troubleshooting
- **Bot not responding**: Check that the bot token is correct and the bot is invited to your server
- **No alerts**: Verify the channel ID is correct and the bot has permissions to send messages
- **Tally details missing**: Ensure your Tally API key is valid and properly set in `.env`
- **Rate limit errors**: The bot implements automatic rate limiting, but excessive requests may still cause issues

## Contributing
Feel free to submit issues and enhancement requests!
I'm not a professional programmer, so please be patient with me. 
This was made with Claude 4 Opus + Sonnet.