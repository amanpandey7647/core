# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
from typing import Optional, Union

# --- Environment Loading ---
# Requires: pip install python-dotenv
from dotenv import load_dotenv

load_dotenv()  # Load variables from .env file if it exists

# --- Telegram Libraries ---
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.phone import GetGroupCallRequest
from telethon.tl.types import InputPeerChat, InputPeerChannel, InputGroupCall, GroupCallDiscarded, MessageMediaDocument

# --- Py-TgCalls Libraries (v3 Style) ---
from pytgcalls import PyTgCalls
from pytgcalls.types import Update
# Import v3 types if needed for specific flags/parameters
# from pytgcalls.types import MediaStream # Still useful in v3
# from pytgcalls.types.stream import StreamAudioEnded, StreamVideoEnded # Example v3 stream end types

# --- Optional: yt-dlp for URL extraction (as seen in vcbot.py) ---
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YTDLP_AVAILABLE = False
    logging.warning("yt-dlp not installed. YouTube URL handling might rely solely on PyTgCalls internal mechanisms or fail.")
from envv import *

# --- Configuration from Environment ---
try:
    API_ID = int(os.environ.get("API_ID", 0)) or apiid
    API_HASH = os.environ.get("API_HASH", None) or apihash
    # Use the exact variable name from vcbot.py example if preferred, e.g., SESSION_VC
    # For consistency with .env example, using STRING_SESSION
    SESSION_STRING = os.environ.get("STRING_SESSION", None) or session

    if not (API_ID and API_HASH and SESSION_STRING):
        raise ValueError("API_ID, API_HASH, and STRING_SESSION must be set in environment or .env file.")
except (TypeError, ValueError) as e:
    logging.critical(f"Error loading configuration from environment variables: {e}")
    exit(1)


# --- Global Variables ---
client: Optional[TelegramClient] = None
pytgcalls_instance: Optional[PyTgCalls] = None
current_call_chat_id: Optional[int] = None
currently_playing_downloaded_file: Optional[str] = None

# --- Logging ---
# Using basicConfig from vcbot.py example structure
LOG_LEVEL = logging.INFO # Or logging.DEBUG for more verbosity
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__) # Get logger instance


# --- Helper Functions ---
async def get_active_call(target_client: TelegramClient, chat_id: int) -> Optional[InputGroupCall]:
    # (Same as previous version)
    if not target_client: return None
    try:
        peer = await target_client.get_input_entity(chat_id)
        if not isinstance(peer, (InputPeerChat, InputPeerChannel)): return None
        call_result = await target_client(GetGroupCallRequest(peer=peer))
        if hasattr(call_result, 'call') and isinstance(call_result.call, InputGroupCall):
            if not isinstance(call_result.call, GroupCallDiscarded) and call_result.call.id != 0:
                return call_result.call
        return None
    except Exception as e:
        logger.warning(f"Could not get group call info for chat {chat_id}: {e}")
        return None

def is_url(text: str) -> bool:
    # (Same as previous version)
    return bool(re.match(r'^(?:http|ftp)s?://', text))

async def cleanup_file(path: Optional[str]):
    # (Same as previous version)
    global currently_playing_downloaded_file
    if path and os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"Cleaned up temporary file: {path}")
            if currently_playing_downloaded_file == path:
                currently_playing_downloaded_file = None
        except OSError as e:
            logger.error(f"Error removing file {path}: {e}")
    elif currently_playing_downloaded_file == path:
        currently_playing_downloaded_file = None

# --- Py-TgCalls v3 Event Handlers ---
def register_pytgcalls_handlers():
    if not pytgcalls_instance:
        logger.error("Cannot register PyTgCalls handlers: instance not created.")
        return

    # Example stream end handler (check v3 docs for exact update type)
    @pytgcalls_instance.on_stream_end()
    # async def stream_end_handler(update: Union[StreamAudioEnded, StreamVideoEnded]): # Use actual v3 types if known
    async def stream_end_handler(update): # Generic for now
        global currently_playing_downloaded_file
        # v3 might not pass chat_id easily here, rely on global state
        active_chat = current_call_chat_id
        logger.info(f"Stream ended in chat: {active_chat} (Update Type: {type(update)})")
        if currently_playing_downloaded_file:
            await cleanup_file(currently_playing_downloaded_file)
        if active_chat and client:
             pass # Optional: Notify chat

    # Example: Add handler for when the call itself is closed
    @pytgcalls_instance.on_closed_voice_chat()
    async def closed_handler():
        global current_call_chat_id, currently_playing_downloaded_file
        active_chat = current_call_chat_id
        logger.info(f"Call closed detected in chat: {active_chat}")
        if active_chat:
            current_call_chat_id = None # Reset state
            if currently_playing_downloaded_file:
                await cleanup_file(currently_playing_downloaded_file) # Cleanup if playing

    logger.info("Py-TgCalls v3 event handlers registered.")


# --- Telethon Event Handlers (Incoming from Anyone - v3 API based) ---
async def register_telethon_handlers(telethon_client: TelegramClient):

    # Help command
    @telethon_client.on(events.NewMessage(pattern=r'/vchelp', forwards=False))
    async def help_handler(event):
        me = await telethon_client.get_me()
        help_text = (f"👋 **VC Player ({me.first_name}) [v3 API]**\n\nCommands:\n"
                     "**/joinvc**: Join VC.\n**/leavevc**: Leave VC.\n"
                     "**/playvc [url|path]**: Play audio/video.\n"
                     "**/audiovc** (reply): Play replied audio/voice/video.\n"
                     "**/videovc** (reply): Play replied video.\n"
                     "**/stopvc**: Stop.\n**/pausevc**: Pause.\n**/resumevc**: Resume.")
        await event.reply(help_text)

    # Join command
    @telethon_client.on(events.NewMessage(pattern=r'/joinvc', forwards=False))
    async def join_handler(event):
        global current_call_chat_id
        chat_id = event.chat_id; sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        logger.info(f"Received /joinvc from {sender_name} ({sender.id}) in chat {chat_id}")
        chat_peer = await event.get_chat()
        if not isinstance(chat_peer, (InputPeerChat, InputPeerChannel)): return await event.reply("❌ Groups/Channels only.")
        if not pytgcalls_instance: return await event.reply("❌ Call handler not ready.")
        if current_call_chat_id and current_call_chat_id != chat_id: return await event.reply(f"😕 In call in `{current_call_chat_id}`.")
        elif current_call_chat_id == chat_id: return await event.reply("✅ Already joined.")

        status_msg = await event.reply("⏳ Checking VC...")
        try:
            active_call = await get_active_call(telethon_client, chat_id)
            if not active_call: return await status_msg.edit("🚫 No active VC found.")
            await status_msg.edit("📞 Joining...")
            await pytgcalls_instance.join_group_call(chat_id) # v3 join
            current_call_chat_id = chat_id
            await status_msg.edit(f"✅ Joined! (Req by {sender_name})")
            logger.info(f"Successfully joined chat {chat_id}")
        except Exception as e:
            logger.error(f"Join Error {chat_id}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Error joining: `{type(e).__name__}`")
            current_call_chat_id = None

    # Leave command
    @telethon_client.on(events.NewMessage(pattern=r'/leavevc', forwards=False))
    async def leave_handler(event):
        global current_call_chat_id, currently_playing_downloaded_file
        sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        if not pytgcalls_instance: return await event.reply("❌ Call handler not ready.")
        target_chat_id = current_call_chat_id if current_call_chat_id else event.chat_id
        if current_call_chat_id and current_call_chat_id != event.chat_id: return await event.reply(f"❓ Active in `{current_call_chat_id}`.")
        elif not current_call_chat_id: return await event.reply("❓ Not in VC.")

        status_msg = await event.reply("👋 Leaving...")
        try:
            await pytgcalls_instance.leave_call(target_chat_id) # v3 leave
            await status_msg.edit(f"✅ Left VC. (Req by {sender_name})")
            logger.info(f"Left call in chat {target_chat_id}")
            if currently_playing_downloaded_file: await cleanup_file(currently_playing_downloaded_file)
            if current_call_chat_id == target_chat_id: current_call_chat_id = None
        except Exception as e: # Catch generic v3 leave errors
            logger.error(f"Leave Error {target_chat_id}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Error leaving: `{type(e).__name__}`")

    # Shared Play Logic
    async def common_play_logic(event, input_text: Optional[str], is_video_hint: bool): # is_video used as hint now
        global current_call_chat_id, currently_playing_downloaded_file
        download_path: Optional[str] = None; status_msg = None; sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        if not pytgcalls_instance: return await event.reply("❌ Call handler not ready.")
        if not current_call_chat_id or current_call_chat_id != event.chat_id: return await event.reply("😕 Not in VC.")

        status_msg = await event.reply("⏳ Processing...")
        try: # Stop previous stream
            await pytgcalls_instance.stop_stream(current_call_chat_id); logger.info("Stopped previous stream.")
            if currently_playing_downloaded_file: await cleanup_file(currently_playing_downloaded_file)
            await asyncio.sleep(0.5)
        except Exception: pass # Ignore errors if nothing playing

        reply_msg = await event.get_reply_message(); is_reply = bool(reply_msg); stream_input = None
        # --- Reply Handling (same logic) ---
        if is_reply and (input_text is None):
            target_media = None; media_type = "unknown"
            # Media detection logic (same as before)
            if is_video_hint and reply_msg.video: target_media = reply_msg.video; media_type = "video"
            elif not is_video_hint and reply_msg.audio: target_media = reply_msg.audio; media_type = "audio"
            elif not is_video_hint and reply_msg.voice: target_media = reply_msg.voice; media_type = "voice note"
            elif not is_video_hint and reply_msg.video: target_media = reply_msg.video; media_type = "video (audio only)"
            elif reply_msg.document:
                 doc = reply_msg.document; mime_type = getattr(doc, 'mime_type', '').lower()
                 if is_video_hint and ('video' in mime_type): target_media = reply_msg.document; media_type = f"video document ({mime_type})"
                 elif not is_video_hint and ('audio' in mime_type or 'video' in mime_type): target_media = reply_msg.document; media_type = f"document ({mime_type}, audio only)"
            if not target_media: cmd = "/videovc" if is_video_hint else "/audiovc"; return await status_msg.edit(f"Reply to media with `{cmd}`.")
            # Download logic (same as before)
            try:
                await status_msg.edit(f"📥 Downloading {media_type}..."); os.makedirs("downloads", exist_ok=True)
                file_name_attr = getattr(target_media, 'attributes', None); dl_filename = None
                if file_name_attr:
                    for attr in file_name_attr:
                        if hasattr(attr, 'file_name'): dl_filename = attr.file_name; break
                base_name = dl_filename if dl_filename else f"{media_type.split(' ')[0]}_{reply_msg.id}"
                download_path = os.path.join("downloads", base_name)
                await telethon_client.download_media(reply_msg.media, file=download_path)
                if not os.path.exists(download_path): raise Exception("File not found after download.")
                await status_msg.edit(f"✅ Downloaded! Preparing..."); logger.info(f"Downloaded {media_type} to: {download_path}")
                stream_input = download_path; currently_playing_downloaded_file = download_path
            except Exception as e: logger.error(f"Download Error: {e}", exc_info=True); await status_msg.edit(f"❌ Download failed: `{e}`"); await cleanup_file(download_path); return
        # --- Argument Handling (same logic) ---
        elif input_text:
            stream_input = input_text.strip()
            if not is_url(stream_input) and not os.path.exists(stream_input): return await status_msg.edit(f"❓ Not URL/local path: `{stream_input}`")
            elif os.path.isdir(stream_input): return await status_msg.edit(f"❌ Is directory: `{stream_input}`")
        else: cmd = "/playvc"; return await status_msg.edit(f"Usage: `{cmd} [url|path]` or reply `/audiovc` or `/videovc`.")

        # --- Start Playback (v3) ---
        try:
            display_name = os.path.basename(stream_input) if os.path.exists(stream_input) else stream_input
            await status_msg.edit(f"▶️ Playing: **{display_name}** (Req by {sender_name})")
            # Use the v3 play method - pass path or URL directly
            await pytgcalls_instance.play(current_call_chat_id, stream_input)
            logger.info(f"Started playing: {stream_input} in chat {current_call_chat_id}")
        except Exception as e: # Generic exception for v3 play errors
            logger.error(f"Playback Error {stream_input}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Playback Error: `{type(e).__name__}`")
            await cleanup_file(download_path) # Cleanup if it was a downloaded file

    # Play command (v3 play handles both, maybe?)
    @telethon_client.on(events.NewMessage(pattern=r'/playvc(?: (.+))?', forwards=False))
    async def play_handler(event): await common_play_logic(event, event.pattern_match.group(1), is_video_hint=True) # Give hint it might be video

    # Audio reply command
    @telethon_client.on(events.NewMessage(pattern=r'/audiovc', forwards=False))
    async def audio_reply_handler(event):
        if not event.reply_to_msg_id: return await event.reply("Reply to audio/voice/video with `/audiovc`.")
        await common_play_logic(event, input_text=None, is_video_hint=False) # Hint audio preferred

    # Video reply command
    @telethon_client.on(events.NewMessage(pattern=r'/videovc', forwards=False))
    async def video_reply_handler(event):
        if not event.reply_to_msg_id: return await event.reply("Reply to video/doc with `/videovc`.")
        await common_play_logic(event, input_text=None, is_video_hint=True) # Hint video preferred

    # Stop command
    @telethon_client.on(events.NewMessage(pattern=r'/stopvc', forwards=False))
    async def stop_handler(event):
        global current_call_chat_id, currently_playing_downloaded_file; sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        if not pytgcalls_instance or not current_call_chat_id or current_call_chat_id != event.chat_id: return
        try:
            await event.reply("⏹️ Stopping...");
            await pytgcalls_instance.stop_stream(current_call_chat_id) # v3 method
            logger.info(f"Playback stopped by {sender_name} in {event.chat_id}")
            if currently_playing_downloaded_file: await cleanup_file(currently_playing_downloaded_file)
        except Exception as e: logger.error(f"Stop Error: {e}", exc_info=True); await event.reply(f"❌ Stop Error: `{type(e).__name__}`")

    # Pause command
    @telethon_client.on(events.NewMessage(pattern=r'/pausevc', forwards=False))
    async def pause_handler(event):
        if not pytgcalls_instance or not current_call_chat_id or current_call_chat_id != event.chat_id: return
        sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        try:
            await pytgcalls_instance.pause_stream(current_call_chat_id) # v3 method
            await event.reply(f"⏸️ Paused. (Req by {sender_name})"); logger.info(f"Paused in {current_call_chat_id}")
        except Exception as e: logger.error(f"Pause Error: {e}", exc_info=True); await event.reply(f"❌ Pause Error: `{type(e).__name__}`")

    # Resume command
    @telethon_client.on(events.NewMessage(pattern=r'/resumevc', forwards=False))
    async def resume_handler(event):
        if not pytgcalls_instance or not current_call_chat_id or current_call_chat_id != event.chat_id: return
        sender = await event.get_sender(); sender_name = getattr(sender, 'first_name', 'User')
        try:
            await pytgcalls_instance.resume_stream(current_call_chat_id) # v3 method
            await event.reply(f"⏯️ Resumed. (Req by {sender_name})"); logger.info(f"Resumed in {current_call_chat_id}")
        except Exception as e: logger.error(f"Resume Error: {e}", exc_info=True); await event.reply(f"❌ Resume Error: `{type(e).__name__}`")

    logger.info("Telethon incoming command handlers registered.")


# --- Main Execution Function ---
async def main():
    global client, pytgcalls_instance
    logger.info("Starting User VC Client (Incoming Mode - PyTgCalls v3 API)...")
    try:
        # Initialize Telethon Client with String Session
        logger.info("Initializing Telethon Client with String Session...")
        session = StringSession(SESSION_STRING)
        client = TelegramClient(session, API_ID, API_HASH,
                                # connection=ConnectionTcpAbridged, # Optional from vcbot.py
                                auto_reconnect=True, connection_retries=None)

        logger.info("Connecting Telethon client...");
        # Use start() directly which handles connect and auth check
        await client.start()
        # No need for separate connect/is_user_authorized with start()

        me = await client.get_me(); assert me, "Failed to get self user";
        logger.info(f"Telethon client started as User: {me.first_name} (@{me.username or 'N/A'}, ID: {me.id})")

        # Initialize Py-TgCalls v3 AFTER client is ready
        logger.info("Initializing Py-TgCalls v3 instance...")
        pytgcalls_instance = PyTgCalls(client) # Pass the initialized client
        register_pytgcalls_handlers() # Register handlers AFTER instance exists
        await pytgcalls_instance.start() # Start the pytgcalls internal client loop
        logger.info("Py-TgCalls started successfully.")

        await register_telethon_handlers(client);
        logger.info("Ready for commands (/vchelp).")
        # Keep the client running
        await client.run_until_disconnected()

    except ImportError as e: logger.error(f"Import Error: {e}. Check installations (pytgcalls v3?)!")
    # Add specific v3 exceptions here if known
    except ValueError as e: logger.error(f"Configuration/Value Error: {e}") # Catch env loading errors too
    except Exception as e: logger.exception(f"Critical runtime error: {e}")
    finally:
        logger.info("Shutting down...");
        # Graceful shutdown
        if pytgcalls_instance and current_call_chat_id:
            try: await pytgcalls_instance.leave_call(current_call_chat_id); logger.info("Left call during shutdown.")
            except Exception as leave_err: logger.warning(f"Shutdown leave error: {leave_err}")
        # Check if pytgcalls needs explicit stopping in v3
        # if pytgcalls_instance: await pytgcalls_instance.stop() # If stop method exists
        if client and client.is_connected(): await client.disconnect(); logger.info("Client disconnected.")
        if currently_play