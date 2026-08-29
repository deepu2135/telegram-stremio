import os
import uuid
import logging
import asyncio
import urllib.parse
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid
import pyrogram.enums

from config import Config
from tg_client import tg_client_manager
from utils import format_size, parse_split_info

logger = logging.getLogger("app_api")

router = APIRouter(prefix="/api")

# Dynamic Login Sessions Store: token -> session_data
# In-memory dictionary to hold temporary Pyrogram clients during login flow
login_sessions = {}

def update_env_file(updates: dict):
    """Safely updates or creates environment variables in the local .env file."""
    env_path = "/data/data/com.termux/files/home/Telegram-stremio/.env"
    lines = []
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Failed to read .env file: {e}")
            
    updated_keys = set()
    new_lines = []
    
    # Process existing lines and update values
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            try:
                key, val = stripped.split("=", 1)
                key = key.strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    updated_keys.add(key)
                    continue
            except ValueError:
                pass
        new_lines.append(line)
        
    # Append any keys that weren't in the original .env file
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")
            
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        logger.error(f"Failed to write to .env file: {e}")
        raise RuntimeError(f"Could not update .env configuration: {e}")

def reload_config():
    """Reloads the .env configuration and applies changes to Config."""
    try:
        from dotenv import load_dotenv
        env_path = "/data/data/com.termux/files/home/Telegram-stremio/.env"
        load_dotenv(dotenv_path=env_path, override=True)
    except ImportError:
        pass
        
    Config.API_ID = os.getenv("API_ID")
    Config.API_HASH = os.getenv("API_HASH")
    Config.BOT_TOKEN = os.getenv("BOT_TOKEN")
    Config.USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")
    Config.TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
    Config.LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
    
    # Run parsing/validation without crash to convert types
    if Config.API_ID:
        try:
            Config.API_ID = int(Config.API_ID)
        except ValueError:
            pass

@router.get("/auth/status")
async def auth_status():
    """Returns the login and configuration status of the Telegram client."""
    reload_config()
    
    needs_setup = not (Config.API_ID and Config.API_HASH)
    
    if needs_setup:
        return {
            "logged_in": False,
            "needs_setup": True,
            "api_id": None,
            "api_hash": None,
            "phone": None
        }
        
    logged_in = tg_client_manager.is_running and tg_client_manager.client is not None
    username = None
    phone = None
    
    if logged_in:
        try:
            me = await tg_client_manager.client.get_me()
            username = me.username or f"{me.first_name} {me.last_name or ''}".strip()
            phone = me.phone_number
        except Exception as e:
            logger.warning(f"Failed to get user profile: {e}")
            logged_in = False
            
    return {
        "logged_in": logged_in,
        "needs_setup": False,
        "api_id": Config.API_ID,
        "api_hash": Config.API_HASH[:6] + "..." if Config.API_HASH else None,
        "username": username,
        "phone": phone
    }

@router.post("/auth/setup")
async def auth_setup(request: Request):
    """Sets up the initial API_ID and API_HASH for the Telegram Application."""
    data = await request.json()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    
    if not api_id or not api_hash:
        raise HTTPException(status_code=400, detail="API ID and API Hash are required.")
        
    try:
        int(api_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="API ID must be an integer.")
        
    update_env_file({
        "API_ID": api_id,
        "API_HASH": api_hash
    })
    reload_config()
    
    return {"status": "success", "message": "API credentials configured successfully."}

@router.post("/auth/save-session")
async def auth_save_session(request: Request):
    """Saves API_ID, API_HASH, and USER_SESSION_STRING directly and connects."""
    data = await request.json()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    session_string = str(data.get("session_string", "")).strip()
    
    if not api_id or not api_hash or not session_string:
        raise HTTPException(status_code=400, detail="API ID, API Hash, and Session String are all required.")
        
    try:
        int(api_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="API ID must be an integer.")
        
    # Save values to .env file
    update_env_file({
        "API_ID": api_id,
        "API_HASH": api_hash,
        "USER_SESSION_STRING": session_string
    })
    reload_config()
    
    # Reboot the client manager using the new credentials
    try:
        await tg_client_manager.stop()
    except Exception as e:
        logger.warning(f"Error stopping client: {e}")
        
    tg_client_manager.initialize()
    try:
        await tg_client_manager.start()
    except Exception as e:
        # Rollback the session string so they can retry
        update_env_file({"USER_SESSION_STRING": ""})
        reload_config()
        tg_client_manager.client = None
        tg_client_manager.is_running = False
        raise HTTPException(
            status_code=400, 
            detail=f"Failed to connect to Telegram with the provided session: {str(e)}"
        )
        
    return {"status": "success", "message": "Successfully authorized with session string!"}

@router.post("/auth/save-bot")
async def auth_save_bot(request: Request):
    """Saves API_ID, API_HASH, and BOT_TOKEN directly and connects."""
    data = await request.json()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    bot_token = str(data.get("bot_token", "")).strip()
    
    if not api_id or not api_hash or not bot_token:
        raise HTTPException(status_code=400, detail="API ID, API Hash, and Bot Token are all required.")
        
    try:
        int(api_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="API ID must be an integer.")
        
    # Save values to .env file, clearing any existing user session
    update_env_file({
        "API_ID": api_id,
        "API_HASH": api_hash,
        "BOT_TOKEN": bot_token,
        "USER_SESSION_STRING": ""
    })
    reload_config()
    
    # Reboot the client manager using the new bot credentials
    try:
        await tg_client_manager.stop()
    except Exception as e:
        logger.warning(f"Error stopping client: {e}")
        
    tg_client_manager.initialize()
    try:
        await tg_client_manager.start()
    except Exception as e:
        # Rollback bot token on failure
        update_env_file({"BOT_TOKEN": ""})
        reload_config()
        tg_client_manager.client = None
        tg_client_manager.is_running = False
        raise HTTPException(
            status_code=400, 
            detail=f"Failed to connect to Telegram using the bot token: {str(e)}"
        )
        
    return {"status": "success", "message": "Successfully authorized bot client!"}

@router.post("/auth/send-code")
async def auth_send_code(request: Request):
    """Sends a verification code (OTP) to the specified Telegram phone number."""
    data = await request.json()
    phone_number = str(data.get("phone_number", "")).strip()
    
    reload_config()
    if not Config.API_ID or not Config.API_HASH:
        raise HTTPException(status_code=400, detail="Please configure API ID and API Hash first.")
        
    if not phone_number:
        raise HTTPException(status_code=400, detail="Phone number is required.")
        
    # Initialize a temporary in-memory Pyrogram client to send the OTP
    token = uuid.uuid4().hex
    client = Client(
        name=f"temp_login_{token}",
        api_id=int(Config.API_ID),
        api_hash=Config.API_HASH,
        in_memory=True,
        no_updates=True
    )
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone_number)
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.error(f"Failed to send code to {phone_number}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to send code: {str(e)}")
        
    # Save the login session state for subsequent sign-in step
    login_sessions[token] = {
        "client": client,
        "phone_number": phone_number,
        "phone_code_hash": sent_code.phone_code_hash,
        "created_at": asyncio.get_event_loop().time()
    }
    
    # Auto-cleanup expired login sessions in 10 minutes
    async def cleanup():
        await asyncio.sleep(600)
        sess = login_sessions.pop(token, None)
        if sess:
            try:
                await sess["client"].disconnect()
            except Exception:
                pass
    asyncio.create_task(cleanup())
    
    return {"status": "success", "token": token}

@router.post("/auth/sign-in")
async def auth_sign_in(request: Request):
    """Completes sign-in with the verification OTP code and optional 2FA password."""
    data = await request.json()
    token = data.get("token")
    code = str(data.get("code", "")).strip()
    password = data.get("password")  # Optional 2FA password
    
    if not token or token not in login_sessions:
        raise HTTPException(status_code=400, detail="Session expired or invalid. Please request a new code.")
        
    session = login_sessions[token]
    client = session["client"]
    phone_number = session["phone_number"]
    phone_code_hash = session["phone_code_hash"]
    
    if not code:
        raise HTTPException(status_code=400, detail="Verification code is required.")
        
    try:
        # Step 1: Sign in with phone number + OTP code
        await client.sign_in(
            phone_number=phone_number,
            phone_code_hash=phone_code_hash,
            phone_code=code
        )
    except SessionPasswordNeeded:
        # Step 2: If 2FA password is required
        if not password:
            return JSONResponse(
                status_code=401,
                content={"status": "password_required", "token": token, "message": "Two-factor authentication password is required."}
            )
        try:
            await client.check_password(password)
        except PasswordHashInvalid:
            raise HTTPException(status_code=400, detail="Incorrect 2FA password.")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"2FA Authentication failed: {str(e)}")
    except PhoneCodeInvalid:
        raise HTTPException(status_code=400, detail="The code you entered is invalid.")
    except PhoneCodeExpired:
        raise HTTPException(status_code=400, detail="The code has expired. Please request a new one.")
    except Exception as e:
        logger.error(f"Sign-in failure: {e}")
        raise HTTPException(status_code=400, detail=f"Authentication failed: {str(e)}")
        
    # Step 3: Successfully authenticated, export session and reboot main client
    try:
        session_string = await client.export_session_string()
        await client.disconnect()
        
        # Save to .env for persistence
        update_env_file({"USER_SESSION_STRING": session_string})
        reload_config()
        
        # Shut down current client manager client and start the new one
        await tg_client_manager.stop()
        tg_client_manager.initialize()
        await tg_client_manager.start()
        
        # Remove from active login sessions cache
        login_sessions.pop(token, None)
        
        return {"status": "success", "message": "Successfully logged in!"}
    except Exception as e:
        logger.error(f"Failed to finalize session setup: {e}")
        raise HTTPException(status_code=500, detail=f"Login finalized but system reboot failed: {str(e)}")

@router.post("/auth/logout")
async def auth_logout():
    """Logs out the user, stops Pyrogram, and clears the session credentials."""
    try:
        await tg_client_manager.stop()
    except Exception as e:
        logger.warning(f"Error stopping client on logout: {e}")
        
    update_env_file({"USER_SESSION_STRING": ""})
    reload_config()
    
    # Reinitialize manager as empty/unconnected state
    tg_client_manager.client = None
    tg_client_manager.is_running = False
    
    return {"status": "success", "message": "Logged out successfully."}

@router.get("/chats")
async def get_chats():
    """Retrieves all joined channels and groups for browsing, including archived folders."""
    if not tg_client_manager.is_running or not tg_client_manager.client:
        return {"logged_in": False, "chats": []}
        
    chats = []
    seen_ids = set()
    
    async def process_dialogs(folder_id):
        try:
            async for dialog in tg_client_manager.client.get_dialogs(limit=150, folder=folder_id):
                chat = dialog.chat
                if chat.id in seen_ids:
                    continue
                seen_ids.add(chat.id)
                
                title = chat.title or f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "Unnamed Chat"
                chat_type = str(chat.type.name).lower() if hasattr(chat.type, "name") else str(chat.type).split(".")[-1].lower()
                
                chats.append({
                    "id": chat.id,
                    "title": f"📦 {title} (Archived)" if folder_id == 1 else title,
                    "username": chat.username,
                    "type": chat_type,
                    "unread_count": dialog.unread_messages_count,
                    "archived": folder_id == 1
                })
        except Exception as folder_err:
            logger.warning(f"Failed to fetch dialogs for folder {folder_id}: {folder_err}")

    try:
        # Fetch main dialogs (folder 0) and archived dialogs (folder 1)
        await process_dialogs(0)
        await process_dialogs(1)
    except Exception as e:
        logger.error(f"Failed to fetch chats dialogs list: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load chats list: {str(e)}")
        
    return {"logged_in": True, "chats": chats}

def is_playable(msg):
    if msg.video or msg.audio or msg.video_note or msg.animation:
        return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime.startswith("video/") or mime.startswith("audio/"):
            return True
        fn = (msg.document.file_name or "").lower()
        video_exts = [".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".3gp", ".wmv", ".m4v", ".ts"]
        audio_exts = [".mp3", ".ogg", ".flac", ".wav", ".m4a", ".aac", ".opus", ".wma"]
        if any(fn.endswith(ext) for ext in video_exts + audio_exts):
            return True
    return False

@router.get("/chats/all/media")
async def get_all_chats_media(limit: int = 50):
    """Fetches and aggregates recent playable media across all joined chats/channels."""
    if not tg_client_manager.is_running or not tg_client_manager.client:
        raise HTTPException(status_code=401, detail="Telegram client is not running.")
        
    try:
        chats = []
        seen_ids = set()
        
        async def collect_chats(folder_id):
            try:
                async for dialog in tg_client_manager.client.get_dialogs(limit=50, folder=folder_id):
                    chat = dialog.chat
                    if chat.id in seen_ids:
                        continue
                    seen_ids.add(chat.id)
                    type_str = str(chat.type.name).lower() if hasattr(chat.type, "name") else str(chat.type).split(".")[-1].lower()
                    if type_str in ("channel", "supergroup", "group"):
                        chats.append((chat.id, chat.title or "Channel"))
                        if len(chats) >= 40:
                            break
            except Exception as fe:
                logger.warning(f"Failed to fetch folder {folder_id} dialogs: {fe}")
                
        await collect_chats(0)
        if len(chats) < 40:
            await collect_chats(1)
        
        if not chats:
            return {"status": "success", "media": []}
            
        results = []
        for chat_id, chat_title in chats:
            try:
                try:
                    await tg_client_manager.client.get_chat(chat_id)
                except Exception:
                    pass
                async for msg in tg_client_manager.client.get_chat_history(chat_id=chat_id, limit=15):
                    if is_playable(msg):
                        results.append(msg)
            except Exception as e:
                logger.warning(f"Failed to fetch history for chat {chat_id} ({chat_title}): {e}")
                
        results.sort(key=lambda m: m.date, reverse=True)
        results = results[:limit]
        
        media_list = []
        api_key_query = f"?api_key={Config.API_KEY}" if Config.API_KEY else ""
        
        for msg in results:
            media = msg.video or msg.document or msg.audio or msg.video_note or msg.animation
            if not media:
                continue
                
            fn = getattr(media, "file_name", "") or msg.caption or f"video_{msg.id}.mp4"
            fn = fn.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
            if len(fn) > 100:
                fn = fn[:97] + "..."
                
            chat_title = getattr(msg.chat, "title", "Channel") or "Channel"
            
            media_list.append({
                "id": msg.id,
                "chat_id": str(msg.chat.id),
                "chat_title": chat_title,
                "name": fn,
                "raw_size": media.file_size,
                "size": format_size(media.file_size),
                "mime_type": media.mime_type or "video/mp4",
                "date": msg.date,
                "caption": f"[{chat_title}] " + (msg.caption or ""),
                "type": "video" if not isinstance(media, pyrogram.types.Audio) else "audio",
                "stream_url": f"/stream/file/{msg.chat.id}/{msg.id}/{urllib.parse.quote(fn)}{api_key_query}",
                "poster_url": f"/poster/{msg.chat.id}/{msg.id}"
            })
            
        return {"status": "success", "media": media_list}
    except Exception as e:
        logger.error(f"Failed to fetch aggregated chat media: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch chat media: {str(e)}")

@router.get("/chats/{chat_id}/media")
async def get_chat_media(chat_id: str, query: str = "", offset_id: int = 0, limit: int = 50):
    """Lists playable video and audio files from the selected channel/chat history."""
    if not tg_client_manager.is_running or not tg_client_manager.client:
        raise HTTPException(status_code=401, detail="Telegram client is not running.")
        
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
            
        # Preemptively resolve peer to avoid PeerIdInvalid errors
        try:
            await tg_client_manager.client.get_chat(chat_id_val)
        except Exception as pe:
            logger.warning(f"Could not preemptively resolve chat peer {chat_id_val}: {pe}")
            
        results = []
        
        if query:
            # Search messages in history matching search term
            async for msg in tg_client_manager.client.search_messages(
                chat_id=chat_id_val,
                query=query,
                limit=100
            ):
                if is_playable(msg):
                    results.append(msg)
        else:
            # Stream/browse full chat history sequentially, scanning up to 800 messages to fill the page
            history_args = {"chat_id": chat_id_val, "limit": 100}
            if offset_id > 0:
                history_args["offset_id"] = offset_id
                
            scanned = 0
            async for msg in tg_client_manager.client.get_chat_history(**history_args):
                scanned += 1
                if is_playable(msg):
                    results.append(msg)
                if len(results) >= limit or scanned >= 800:
                    break
                        
        media_list = []
        api_key_query = f"?api_key={Config.API_KEY}" if Config.API_KEY else ""
        
        for msg in results:
            media = msg.video or msg.document or msg.audio or msg.video_note or msg.animation
            if not media:
                continue
                
            fn = getattr(media, "file_name", "") or msg.caption or f"video_{msg.id}.mp4"
            # Clean filename: replace newlines/carriage returns with space and strip quotes to prevent Uvicorn header crashes
            fn = fn.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
            if len(fn) > 100:
                fn = fn[:97] + "..."
            
            # Clean streaming URLs
            stream_url = f"/stream/file/{chat_id}/{msg.id}/{urllib.parse.quote(fn)}{api_key_query}"
            
            # Set poster image if thumbnail is available
            has_thumb = bool(getattr(media, 'thumbs', None))
            poster_url = f"/poster/{chat_id}/{msg.id}" if has_thumb else None
            
            # Determine correct type: video or audio
            mime = (getattr(media, "mime_type", "") or "").lower()
            fn_lower = fn.lower()
            is_audio_file = bool(msg.audio) or mime.startswith("audio/") or any(fn_lower.endswith(ext) for ext in [".mp3", ".ogg", ".flac", ".wav", ".m4a", ".aac", ".opus", ".wma"])
            
            media_type = "audio" if is_audio_file else "video"
            
            media_list.append({
                "id": msg.id,
                "chat_id": chat_id,
                "name": fn,
                "raw_size": media.file_size,
                "size": format_size(media.file_size),
                "mime_type": getattr(media, "mime_type", "video/mp4"),
                "date": msg.date.timestamp() if msg.date else 0,
                "caption": msg.caption,
                "type": media_type,
                "stream_url": stream_url,
                "poster_url": poster_url
            })
            
        return {"status": "success", "media": media_list}
        
    except Exception as e:
        logger.error(f"Failed to fetch media for chat {chat_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch chat media: {str(e)}")

@router.get("/search/global")
async def global_search_media(query: str, limit: int = 50):
    """Runs a global search across all joined channels for matching media titles."""
    if not tg_client_manager.is_running or not tg_client_manager.client:
        raise HTTPException(status_code=401, detail="Telegram client is not running.")
        
    if not query or not query.strip():
        return {"status": "success", "media": []}
        
    try:
        results = []
        async for msg in tg_client_manager.client.search_global(query=query, limit=100):
            if msg.video or msg.document or msg.audio:
                results.append(msg)
                if len(results) >= limit:
                    break
                    
        media_list = []
        api_key_query = f"?api_key={Config.API_KEY}" if Config.API_KEY else ""
        
        for msg in results:
            media = msg.video or msg.document or msg.audio
            if not media:
                continue
                
            fn = getattr(media, "file_name", "") or msg.caption or f"video_{msg.id}.mp4"
            
            stream_url = f"/stream/file/{msg.chat.id}/{msg.id}/{urllib.parse.quote(fn)}{api_key_query}"
            
            has_thumb = bool(getattr(media, 'thumbs', None))
            poster_url = f"/poster/{msg.chat.id}/{msg.id}" if has_thumb else None
            
            # Format chat title
            chat_title = msg.chat.title or "Channel Search"
            
            media_list.append({
                "id": msg.id,
                "chat_id": msg.chat.id,
                "chat_title": chat_title,
                "name": fn,
                "raw_size": media.file_size,
                "size": format_size(media.file_size),
                "mime_type": getattr(media, "mime_type", "video/mp4"),
                "date": msg.date.timestamp() if msg.date else 0,
                "caption": msg.caption,
                "type": "video" if msg.video else "audio" if msg.audio else "document",
                "stream_url": stream_url,
                "poster_url": poster_url
            })
            
        return {"status": "success", "media": media_list}
        
    except Exception as e:
        logger.error(f"Global search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Global search failed: {str(e)}")
