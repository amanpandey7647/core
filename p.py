# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
from typing import Optional

# --- Telegram Libraries ---
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession # For using pre-generated sessions
from telethon.tl.functions.phone import GetGroupCallRequest
from telethon.tl.types import InputPeerChat, InputPeerChannel, InputGroupCall, GroupCallDiscarded, MessageMediaDocument, MessageMediaPhoto

# --- Py-TgCalls Libraries ---
from pytgcalls import GroupCallFactory
from pytgcalls.exceptions import (
    GroupCallNotFoundError, NoActiveGroupCall, AlreadyJoinedError, RecordingNotFound,
    RequestTimeoutError, NoMtProtoClientSet, ClientNotStarted, InvalidVideoResolutionRequested,
    NodeJSNotInstalled, FFmpegNotInstalled
)
from pytgcalls.types import Update
from pytgcalls.types import AudioQuality, VideoQuality, MediaStream # For direct URL/yt-dlp streaming
from pytgcalls.types.input_stream import AudioPiped, AudioVideoPiped # For playing local/downloaded files

# --- Optional but recommended for URL handling ---
# pip install yt-dlp
try:
    import yt_dlp
except ImportError:
    yt_dlp = None
    logging.warning("yt-dlp not installed. Direct YouTube/URL streaming via MediaStream will be used, but advanced download/extraction/title fetching features are unavailable.")


# --- Configuration ---
# Get from https://my.telegram.org/apps
API_ID = 1234567  # Replace with your API ID (Integer)
API_HASH = "YOUR_API_HASH" # Replace with your API Hash (String)

# --- Choose ONE User Authentication Method ---

# Option 1: Interactive Login (Recommended for first time)
SESSION_NAME = "user_vc_session" # Name for the session file
STRING_SESSION = None

# Option 2: String Session (If you have generated one previously)
# SESSION_NAME = None
# STRING_SESSION = "YOUR_LONG_STRING_SESSION_HERE..." # Replace with your generated session string


# --- Global Variables ---
client: Optional[TelegramClient] = None
group_call_factory: Optional[GroupCallFactory] = None
current_call_chat_id: Optional[int] = None
currently_playing_downloaded_file: Optional[str] = None

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

async def get_active_call(target_client: TelegramClient, chat_id: int) -> Optional[InputGroupCall]:
    """Checks if there is an active group call in the chat using Telethon."""
    if not target_client: return None
    try:
        peer = await target_client.get_input_entity(chat_id)
        if not isinstance(peer, (InputPeerChat, InputPeerChannel)):
             logger.warning(f"Peer for chat ID {chat_id} is not a Chat or Channel.")
             return None
        call_result = await target_client(GetGroupCallRequest(peer=peer))
        if hasattr(call_result, 'call') and isinstance(call_result.call, InputGroupCall):
            if not isinstance(call_result.call, GroupCallDiscarded) and call_result.call.id != 0:
                return call_result.call
        return None
    except Exception as e:
        logger.warning(f"Could not get group call info for chat {chat_id}: {e}")
        return None

def is_url(text: str) -> bool:
    """Simple check if a string looks like a URL."""
    return bool(re.match(r'^(?:http|ftp)s?://', text))

async def cleanup_file(path: Optional[str]):
    """Removes a file if it exists and logs the action."""
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

# --- Py-TgCalls Setup ---
# (Identical to previous version - create_group_call_instance and its event handlers)
def create_group_call_instance():
    """Creates and configures the GroupCallFactory instance."""
    global group_call_factory
    if not client:
        raise ValueError("Telethon client must be initialized before PyTgCalls")

    group_call_factory = GroupCallFactory(client, GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON)
    app = group_call_factory.get_group_call()

    @app.on_network_status_changed
    async def network_status_changed_handler(gc: GroupCallFactory.MTPROTO_CLIENT, is_connected: bool):
        chat_desc = f"chat {gc.full_chat.id}" if gc.full_chat else "unknown chat"
        status = "Connected" if is_connected else "Disconnected"
        logger.info(f"Py-TgCalls Network: {status} in {chat_desc}")

    @app.on_stream_end()
    async def stream_end_handler(gc: GroupCallFactory.MTPROTO_CLIENT, update: Update):
        global currently_playing_downloaded_file
        chat_id = gc.full_chat.id if gc.full_chat else None
        logger.info(f"Stream ended in chat: {chat_id} (Payload: {update.payload})")
        # IMPORTANT: Clean up the downloaded file *after* the stream has truly ended
        if currently_playing_downloaded_file:
            await cleanup_file(currently_playing_downloaded_file)
        if chat_id and client:
            try:
                # Optional: Send message that playback finished
                # await client.send_message(chat_id, "Playback finished.")
                pass
            except Exception as send_err:
                 logger.warning(f"Could not send playback finished message to {chat_id}: {send_err}")

    @app.on_call_ended()
    async def call_ended_handler(gc: GroupCallFactory.MTPROTO_CLIENT, update: Update):
        global current_call_chat_id, currently_playing_downloaded_file
        chat_id = gc.full_chat.id if gc.full_chat else None
        logger.info(f"Group call itself ended in chat: {chat_id}")
        if chat_id and current_call_chat_id == chat_id:
            current_call_chat_id = None
            if currently_playing_downloaded_file:
                 await cleanup_file(currently_playing_downloaded_file)

    @app.on_call_joined()
    async def call_joined_handler(gc: GroupCallFactory.MTPROTO_CLIENT, update: Update):
        chat_id = gc.full_chat.id if gc.full_chat else None
        logger.info(f"Successfully joined call in chat: {chat_id}")

    @app.on_call_left()
    async def call_left_handler(gc: GroupCallFactory.MTPROTO_CLIENT, update: Update):
        chat_id = gc.full_chat.id if gc.full_chat else None
        logger.info(f"Left call confirmation received for chat: {chat_id}")

    logger.info("Py-TgCalls event handlers registered.")


# --- Telethon Event Handlers (Incoming from Anyone) ---

async def register_handlers(telethon_client: TelegramClient):
    """Registers all Telethon command handlers for incoming messages."""

    # Renamed to /vchelp, usable by anyone
    @telethon_client.on(events.NewMessage(pattern=r'/vchelp', forwards=False))
    async def help_handler(event):
        """Handler for the /vchelp command."""
        me = await telethon_client.get_me()
        help_text = (
            f"👋 **VC Player ({me.first_name})**\n\n"
            "Send commands in the group to control playback:\n"
            "**/joinvc**: Join the voice chat.\n"
            "**/leavevc**: Leave the voice chat.\n"
            "**/playvc [url|path]**: Play audio.\n"
            "**/vplayvc [url|path]**: Play video.\n"
            "**/audiovc** (reply to audio/voice/video): Play audio from replied file.\n"
            "**/videovc** (reply to video): Play replied video file.\n"
            "**/stopvc**: Stop playback.\n"
            "**/pausevc**: Pause playback.\n"
            "**/resumevc**: Resume playback."
        )
        await event.reply(help_text)

    # Reacts to incoming /joinvc command from anyone
    @telethon_client.on(events.NewMessage(pattern=r'/joinvc', forwards=False))
    async def join_handler(event):
        """Handler for the /joinvc command from any user."""
        global current_call_chat_id
        chat_id = event.chat_id
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')

        logger.info(f"Received /joinvc command from {sender_name} ({sender.id}) in chat {chat_id}")

        chat_peer = await event.get_chat()
        if not isinstance(chat_peer, (InputPeerChat, InputPeerChannel)):
             await event.reply("❌ This command only works in groups or channels.")
             return

        if not group_call_factory:
            await event.reply("❌ Error: Call handler not initialized.")
            return

        if current_call_chat_id and current_call_chat_id != chat_id:
            await event.reply(f"😕 Already in a call in chat `{current_call_chat_id}`. Use `/leavevc` there first.")
            return
        elif current_call_chat_id == chat_id:
            app = group_call_factory.get_group_call()
            if app and app.is_connected:
                 await event.reply("✅ Already joined and connected.")
                 return
            else:
                 logger.warning(f"State mismatch: current_call_chat_id is {chat_id}, but not connected. Attempting to rejoin.")
                 current_call_chat_id = None

        status_msg = await event.reply("⏳ Checking voice chat status...")
        try:
            active_call = await get_active_call(telethon_client, chat_id)
            if not active_call:
                await status_msg.edit("🚫 No active voice chat found. Please start one first.")
                return

            app = group_call_factory.get_group_call()
            await status_msg.edit("📞 Joining voice chat...")
            join_status = await app.join(chat_id)

            if join_status:
                current_call_chat_id = chat_id
                await status_msg.edit(f"✅ Joined the voice chat! (Requested by {sender_name})")
                logger.info(f"Successfully initiated join for chat {chat_id}")
            else:
                 await status_msg.edit("🤔 Couldn't join the voice chat (join returned false).")

        except AlreadyJoinedError:
            current_call_chat_id = chat_id
            await status_msg.edit("✅ Already in this voice chat.")
        except (GroupCallNotFoundError, NoActiveGroupCall):
            await status_msg.edit("🚫 No active voice chat found (or PyTgCalls couldn't detect it).")
        except (NoMtProtoClientSet, ClientNotStarted):
             logger.error("PyTgCalls client setup issue.")
             await status_msg.edit("❌ Internal Error: PyTgCalls client not set up correctly.")
        except RequestTimeoutError:
             await status_msg.edit("⏳ Timed out trying to join. Check permissions and network, then try again.")
        except Exception as e:
            logger.error(f"Error joining call in chat {chat_id}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Error joining: `{type(e).__name__}`")
            current_call_chat_id = None

    # Reacts to incoming /leavevc command from anyone
    @telethon_client.on(events.NewMessage(pattern=r'/leavevc', forwards=False))
    async def leave_handler(event):
        """Handler for the /leavevc command from any user."""
        global current_call_chat_id, currently_playing_downloaded_file
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')

        if not group_call_factory:
            await event.reply("❌ Error: Call handler not initialized.")
            return

        app = group_call_factory.get_group_call()
        target_chat_id = current_call_chat_id if current_call_chat_id else event.chat_id

        # Check if the command is from the chat we are supposedly in
        if current_call_chat_id and current_call_chat_id != event.chat_id:
             await event.reply(f"❓ Currently active in `{current_call_chat_id}`. Use `/leavevc` there.")
             return
        elif not current_call_chat_id or not app.is_connected:
             await event.reply("❓ Not currently in a voice chat.")
             return

        status_msg = await event.reply("👋 Leaving voice chat...")
        try:
            await app.leave_current_group_call()
            await status_msg.edit(f"✅ Left the voice chat. (Requested by {sender_name})")
            logger.info(f"Left call in chat {target_chat_id}")

            if currently_playing_downloaded_file:
                await cleanup_file(currently_playing_downloaded_file)

            if current_call_chat_id == target_chat_id:
                 current_call_chat_id = None

        except NoActiveGroupCall:
            await status_msg.edit("❓ Wasn't in an active call according to Py-TgCalls.")
            if current_call_chat_id == target_chat_id: current_call_chat_id = None
            if currently_playing_downloaded_file: await cleanup_file(currently_playing_downloaded_file)
        except RequestTimeoutError:
             await status_msg.edit("⏳ Timed out trying to leave.")
        except Exception as e:
            logger.error(f"Error leaving call from chat {target_chat_id}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Error leaving: `{type(e).__name__}`")


    async def common_play_logic(event, input_text: Optional[str], is_video: bool):
        """Shared logic for play commands, reacting to incoming messages."""
        global current_call_chat_id, currently_playing_downloaded_file
        download_path: Optional[str] = None
        status_msg = None # To send/edit status replies
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')

        if not group_call_factory:
            await event.reply("❌ Error: Call handler not initialized.")
            return

        if not current_call_chat_id or current_call_chat_id != event.chat_id:
            await event.reply("😕 Not in this group's voice chat. Use `/joinvc` first.")
            return

        app = group_call_factory.get_group_call()
        if not app or not app.is_connected:
             await event.reply("🔌 Connection to VC seems broken. Try `/joinvc` again.")
             current_call_chat_id = None
             return

        status_msg = await event.reply("Processing...")

        # --- Stop previous playback and cleanup ---
        if app.is_playing or app.is_paused:
             logger.info("Stopping previous playback before starting new one.")
             await status_msg.edit("⏹️ Stopping previous playback...")
             await app.stop_media()
             if currently_playing_downloaded_file:
                 await cleanup_file(currently_playing_downloaded_file)
             await asyncio.sleep(0.5)

        # --- Handle replied messages ---
        reply_msg = await event.get_reply_message()
        is_reply = bool(reply_msg)
        stream_input = None

        if is_reply and (input_text is None):
            target_media = None
            media_type = "unknown"
            # (Media detection logic remains the same as previous version)
            if is_video and reply_msg.video:
                target_media = reply_msg.video; media_type = "video"
            elif not is_video and reply_msg.audio:
                target_media = reply_msg.audio; media_type = "audio"
            elif not is_video and reply_msg.voice:
                target_media = reply_msg.voice; media_type = "voice note"
            elif not is_video and reply_msg.video:
                 target_media = reply_msg.video; media_type = "video (audio only)"
            elif reply_msg.document:
                 doc = reply_msg.document; mime_type = getattr(doc, 'mime_type', '').lower()
                 if is_video and ('video' in mime_type):
                      target_media = reply_msg.document; media_type = f"video document ({mime_type})"
                 elif not is_video and ('audio' in mime_type or 'video' in mime_type):
                      target_media = reply_msg.document; media_type = f"document ({mime_type}, audio only)"

            if not target_media:
                cmd = "/videovc" if is_video else "/audiovc"
                await status_msg.edit(f"Reply to a supported media file with `{cmd}`.")
                return

            try:
                await status_msg.edit(f"📥 Downloading replied {media_type}...")
                os.makedirs("downloads", exist_ok=True)
                file_name_attr = getattr(target_media, 'attributes', None)
                dl_filename = None
                if file_name_attr:
                    for attr in file_name_attr:
                        if hasattr(attr, 'file_name'): dl_filename = attr.file_name; break
                base_name = dl_filename if dl_filename else f"{media_type.split(' ')[0]}_{reply_msg.id}"
                download_path = os.path.join("downloads", base_name)

                await telethon_client.download_media(reply_msg.media, file=download_path)

                if not os.path.exists(download_path): raise Exception("File not found after download.")

                await status_msg.edit(f"✅ Downloaded! Preparing...")
                logger.info(f"Downloaded replied {media_type} to: {download_path}")
                stream_input = download_path
                currently_playing_downloaded_file = download_path
            except Exception as e:
                logger.error(f"Failed to download replied media: {e}", exc_info=True)
                await status_msg.edit(f"❌ Failed to download: `{e}`")
                await cleanup_file(download_path)
                return

        # --- Handle URL or File Path Arguments ---
        elif input_text:
            stream_input = input_text.strip()
            if not is_url(stream_input) and not os.path.exists(stream_input):
                await status_msg.edit(f"❓ Input is not a URL or local path: `{stream_input}`")
                return
            elif os.path.isdir(stream_input):
                 await status_msg.edit(f"❌ Input is a directory: `{stream_input}`")
                 return
        else:
             cmd = "/vplayvc" if is_video else "/playvc"
             await status_msg.edit(f"Usage: `{cmd} [url | path]` or reply `{cmd}` to media.")
             return

        # --- Prepare MediaStream or PipedStream ---
        stream_object = None
        display_name = stream_input
        try:
            # (Stream object creation logic remains the same)
            if os.path.exists(stream_input):
                if is_video:
                    stream_object = AudioVideoPiped(stream_input, AudioQuality.HIGH, VideoQuality.SD_480p)
                else:
                    stream_object = AudioPiped(stream_input, AudioQuality.HIGH)
                display_name = os.path.basename(stream_input)
            elif is_url(stream_input):
                stream_object = MediaStream(stream_input, AudioQuality.HIGH, VideoQuality.SD_480p if is_video else None)
                if yt_dlp and ("youtube.com" in stream_input or "youtu.be" in stream_input):
                    try:
                         with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True, 'format': 'bestaudio' if not is_video else 'best'}) as ydl:
                             info = ydl.extract_info(stream_input, download=False, process=False)
                             display_name = info.get('title', stream_input)
                    except Exception as ytdl_err: logger.warning(f"yt-dlp failed to get title: {ytdl_err}")
                else: display_name = stream_input # Fallback
            else: raise ValueError(f"Invalid stream input state: {stream_input}")

            # --- Start Playback ---
            play_type = 'Video' if is_video else 'Audio'
            await status_msg.edit(f"▶️ Playing {play_type}: **{display_name}** (Requested by {sender_name})")

            await app.play(stream_object)
            logger.info(f"Started playing {play_type.lower()}: {stream_input} in chat {current_call_chat_id}")

        except (InvalidVideoResolutionRequested, FFmpegNotInstalled, NodeJSNotInstalled) as dep_err:
            logger.error(f"Dependency/Configuration error: {dep_err}")
            await status_msg.edit(f"❌ Error: {dep_err}")
            await cleanup_file(download_path)
        except NoActiveGroupCall:
            await status_msg.edit("😕 Call ended or I left. Use `/joinvc` again.")
            current_call_chat_id = None
            await cleanup_file(download_path)
        except Exception as e:
            logger.error(f"Error during playback setup for {stream_input}: {e}", exc_info=True)
            await status_msg.edit(f"❌ Error starting playback: `{type(e).__name__}`")
            await cleanup_file(download_path)


    # Handlers for /playvc, /vplayvc, /audiovc, /videovc, /stopvc, /pausevc, /resumevc
    # These now react to *incoming* messages and use event.reply()

    @telethon_client.on(events.NewMessage(pattern=r'/playvc(?: (.+))?', forwards=False))
    async def play_handler(event):
        input_text = event.pattern_match.group(1)
        await common_play_logic(event, input_text, is_video=False)

    @telethon_client.on(events.NewMessage(pattern=r'/vplayvc(?: (.+))?', forwards=False))
    async def vplay_handler(event):
        input_text = event.pattern_match.group(1)
        await common_play_logic(event, input_text, is_video=True)

    @telethon_client.on(events.NewMessage(pattern=r'/audiovc', forwards=False))
    async def audio_reply_handler(event):
        if not event.reply_to_msg_id:
            await event.reply("Please reply to an audio, voice, or video file with `/audiovc`.")
            return
        await common_play_logic(event, input_text=None, is_video=False)

    @telethon_client.on(events.NewMessage(pattern=r'/videovc', forwards=False))
    async def video_reply_handler(event):
        if not event.reply_to_msg_id:
            await event.reply("Please reply to a video file or video document with `/videovc`.")
            return
        await common_play_logic(event, input_text=None, is_video=True)


    @telethon_client.on(events.NewMessage(pattern=r'/stopvc', forwards=False))
    async def stop_handler(event):
        global current_call_chat_id, currently_playing_downloaded_file
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')
        if not group_call_factory: return await event.reply("❌ Handler Error")
        if not current_call_chat_id or current_call_chat_id != event.chat_id:
             return # Silently ignore if not in the call of this chat

        app = group_call_factory.get_group_call()
        if not app.is_playing and not app.is_paused:
             return await event.reply("⏹️ Nothing is currently playing.")

        try:
            await event.reply("⏹️ Stopping playback...")
            await app.stop_media()
            # Don't edit the reply above, just send another one or log
            logger.info(f"Playback stopped by command from {sender_name} in chat {event.chat_id}")
            if currently_playing_downloaded_file:
                await cleanup_file(currently_playing_downloaded_file)
        except NoActiveGroupCall:
            await event.reply("❓ Not in an active call to stop.") # Should ideally be caught by initial check
            if current_call_chat_id == event.chat_id: current_call_chat_id = None
            if currently_playing_downloaded_file: await cleanup_file(currently_playing_downloaded_file)
        except Exception as e:
            logger.error(f"Error stopping playback: {e}", exc_info=True)
            await event.reply(f"❌ Error stopping: `{type(e).__name__}`")


    @telethon_client.on(events.NewMessage(pattern=r'/pausevc', forwards=False))
    async def pause_handler(event):
        if not group_call_factory: return await event.reply("❌ Handler Error")
        if not current_call_chat_id or current_call_chat_id != event.chat_id: return

        app = group_call_factory.get_group_call()
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')
        try:
            if await app.set_pause(True):
                await event.reply(f"⏸️ Playback paused. (Requested by {sender_name})")
                logger.info(f"Playback paused in chat {current_call_chat_id}")
            else:
                await event.reply("❓ Could not pause (maybe not playing or already paused?).")
        except NoActiveGroupCall: pass # Ignore if call ended meanwhile
        except Exception as e:
            logger.error(f"Error pausing playback: {e}", exc_info=True)
            await event.reply(f"❌ Error pausing: `{type(e).__name__}`")

    @telethon_client.on(events.NewMessage(pattern=r'/resumevc', forwards=False))
    async def resume_handler(event):
        if not group_call_factory: return await event.reply("❌ Handler Error")
        if not current_call_chat_id or current_call_chat_id != event.chat_id: return

        app = group_call_factory.get_group_call()
        sender = await event.get_sender()
        sender_name = getattr(sender, 'first_name', 'User')
        try:
            if await app.set_pause(False):
                await event.reply(f"⏯️ Playback resumed. (Requested by {sender_name})")
                logger.info(f"Playback resumed in chat {current_call_chat_id}")
            else:
                await event.reply("❓ Could not resume (maybe not paused or already playing?).")
        except NoActiveGroupCall: pass # Ignore if call ended meanwhile
        except Exception as e:
            logger.error(f"Error resuming playback: {e}", exc_info=True)
            await event.reply(f"❌ Error resuming: `{type(e).__name__}`")

    logger.info("Telethon incoming command handlers registered.")


# --- Main Execution Function ---
# (Identical to previous version - handles startup, shutdown, and cleanup)
async def main():
    global client
    logger.info("Starting User VC Client (Incoming Mode)...")

    session_source = None
    try:
        if STRING_SESSION:
            session_source = StringSession(STRING_SESSION)
            logger.info("Initializing Telethon with provided String Session.")
        elif SESSION_NAME:
            session_source = SESSION_NAME
            logger.info(f"Initializing Telethon with session file: '{SESSION_NAME}'.")
            logger.info("If file doesn't exist or is invalid, you'll be prompted for login.")
        else:
            raise ValueError("Either SESSION_NAME or STRING_SESSION must be configured.")

        client = TelegramClient(session_source, API_ID, API_HASH,
                                auto_reconnect=True, connection_retries=None)

        logger.info("Connecting Telethon client...")
        await client.connect()

        if not await client.is_user_authorized():
             logger.info("Client not authorized. Starting interactive login...")
             await client.start()
        else:
             logger.info("Client already authorized.")

        me = await client.get_me()
        if not me: raise Exception("Failed to get user information.")
        logger.info(f"Telethon client started as User: {me.first_name} (@{me.username or 'N/A'}, ID: {me.id})")

        logger.info("Initializing Py-TgCalls...")
        create_group_call_instance()
        await group_call_factory.start()
        logger.info("Py-TgCalls started successfully.")

        await register_handlers(client)
        logger.info("Event handlers registered. Listening for commands...")
        logger.info("Send /vchelp in a chat where the user is present for commands.")

        await client.run_until_disconnected()

    except ImportError as e:
         logger.error(f"Import Error: {e}. Install deps: pip install -U telethon \"pytgcalls[pyav]\" yt-dlp")
    except (FFmpegNotInstalled, NodeJSNotInstalled) as e:
         logger.error(f"Critical Dependency Error: {e}. Install and retry.")
    except ValueError as e:
         logger.error(f"Configuration Error: {e}")
    except Exception as e:
        logger.exception(f"Critical error during startup or runtime: {e}")
    finally:
        logger.info("Shutdown sequence initiated...")
        if group_call_factory:
            try:
                if current_call_chat_id:
                    app = group_call_factory.get_group_call()
                    if app and app.is_connected:
                         logger.info(f"Leaving call in {current_call_chat_id} during shutdown.")
                         await app.leave_current_group_call()
                         if currently_playing_downloaded_file:
                              await cleanup_file(currently_playing_downloaded_file)
            except Exception as leave_err:
                logger.warning(f"Error during PyTgCalls shutdown: {leave_err}")

        if client and client.is_connected():
            logger.info("Disconnecting Telethon client...")
            await client.disconnect()
            logger.info("Telethon client disconnected.")

        if currently_playing_downloaded_file:
             logger.warning(f"Performing final cleanup for: {currently_playing_downloaded_file}")
             await cleanup_file(currently_playing_downloaded_file)
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        os.makedirs("downloads", exist_ok=True)
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested via KeyboardInterrupt.")
    except Exception as e:
         logger.critical(f"Fatal error preventing event loop start: {e}", exc_info=True)
