import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import requests
from datetime import datetime, timezone
import asyncio
import re
import sqlite3
import threading
import time

# Load environment variables
load_dotenv()

# Configure SQLite to handle datetime properly
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("timestamp", lambda b: datetime.fromisoformat(b.decode()))

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', 0))
PROPOSALS_CHANNEL_ID = int(os.getenv('PROPOSALS_CHANNEL_ID', 0))
SYNC_INTERVAL_MINUTES = int(os.getenv('SYNC_INTERVAL_MINUTES', 5))  # Default to 5 minutes

# Tally API configuration
TALLY_API_URL = "https://api.tally.xyz/query"
TALLY_API_KEY = os.getenv("TALLY_API_KEY")
TALLY_ORG_ID = os.getenv('TALLY_ORG_ID', '2323517483434116775')  # Wormhole organization ID on Tally

intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
bot = commands.Bot(command_prefix='/', intents=intents)

# Store announced proposal IDs to avoid duplicates
announced_proposals = set()

# Database setup
DATABASE_FILE = 'announced_proposals.db'

# Define final proposal statuses that halt synchronization
FINAL_PROPOSAL_STATUSES = [
    'CANCELED',
    'DEFEATED',
    'EXECUTED',
    'EXPIRED',
    'SUCCEEDED',
    'CROSSCHAINEXECUTED'
]

def init_database():
    """Initialize the database for tracking announced proposals"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS announced_proposals (
            id TEXT PRIMARY KEY,
            announced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            title TEXT,
            status TEXT,
            tally_id TEXT,
            discord_message_id TEXT,
            last_sync_at TIMESTAMP
        )
    ''')
    
    # Add new columns if they don't exist (for migration)
    cursor.execute("PRAGMA table_info(announced_proposals)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'discord_message_id' not in columns:
        cursor.execute('ALTER TABLE announced_proposals ADD COLUMN discord_message_id TEXT')
    
    if 'last_sync_at' not in columns:
        cursor.execute('ALTER TABLE announced_proposals ADD COLUMN last_sync_at TIMESTAMP')
    
    conn.commit()
    conn.close()

def load_announced_proposals():
    """Load previously announced proposals from database"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('SELECT id FROM announced_proposals')
    proposals = {row[0] for row in cursor.fetchall()}
    
    conn.close()
    print(f"Loaded {len(proposals)} previously announced proposals from database")
    return proposals

def save_announced_proposal(proposal):
    """Save an announced proposal to the database"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO announced_proposals (id, title, status, tally_id, discord_message_id, last_sync_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (proposal.id, proposal.title, proposal.status, proposal.id, 
          getattr(proposal, 'discord_message_id', None), 
          datetime.now(timezone.utc) if hasattr(proposal, 'discord_message_id') and proposal.discord_message_id else None))
    
    conn.commit()
    conn.close()

def load_proposals_for_sync():
    """Load proposals that need syncing (ACTIVE status with discord_message_id)"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, discord_message_id, status 
        FROM announced_proposals 
        WHERE discord_message_id IS NOT NULL
    ''')
    
    proposals_to_sync = {}
    for row in cursor.fetchall():
        proposals_to_sync[row[0]] = {
            'discord_message_id': row[1],
            'last_status': row[2]
        }
    
    conn.close()
    return proposals_to_sync

def update_proposal_sync_status(proposal_id, status):
    """Update the sync timestamp and status for a proposal"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE announced_proposals 
        SET last_sync_at = ?, status = ?
        WHERE id = ?
    ''', (datetime.now(timezone.utc), status, proposal_id))
    
    conn.commit()
    conn.close()

async def update_proposal_embed(channel, message_id, proposal):
    """Update an existing Discord embed with new proposal data"""
    try:
        message = await channel.fetch_message(int(message_id))
        new_embed = proposal.create_embed()
        await message.edit(embed=new_embed)
        return True
    except discord.NotFound:
        print(f"Message {message_id} not found for proposal {proposal.id}")
        return False
    except Exception as e:
        print(f"Error updating message {message_id}: {e}")
        return False

class TallyRateLimiter:
    """Rate limiter for Tally API (1.1 seconds between requests)"""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_request = 0
    
    def wait_if_needed(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request
            if elapsed < 1.1:
                time.sleep(1.1 - elapsed)
            self.last_request = time.time()

# Global rate limiter instance
tally_rate_limiter = TallyRateLimiter()

def fetch_wormhole_proposals_from_tally(limit=20, status_filter=None):
    """Fetch Wormhole proposals directly from Tally API"""
    tally_rate_limiter.wait_if_needed()
    
    query = """
    query GovernanceProposals($input: ProposalsInput!) {
      proposals(input: $input) {
        nodes {
          ... on Proposal {
            id
            onchainId
            status
            createdAt
            metadata {
              title
              description
            }
            proposer {
              address
              name
              ens
            }
            governor {
              id
              name
              slug
            }
            start {
              ... on Block {
                timestamp
              }
              ... on BlocklessTimestamp {
                timestamp
              }
            }
            end {
              ... on Block {
                timestamp
              }
              ... on BlocklessTimestamp {
                timestamp
              }
            }
            block {
              timestamp
            }
            voteStats {
              votesCount
              percent
              type
              votersCount
            }
          }
        }
        pageInfo {
          firstCursor
          lastCursor
          count
        }
      }
    }
    """
    
    # Build filters
    filters = {"organizationId": TALLY_ORG_ID}
    if status_filter:
        filters["status"] = status_filter
    
    variables = {
        "input": {
            "filters": filters,
            "sort": {
                "sortBy": "id",
                "isDescending": True
            },
            "page": {
                "limit": limit
            }
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "Api-Key": TALLY_API_KEY
    }
    
    try:
        response = requests.post(
            TALLY_API_URL,
            json={"query": query, "variables": variables},
            headers=headers
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'data' in result and 'proposals' in result['data']:
                return result['data']['proposals']['nodes']
    except Exception as e:
        print(f"Error fetching proposals from Tally: {e}")
    
    return []

class TallyProposal:
    def __init__(self, proposal_data):
        self.id = proposal_data.get('id')
        self.onchain_id = proposal_data.get('onchainId')
        self.status = proposal_data.get('status', 'UNKNOWN')
        self.created_at = proposal_data.get('createdAt')
        
        # Metadata
        metadata = proposal_data.get('metadata', {})
        base_title = metadata.get('title', 'N/A')
        self.description = metadata.get('description', 'N/A')
        
        # Extract full title including WIP prefix if it exists in the description
        # Look for patterns like "WIP-1: Title" at the start of description
        title_with_prefix_match = re.match(r'^([A-Z0-9\-\s]+:\s*' + re.escape(base_title) + r')', self.description, re.IGNORECASE)
        if title_with_prefix_match:
            self.title = title_with_prefix_match.group(1).strip()
        else:
            self.title = base_title
        
        # Proposer info
        proposer = proposal_data.get('proposer', {})
        self.proposer_address = proposer.get('address')
        self.proposer_name = proposer.get('name') or proposer.get('ens') or self._mask_address(self.proposer_address)
        
        # Governor info
        governor = proposal_data.get('governor', {})
        self.governor_name = governor.get('name', 'Wormhole')
        # Force governor slug to 'wormhole' since Tally API returns 'wormhole-governor-1' 
        # but the actual URL uses just 'wormhole'
        self.governor_slug = 'wormhole'
        
        # Voting end time
        end_block = proposal_data.get('end', {})
        self.end_timestamp = end_block.get('timestamp')
        
        # Creation time (from block)
        block = proposal_data.get('block', {})
        self.block_timestamp = block.get('timestamp')
        
        # Vote statistics
        self.vote_stats = proposal_data.get('voteStats', [])
        
        # Build Tally URL
        self.tally_url = f"https://www.tally.xyz/gov/{self.governor_slug}/proposal/{self.id}"
        
        # Build proposer profile URL
        if self.proposer_address:
            self.proposer_url = f"https://www.tally.xyz/profile/{self.proposer_address}?governanceId={self.governor_slug}"

        # Initialize discord_message_id and last_sync_at
        self.discord_message_id = None
        self.last_sync_at = None

    def _mask_address(self, address):
        """Mask Ethereum address to format 0x1234...5678"""
        if not address or len(address) < 10:
            return address
        return f"{address[:6]}...{address[-4:]}"

    @property
    def end_date(self):
        if self.end_timestamp:
            # Handle both millisecond timestamps and ISO date strings
            if isinstance(self.end_timestamp, str):
                return datetime.fromisoformat(self.end_timestamp.replace('Z', '+00:00'))
            else:
                return datetime.fromtimestamp(int(self.end_timestamp) / 1000, tz=timezone.utc)
        return None

    @property
    def creation_date(self):
        if self.block_timestamp:
            # Handle both millisecond timestamps and ISO date strings
            if isinstance(self.block_timestamp, str):
                return datetime.fromisoformat(self.block_timestamp.replace('Z', '+00:00'))
            else:
                return datetime.fromtimestamp(int(self.block_timestamp) / 1000, tz=timezone.utc)
        return None

    @property
    def is_active(self):
        # Only consider proposals in ACTIVE status for announcement
        return self.status.upper() in ['ACTIVE', 'QUEUED']
    
    @property
    def is_syncable(self):
        # Proposals should be synced while NOT in a final status
        # Final statuses indicate the proposal's voting and execution is finished
        return self.status.upper() not in FINAL_PROPOSAL_STATUSES

    def get_vote_percentages(self):
        """Calculate vote percentages from vote stats"""
        for_votes = 0
        against_votes = 0
        abstain_votes = 0
        total_votes = 0
        
        for stat in self.vote_stats:
            vote_type = stat.get('type', '').upper()
            votes = float(stat.get('votesCount', 0))
            
            if vote_type == 'FOR':
                for_votes = votes
            elif vote_type == 'AGAINST':
                against_votes = votes
            elif vote_type == 'ABSTAIN':
                abstain_votes = votes
            
            total_votes += votes
        
        if total_votes == 0:
            return 0, 0, 0
        
        for_percent = (for_votes / total_votes) * 100
        against_percent = (against_votes / total_votes) * 100
        abstain_percent = (abstain_votes / total_votes) * 100
        
        return for_percent, against_percent, abstain_percent

    def extract_abstract(self):
        """Extract abstract from description or return truncated description"""
        # Start with the raw description
        clean_text = self.description.strip()
        
        # Remove the full title from the beginning of the description
        # Since self.title now includes the WIP prefix, we can do a simple removal
        if clean_text.startswith(self.title):
            clean_text = clean_text[len(self.title):].strip()
        
        # Also check case-insensitive
        if clean_text.lower().startswith(self.title.lower()):
            clean_text = clean_text[len(self.title):].strip()
        
        # Split into lines and filter out any line containing the title
        lines = clean_text.split('\n')
        filtered_lines = []
        title_lower = self.title.lower()
        
        for line in lines:
            # Skip any line that contains the title (case-insensitive)
            if title_lower in line.lower():
                continue
            filtered_lines.append(line)
        
        # Rejoin the filtered lines
        clean_text = '\n'.join(filtered_lines).strip()
        
        # Remove markdown headers that contain keywords like Abstract, TL;DR, etc.
        header_keywords = r'(?:Abstract|Summary|Overview|TL;?DR|Introduction|Description|Proposal|Background|Context|Rationale|Motivation|Purpose)'
        
        # Remove markdown headers with these keywords (e.g., "## Abstract", "# TL;DR")
        clean_text = re.sub(r'^#{1,6}\s*' + header_keywords + r'\s*:?\s*$', '', clean_text, flags=re.IGNORECASE | re.MULTILINE)
        
        # Remove these keywords when they appear at the start of a line followed by a colon
        clean_text = re.sub(r'^' + header_keywords + r'\s*:\s*', '', clean_text, flags=re.IGNORECASE | re.MULTILINE)
        
        # Now remove markdown formatting but preserve the text
        clean_text = re.sub(r'#{1,6}\s+', '', clean_text)  # Remove header markers
        clean_text = re.sub(r'\*{1,2}([^\*]+)\*{1,2}', r'\1', clean_text)  # Remove bold/italic
        clean_text = re.sub(r'<[^>]+>', '', clean_text)  # Remove HTML tags
        clean_text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean_text)  # Convert links to text
        
        # Clean up whitespace
        clean_text = re.sub(r'\n\s*\n', '\n', clean_text)  # Remove empty lines
        clean_text = re.sub(r'\n+', ' ', clean_text)  # Replace newlines with spaces
        clean_text = re.sub(r'\s+', ' ', clean_text)  # Replace multiple spaces with single space
        clean_text = clean_text.strip()
        
        # Ensure exactly 280 characters with "..." at the end
        # 277 characters for content + 3 for "..."
        if len(clean_text) > 277:
            # Find a good break point (space) before character 277
            truncated = clean_text[:277]
            last_space = truncated.rfind(' ')
            if last_space > 200:  # Only use space break if it's not too far back
                truncated = truncated[:last_space]
            return truncated + "..."
        else:
            # If text is shorter than 277 chars, still add "..."
            return clean_text + "..."

    def create_embed(self):
        """Create a Discord embed for the proposal"""
        # Determine embed color based on status
        # Using custom purple color
        color = discord.Color(0xB291DE)  # Purple hex color

        # Create the embed without description initially
        embed = discord.Embed(
            title=self.title,
            url=self.tally_url,
            color=color
        )

        # Add author field with masked link
        if self.proposer_address:
            author_text = f"[{self.proposer_name}]({self.proposer_url})"
        else:
            author_text = self.proposer_name
        embed.add_field(name="Author", value=author_text, inline=True)

        # Add voting end date
        if self.end_date:
            end_str = self.end_date.strftime("%m/%d/%Y %H:%M UTC")
        else:
            end_str = "N/A"
        embed.add_field(name="Voting Ends", value=end_str, inline=True)

        # Add status
        status_display = self.status.title() if self.status else "Unknown"
        embed.add_field(name="Status", value=status_display, inline=True)

        # Add description as a field
        description_text = self.extract_abstract()
        embed.add_field(name="Description", value=description_text, inline=False)

        # Add voting progress
        for_percent, against_percent, abstain_percent = self.get_vote_percentages()
        
        # Create visual bars with 10 squares each
        def create_bar(percentage, filled_emoji):
            filled_count = round(percentage / 10)  # Each square represents 10%
            empty_count = 10 - filled_count
            return filled_emoji * filled_count + "⬜" * empty_count
        
        voting_text = f"{create_bar(for_percent, '🟩')}  –  {for_percent:.1f}% FOR\n"
        voting_text += f"{create_bar(against_percent, '🟥')}  –  {against_percent:.1f}% AGAINST\n"
        voting_text += f"{create_bar(abstain_percent, '🟨')}  –  {abstain_percent:.1f}% ABSTAIN"
        
        embed.add_field(name="Voting", value=voting_text, inline=False)

        # Set footer with creation date
        if self.creation_date:
            created_str = self.creation_date.strftime("%m/%d/%Y %H:%M UTC")
            embed.set_footer(text=f"Created: {created_str}")
        
        return embed

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    
    # Initialize database
    init_database()
    
    # Load previously announced proposals
    global announced_proposals
    announced_proposals = load_announced_proposals()
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # Update the task interval before starting
    check_new_proposals.change_interval(minutes=SYNC_INTERVAL_MINUTES)
    print(f"Proposal sync interval set to {SYNC_INTERVAL_MINUTES} minutes")
    
    # Start checking for new proposals
    if not check_new_proposals.is_running():
        check_new_proposals.start()

async def fetch_wormhole_proposals():
    """Wrapper to fetch proposals asynchronously"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_wormhole_proposals_from_tally)

@tasks.loop(minutes=5)  # Initial value, will be updated in on_ready
async def check_new_proposals():
    """Check for new proposals and sync existing ones periodically"""
    channel = bot.get_channel(PROPOSALS_CHANNEL_ID)
    if not channel:
        print(f"Channel with ID {PROPOSALS_CHANNEL_ID} not found")
        return

    # Fetch current proposals from Tally
    proposals = await fetch_wormhole_proposals()
    print(f"Found {len(proposals)} total proposals from Tally")

    # Load proposals that need syncing
    proposals_to_sync = load_proposals_for_sync()
    
    # Process all proposals
    new_active_proposals = []
    proposals_dict = {}
    
    for proposal_data in proposals:
        proposal = TallyProposal(proposal_data)
        proposals_dict[proposal.id] = proposal
        
        # Check if this is a proposal that reached ACTIVE status and hasn't been announced
        if proposal.is_active and proposal.id not in announced_proposals:
            new_active_proposals.append(proposal)
            announced_proposals.add(proposal.id)

    # Post new proposals that reached ACTIVE status
    if new_active_proposals:
        # Sort proposals by creation date to ensure proper chronological order
        new_active_proposals.sort(key=lambda p: p.creation_date or datetime.min.replace(tzinfo=timezone.utc))
        
        print(f"Found {len(new_active_proposals)} new proposals that reached ACTIVE status to announce")
        for proposal in new_active_proposals:
            embed = proposal.create_embed()
            message = await channel.send(embed=embed)
            proposal.discord_message_id = str(message.id)
            save_announced_proposal(proposal)  # Save with discord_message_id
            await asyncio.sleep(1)  # Small delay between messages
    else:
        print("No new proposals that reached ACTIVE status found")
    
    # Sync existing proposals
    synced_count = 0
    for proposal_id, sync_info in proposals_to_sync.items():
        if proposal_id in proposals_dict:
            proposal = proposals_dict[proposal_id]
            
            # Sync all proposals with embeds until they reach a final status
            if proposal.is_syncable:
                success = await update_proposal_embed(channel, sync_info['discord_message_id'], proposal)
                if success:
                    update_proposal_sync_status(proposal.id, proposal.status)
                    synced_count += 1
                    print(f"Updated embed for proposal {proposal.id}")
            elif sync_info['last_status'] != proposal.status:
                # Status changed to final (e.g., CANCELED, DEFEATED, EXECUTED, etc.), update one last time
                success = await update_proposal_embed(channel, sync_info['discord_message_id'], proposal)
                if success:
                    update_proposal_sync_status(proposal.id, proposal.status)
                    print(f"Final update for proposal {proposal.id} - status changed to {proposal.status} (sync halted)")

    if synced_count > 0:
        print(f"Synced {synced_count} existing proposal embeds")

@bot.tree.command(name='clear_db', description='Clear the announced proposals database (admin only)')
@discord.app_commands.default_permissions(administrator=True)
async def clear_database_slash(interaction: discord.Interaction):
    """Clear the announced proposals database (admin only)"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM announced_proposals')
    conn.commit()
    conn.close()
    
    global announced_proposals
    announced_proposals = set()
    
    await interaction.response.send_message("Announced proposals database has been cleared.", ephemeral=True)

# Run the bot
if __name__ == "__main__":
    if not TALLY_API_KEY:
        print("ERROR: TALLY_API_KEY not found in environment variables!")
        print("Please add your Tally API key to the .env file")
        exit(1)
    
    bot.run(TOKEN)