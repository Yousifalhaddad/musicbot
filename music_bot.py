import os
import asyncio
from dataclasses import dataclass
from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ==============================
# CONFIG – CHANGE THESE
# ==============================

# Option 1: put your token here (do NOT share this file with anyone!)
TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"

# Option 2 (better): leave TOKEN = "" and set an environment variable DISCORD_TOKEN instead.
if not TOKEN:
    TOKEN = os.getenv("DISCORD_TOKEN")

# If you put your server ID here, slash commands appear almost instantly.
# If you leave it as 0, commands are global and may take a while to show up.
GUILD_ID = 0  # e.g. 123456789012345678

# ==============================
# YT-DLP / FFMPEG OPTIONS
# ==============================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ==============================
# DATA STRUCTURES
# ==============================

@dataclass
class Song:
    url: str           # what the user typed
    title: str
    webpage_url: str   # canonical YouTube link
    duration: int      # seconds
    audio_url: str     # direct stream URL
    requested_by: discord.abc.User


music_queues: Dict[int, List[Song]] = {}  # guild_id -> list[Song]


def get_queue(guild_id: int) -> List[Song]:
    return music_queues.setdefault(guild_id, [])


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def fetch_song(url: str, requester: discord.abc.User) -> Song:
    """Use yt-dlp to extract audio info without downloading the file."""
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, _extract)

    # If it was a playlist/search, take the first entry
    if "entries" in info:
        info = info["entries"][0]

    audio_url = info["url"]
    title = info.get("title", "Unknown title")
    webpage_url = info.get("webpage_url", url)
    duration = info.get("duration", 0)

    return Song(
        url=url,
        title=title,
        webpage_url=webpage_url,
        duration=duration,
        audio_url=audio_url,
        requested_by=requester,
    )


async def start_next_song(guild_id: int, bot: commands.Bot):
    """If nothing is playing, start the first song in the queue for this guild."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    queue = get_queue(guild_id)
    if not queue:
        return

    voice_client: discord.VoiceClient | None = guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        # No voice client, clear queue (nowhere to play)
        queue.clear()
        return

    # If something is already playing or paused, do nothing.
    if voice_client.is_playing() or voice_client.is_paused():
        return

    song = queue[0]

    def _after_play(error: Exception | None):
        if error:
            print(f"[Player error in guild {guild_id}]: {error}")

        # remove the song that just finished
        q = get_queue(guild_id)
        if q:
            q.pop(0)

        # schedule the next song on the event loop
        fut = asyncio.run_coroutine_threadsafe(start_next_song(guild_id, bot), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print(f"[Error starting next song]: {exc}")

    source = discord.FFmpegPCMAudio(song.audio_url, **FFMPEG_OPTIONS)

    try:
        voice_client.play(source, after=_after_play)
        print(f"[Now playing in {guild.name}]: {song.title}")
    except Exception as exc:
        print(f"[Error starting playback]: {exc}")
        # drop this song and try the next one
        if queue and queue[0] == song:
            queue.pop(0)
        await start_next_song(guild_id, bot)


# ==============================
# BOT SETUP
# ==============================

intents = discord.Intents.default()
intents.voice_states = True  # needed for voice
# message_content not needed for slash commands

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global command(s)")
    except Exception as e:
        print("Failed to sync slash commands:", e)


# ==============================
# SLASH COMMANDS
# ==============================

@bot.tree.command(name="play", description="Play a YouTube URL or add it to the queue")
@app_commands.describe(url="YouTube video URL (or search term)")
async def play_cmd(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    if interaction.guild is None:
        await interaction.followup.send("You can only use this command in a server.", ephemeral=True)
        return

    user = interaction.user
    voice_state = getattr(user, "voice", None)
    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("You must be in a voice channel first.", ephemeral=True)
        return

    voice_channel = voice_state.channel
    guild = interaction.guild
    voice_client: discord.VoiceClient | None = guild.voice_client

    # Join or move to user's voice channel
    try:
        if voice_client is None or not voice_client.is_connected():
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        await interaction.followup.send(f"I couldn't join your voice channel: `{e}`", ephemeral=True)
        return

    # Fetch song info
    try:
        song = await fetch_song(url, requester=user)
    except Exception as e:
        await interaction.followup.send(f"Couldn't get audio from that link: `{e}`", ephemeral=True)
        return

    queue = get_queue(guild.id)
    queue.append(song)

    if not voice_client.is_playing() and not voice_client.is_paused():
        # nothing playing -> start immediately
        await start_next_song(guild.id, bot)
        msg = f"▶️ Now playing: **[{song.title}]({song.webpage_url})** (`{format_duration(song.duration)}`)"
    else:
        position = len(queue)
        msg = (
            f"➕ Added to queue at position **{position}**:\n"
            f"**[{song.title}]({song.webpage_url})** (`{format_duration(song.duration)}`)"
        )

    await interaction.followup.send(msg)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return

    if not voice_client.is_playing() and not voice_client.is_paused():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return

    voice_client.stop()  # triggers the 'after' callback which starts the next song
    await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    queue = get_queue(guild_id)
    queue.clear()

    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()

    await interaction.response.send_message("⏹️ Stopped playback and cleared the queue.")


@bot.tree.command(name="leave", description="Disconnect from the voice channel and clear the queue")
async def leave_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    queue = get_queue(guild_id)
    queue.clear()

    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel and cleared the queue.")
    else:
        await interaction.response.send_message("I'm not connected to any voice channel.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current music queue")
async def queue_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    if not queue:
        await interaction.response.send_message("📭 The queue is empty.")
        return

    lines = []
    for i, song in enumerate(queue, start=1):
        prefix = "▶️" if i == 1 and interaction.guild.voice_client and interaction.guild.voice_client.is_playing() else f"{i}."
        lines.append(
            f"{prefix} **[{song.title}]({song.webpage_url})** "
            f"(`{format_duration(song.duration)}`) - requested by `{song.requested_by.display_name}`"
        )

    # Discord embed descriptions have a max length, but your queue won't be THAT cursed, probably.
    description = "\n".join(lines)
    embed = discord.Embed(title="🎶 Music queue", description=description, color=0x5865F2)
    await interaction.response.send_message(embed=embed)


# ==============================
# RUN
# ==============================

if __name__ == "__main__":
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("You forgot to put your bot token in the code or DISCORD_TOKEN env var.")
    bot.run(TOKEN)
