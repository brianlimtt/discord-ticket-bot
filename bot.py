print("Bot script is starting...")
import discord
from discord.ext import commands
import os
import asyncio

TOKEN = os.getenv('DISCORD_BOT_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

TRIAL_MOD_ROLE_NAME = "Trial Moderator"
TICKET_CATEGORY_NAME = "Tickets"
BOOSTS_CHANNEL_NAME = "boosts"

import heapq

# Dictionary to keep track of active ticket numbers per guild per server_number
# Structure: {guild_id: {server_number: set of used ticket numbers}}
active_tickets_per_server = {}

# Global boosts queue data
# Each entry: {
#   "author": discord.User,
#   "discord_username": str,
#   "discord_user_id": int or None,
#   "ingame_username": str,
#   "seconds_left": int,
#   "ticket_channel": discord.TextChannel,
#   "task": asyncio.Task,
#   "server_number": int
# }
boosts_queue = []
boosts_pinned_message = {}  # To store the pinned boosts message per guild per server_number for editing, key: (guild.id, server_number)

def seconds_to_hhmmss(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"



def format_boosts_list_plaintext(server_number):
    """
    Returns the plain-text list of up to 20 boosts for the given server_number.
    Each line: Discord mention, in-game username, remaining time.
    """
    filtered_queue = [entry for entry in boosts_queue if entry.get("server_number") == server_number]
    lines = []
    for idx, entry in enumerate(filtered_queue[:20], start=1):
        mention = f"<@{entry['discord_user_id']}>" if entry.get('discord_user_id') else entry['discord_username']
        ingame = entry['ingame_username']
        time_left = seconds_to_hhmmss(entry['seconds_left'])
        line = f"{idx}. {mention} | In-game: {ingame} | Time left: {time_left}"
        lines.append(line)
    if lines:
        return "\n".join(lines)
    else:
        return "No active boosts."

async def update_boosts_message(guild, server_number):
    global boosts_pinned_message
    boosts_channel = discord.utils.get(guild.text_channels, name=BOOSTS_CHANNEL_NAME)
    if boosts_channel is None:
        return
    # Compose plain-text list for this server_number
    list_text = format_boosts_list_plaintext(server_number)
    allowed_mentions = discord.AllowedMentions(users=True)
    key = (guild.id, server_number)
    # Find or create the pinned boosts queue message for this server_number
    # We pin the message with content=list_text and NO embed
    if key not in boosts_pinned_message or boosts_pinned_message[key] is None:
        # Find the pinned boosts queue message with our text marker (first line is always "1. ..." or "No active boosts.")
        found = None
        async for msg in boosts_channel.pins():
            if msg.author == bot.user:
                # Check if this is the boosts list message for this server_number
                if msg.content.startswith("1. ") or msg.content.startswith("No active boosts."):
                    # Optionally, check if server_number is in the message (not strictly necessary if only one per server)
                    found = msg
                    break
        if found:
            boosts_pinned_message[key] = found
            try:
                await found.edit(content=list_text, embed=None, allowed_mentions=allowed_mentions)
            except Exception:
                pass
        else:
            # If no pinned message, send one and pin it
            boosts_pinned_message[key] = await boosts_channel.send(
                content=list_text,
                allowed_mentions=allowed_mentions
            )
            try:
                await boosts_pinned_message[key].pin()
            except discord.Forbidden:
                pass
    else:
        try:
            await boosts_pinned_message[key].edit(content=list_text, embed=None, allowed_mentions=allowed_mentions)
        except discord.NotFound:
            # Message was deleted, reset and recreate
            boosts_pinned_message[key] = await boosts_channel.send(
                content=list_text,
                allowed_mentions=allowed_mentions
            )
            try:
                await boosts_pinned_message[key].pin()
            except discord.Forbidden:
                pass
    return boosts_pinned_message[key]


# --- Countdown message tracking for ticket channels ---
ticket_countdown_messages = {}  # key: ticket_channel.id, value: discord.Message

async def countdown_task(entry, guild):
    # Real-time per-second countdown, updates Boosts pinned message and ticket channel countdown every second.
    server_number = entry.get("server_number", 1)
    ticket_channel = entry.get("ticket_channel")
    countdown_message = None
    # Remove any existing countdown message for this ticket channel
    if ticket_channel and ticket_channel.id in ticket_countdown_messages:
        try:
            old_msg = ticket_countdown_messages[ticket_channel.id]
            await old_msg.delete()
        except Exception:
            pass
        del ticket_countdown_messages[ticket_channel.id]
    # Send the initial countdown message in the ticket channel
    try:
        countdown_message = await ticket_channel.send(
            f"⏳ Boost time remaining: **{seconds_to_hhmmss(entry['seconds_left'])}**"
        )
        ticket_countdown_messages[ticket_channel.id] = countdown_message
    except Exception:
        countdown_message = None
    while entry["seconds_left"] > 0:
        await asyncio.sleep(1)
        entry["seconds_left"] -= 1
        await update_boosts_message(guild, server_number)
        # Update the countdown message in the ticket channel
        if countdown_message:
            try:
                await countdown_message.edit(
                    content=f"⏳ Boost time remaining: **{seconds_to_hhmmss(entry['seconds_left'])}**"
                )
            except (discord.NotFound, discord.Forbidden):
                countdown_message = None
                if ticket_channel and ticket_channel.id in ticket_countdown_messages:
                    del ticket_countdown_messages[ticket_channel.id]
    # When seconds_left reaches 0: remove from boosts_queue, update message, delete ticket channel, notify.
    if entry in boosts_queue:
        boosts_queue.remove(entry)
    await update_boosts_message(guild, server_number)
    boosts_channel = discord.utils.get(guild.text_channels, name=BOOSTS_CHANNEL_NAME)
    user_mention = f"<@{entry['discord_user_id']}>" if entry.get('discord_user_id') else entry['discord_username']
    if boosts_channel:
        await boosts_channel.send(f"Boost for **{user_mention}** has expired and been removed from the queue.")
    # Delete the countdown message in the ticket channel (if not already gone)
    if ticket_channel and ticket_channel.id in ticket_countdown_messages:
        try:
            msg = ticket_countdown_messages[ticket_channel.id]
            await msg.delete()
        except Exception:
            pass
        del ticket_countdown_messages[ticket_channel.id]
    # Delete the ticket channel if it still exists
    if ticket_channel:
        try:
            await ticket_channel.delete()
        except discord.NotFound:
            pass
        except Exception:
            pass

# --- Ticket deletion countdown with cancellation if user responds ---
# Only used for inactivity before approval.
ticket_deletion_tasks = {}  # key: channel_id, value: (task, author_id)

async def ticket_deletion_countdown(ticket_channel, opener_id):
    try:
        countdown_message = await ticket_channel.send("This ticket will be deleted in 60 seconds if there is no reply from the ticket opener.")
        seconds_left = 60
        while seconds_left > 0:
            await asyncio.sleep(1)
            seconds_left -= 1
            try:
                await countdown_message.edit(content=f"This ticket will be deleted in {seconds_left} seconds if there is no reply from the ticket opener.")
            except discord.NotFound:
                break
            # Check if the task is still the same (not cancelled)
            task_info = ticket_deletion_tasks.get(ticket_channel.id)
            if not task_info or task_info[0].cancelled():
                # Task cancelled, exit
                return
        # Delete the ticket channel if it still exists
        await ticket_channel.delete()
    except discord.NotFound:
        pass
    except Exception:
        pass

class TicketView(discord.ui.View):
    def __init__(self, author, discord_username, ingame_username, hours_left, ticket_channel, server_number):
        super().__init__(timeout=None)
        self.author = author
        self.discord_username = discord_username
        self.ingame_username = ingame_username
        self.hours_left = hours_left
        self.ticket_channel = ticket_channel
        self.server_number = server_number

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow Trial Moderators to interact
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild context not found.", ephemeral=True)
            return False
        trial_mod_role = discord.utils.get(guild.roles, name=TRIAL_MOD_ROLE_NAME)
        if trial_mod_role in interaction.user.roles:
            return True
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return False

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild context not found.", ephemeral=True)
            return

        boosts_channel = discord.utils.get(guild.text_channels, name=BOOSTS_CHANNEL_NAME)
        if boosts_channel is None:
            await interaction.response.send_message(f"Boosts channel '{BOOSTS_CHANNEL_NAME}' not found.", ephemeral=True)
            return

        # Validate hours_left is integer
        try:
            hours_int = int(self.hours_left)
            if hours_int <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Hours Remaining must be a positive integer. Please deny and ask the user to create the ticket again with correct input.", ephemeral=True)
            return

        # Send embed to boosts channel and ping the user
        embed = discord.Embed(title="Ticket Approved", color=discord.Color.green())
        embed.add_field(name="Discord Username", value=self.discord_username, inline=False)
        embed.add_field(name="Username (not display name)", value=self.ingame_username, inline=False)
        embed.add_field(name="Hours Remaining", value=str(hours_int), inline=False)
        embed.set_footer(text=f"Ticket opened by {self.author} | Approved by {interaction.user}")

        # Attempt to fetch the user by discord_username mention or name in guild members
        user_mention = None
        member = None
        for m in guild.members:
            if str(m) == self.discord_username or m.name == self.discord_username or m.display_name == self.discord_username:
                member = m
                break
        if member:
            user_mention = member.mention
            discord_user_id = member.id
        else:
            # fallback to just the username string
            user_mention = self.discord_username
            discord_user_id = None

        # Instead of sending a new message, just add to boosts_queue and update pinned message
        entry = {
            "author": self.author,
            "discord_username": self.discord_username,
            "discord_user_id": discord_user_id,
            "ingame_username": self.ingame_username,
            "seconds_left": hours_int * 3600,
            "ticket_channel": self.ticket_channel,
            "task": None,
            "server_number": self.server_number
        }
        boosts_queue.append(entry)

        # Update the pinned boosts queue message for this server_number
        await update_boosts_message(guild, self.server_number)

        # Start countdown task (this will delete the ticket channel on expiry AND update the ticket with live countdown)
        entry["task"] = asyncio.create_task(countdown_task(entry, guild))

        await interaction.response.send_message("Ticket approved and details added to Boosts queue. A live countdown has started in this ticket channel.", ephemeral=True)

        self.clear_items()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Deny ❌", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ticket denied and channel will be deleted.", ephemeral=True)
        # Cancel any countdown task if exists for this ticket
        to_remove = None
        for entry in boosts_queue:
            if entry["ticket_channel"] == self.ticket_channel:
                if entry["task"]:
                    entry["task"].cancel()
                to_remove = entry
                break
        if to_remove:
            boosts_queue.remove(to_remove)
            await update_boosts_message(interaction.guild, to_remove.get("server_number", 1))
        await self.ticket_channel.delete()

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

# --- New: Find or create a category for a given server_number ---
async def get_or_create_ps_category(guild, server_number):
    category_name = f"ps{server_number}"
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        # Ensure we don't exceed 50 categories
        if len(guild.categories) >= 50:
            raise Exception("Maximum number of categories reached in this server.")
        category = await guild.create_category(category_name)
    return category

@bot.command(name='createticket')
@commands.has_role(TRIAL_MOD_ROLE_NAME)
async def createticket(ctx):
    """Creates a ticket with step-by-step questions inside the ticket channel."""
    guild = ctx.guild

    # Initialize active tickets structure for this guild if not present
    if guild.id not in active_tickets_per_server:
        active_tickets_per_server[guild.id] = {}

    # Find a server_number (category) with < 20 active tickets, or create new
    server_number = 1
    while True:
        used_numbers = active_tickets_per_server[guild.id].get(server_number, set())
        if len(used_numbers) < 20:
            break
        server_number += 1

    # Enforce max 20 tickets per server_number
    if len(active_tickets_per_server[guild.id].get(server_number, set())) >= 20:
        await ctx.send(f"Maximum of 20 tickets reached for server ps{server_number}. Please close existing tickets before creating new ones.", delete_after=15)
        return

    # Find the lowest available ticket number for this server_number
    used_numbers = active_tickets_per_server[guild.id].get(server_number, set())
    # Ticket numbers are 1-based
    for ticket_number in range(1, 21):
        if ticket_number not in used_numbers:
            break
    # Reserve the ticket number now
    if server_number not in active_tickets_per_server[guild.id]:
        active_tickets_per_server[guild.id][server_number] = set()
    active_tickets_per_server[guild.id][server_number].add(ticket_number)

    # Create or get the correct category for this server_number
    try:
        category = await get_or_create_ps_category(guild, server_number)
    except Exception as e:
        # Release the ticket number reservation
        active_tickets_per_server[guild.id][server_number].discard(ticket_number)
        await ctx.send(str(e), delete_after=10)
        return

    # Set channel permissions for the ticket channel
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    trial_mod_role = discord.utils.get(guild.roles, name=TRIAL_MOD_ROLE_NAME)
    if trial_mod_role:
        overwrites[trial_mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel_name = f"ps{server_number}-ticket-{ticket_number}"
    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_number} opened by {ctx.author}",
            category=category
        )
    except discord.HTTPException as e:
        # Release the ticket number reservation
        active_tickets_per_server[guild.id][server_number].discard(ticket_number)
        await ctx.send(f"Failed to create ticket channel: {e}", delete_after=10)
        return

    def check_author(m):
        return m.author == ctx.author and m.channel == ticket_channel

    await ctx.send(f"{ctx.author.mention}, your ticket has been created: {ticket_channel.mention}", delete_after=10)

    # --- Start deletion countdown if user does not reply ---
    deletion_task = asyncio.create_task(ticket_deletion_countdown(ticket_channel, ctx.author.id))
    ticket_deletion_tasks[ticket_channel.id] = (deletion_task, ctx.author.id)

    # --- Listen for messages from the ticket opener to cancel deletion countdown ---
    def user_message_check(m):
        return m.channel == ticket_channel and m.author.id == ctx.author.id

    # Start a task to listen for the user's first message
    async def listen_for_opener_response():
        try:
            await bot.wait_for('message', check=user_message_check, timeout=60)
            # User responded, cancel deletion countdown if still running
            task_info = ticket_deletion_tasks.get(ticket_channel.id)
            if task_info:
                task, _ = task_info
                if not task.done():
                    task.cancel()
                del ticket_deletion_tasks[ticket_channel.id]
                try:
                    await ticket_channel.send("Ticket inactivity countdown cancelled. Please continue with the ticket process.")
                except Exception:
                    pass
        except asyncio.TimeoutError:
            # No response in 60 seconds, channel will be deleted by countdown
            pass
    asyncio.create_task(listen_for_opener_response())

    try:
        await ticket_channel.send(f"{ctx.author.mention}, Enter Discord username:")
        discord_username_msg = await bot.wait_for('message', check=check_author, timeout=300)
        discord_username = discord_username_msg.content.strip()

        await ticket_channel.send("Enter username (not display name):")
        ingame_username_msg = await bot.wait_for('message', check=check_author, timeout=300)
        ingame_username = ingame_username_msg.content.strip()

        # Validate hours remaining input as integer, ask repeatedly if invalid
        hours_left = None
        while hours_left is None:
            await ticket_channel.send("Enter hours remaining (integer only):")
            try:
                hours_left_msg = await bot.wait_for('message', check=check_author, timeout=300)
                hours_left_input = hours_left_msg.content.strip()
                hours_left_int = int(hours_left_input)
                if hours_left_int <= 0:
                    await ticket_channel.send("Please enter a positive integer for hours remaining.")
                    continue
                hours_left = str(hours_left_int)
            except ValueError:
                await ticket_channel.send("Invalid input. Please enter an integer number for hours remaining.")
            except asyncio.TimeoutError:
                await ticket_channel.send("You took too long to respond. Please run the command again.")
                # Release the ticket number reservation
                active_tickets_per_server[guild.id][server_number].discard(ticket_number)
                return

    except asyncio.TimeoutError:
        await ticket_channel.send("You took too long to respond. Please run the command again.")
        # Release the ticket number reservation
        active_tickets_per_server[guild.id][server_number].discard(ticket_number)
        return

    await ticket_channel.send(
        f"Ticket opened by {ctx.author.mention}\n"
        f"**Discord Username:** {discord_username}\n"
        f"**Username (not display name):** {ingame_username}\n"
        f"**Hours Remaining:** {hours_left}",
        view=TicketView(ctx.author, discord_username, ingame_username, hours_left, ticket_channel, server_number)
    )

    # Attach ticket number info to channel for deletion cleanup
    # Instead of setting attributes (which is not allowed), store info in a global dict
    if "ticket_channel_info" not in globals():
        global ticket_channel_info
        ticket_channel_info = {}
    ticket_channel_info[ticket_channel.id] = {"server_number": server_number, "ticket_number": ticket_number}

@bot.command(name='close')
@commands.has_role(TRIAL_MOD_ROLE_NAME)
async def close(ctx):
    # Before deleting, free up the ticket number if this is a psX-ticket-# channel
    channel = ctx.channel
    guild = ctx.guild
    server_number = None
    ticket_number = None
    # Try to extract from name
    import re
    match = re.match(r"ps(\d+)-ticket-(\d+)", channel.name)
    if match:
        server_number = int(match.group(1))
        ticket_number = int(match.group(2))
    # Or from global ticket_channel_info (if present)
    if "ticket_channel_info" in globals() and channel.id in ticket_channel_info:
        info = ticket_channel_info[channel.id]
        server_number = info.get("server_number", server_number)
        ticket_number = info.get("ticket_number", ticket_number)
    # Remove from active_tickets_per_server
    if guild and server_number and ticket_number:
        if guild.id in active_tickets_per_server and server_number in active_tickets_per_server[guild.id]:
            active_tickets_per_server[guild.id][server_number].discard(ticket_number)
    await ctx.send("This ticket will be closed in 5 seconds...")
    await asyncio.sleep(5)
    await channel.delete()

@bot.event
async def on_message(message):
    # Listen for ticket opener's message to cancel deletion countdown if needed
    if message.guild and message.channel and message.channel.id in ticket_deletion_tasks:
        task, author_id = ticket_deletion_tasks[message.channel.id]
        if message.author.id == author_id:
            if not task.done():
                task.cancel()
            del ticket_deletion_tasks[message.channel.id]
            try:
                await message.channel.send("Ticket inactivity countdown cancelled. Please continue with the ticket process.")
            except Exception:
                pass
    await bot.process_commands(message)


# -- Hook: Remove ticket number from active_tickets_per_server when channel deleted (for any reason) --
@bot.event
async def on_guild_channel_delete(channel):
    # Only process text channels
    if not isinstance(channel, discord.TextChannel):
        return
    # Check if channel name matches psX-ticket-#
    import re
    match = re.match(r"ps(\d+)-ticket-(\d+)", channel.name)
    server_number = None
    ticket_number = None
    if match:
        server_number = int(match.group(1))
        ticket_number = int(match.group(2))
    # Or from global ticket_channel_info
    if "ticket_channel_info" in globals() and channel.id in ticket_channel_info:
        info = ticket_channel_info[channel.id]
        server_number = info.get("server_number", server_number)
        ticket_number = info.get("ticket_number", ticket_number)
        del ticket_channel_info[channel.id]
    if server_number is None or ticket_number is None:
        return
    guild = channel.guild
    if guild and guild.id in active_tickets_per_server and server_number in active_tickets_per_server[guild.id]:
        active_tickets_per_server[guild.id][server_number].discard(ticket_number)

if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_BOT_TOKEN environment variable not set.")