import time
import logging
import asyncio
import functools
import inspect
import re
from hashlib import sha256
from typing import Callable, Optional, AsyncGenerator, Union

from pyrogram import Client, raw, utils
from pyrogram.types import Message
from pyrogram.session.auth import Auth
from pyrogram.session import Session
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.errors import VolumeLocNotFound, CDNFileHashMismatch
from pyrogram.crypto import aes
import pyrogram
from config import Config
from utils import parse_split_info

logger = logging.getLogger("tg_client")

# Monkey-patch to cache auth keys across media sessions
_original_auth_create = Auth.create
_auth_key_cache = {}

async def _patched_auth_create(self):
    if self.dc_id in _auth_key_cache:
        logger.info(f"Reusing cached auth key for DC{self.dc_id}")
        return _auth_key_cache[self.dc_id]
    
    logger.info(f"Generating new auth key for DC{self.dc_id}...")
    key = await _original_auth_create(self)
    _auth_key_cache[self.dc_id] = key
    return key

Auth.create = _patched_auth_create


class LockedMediaSession:
    def __init__(self, session: Session):
        self.session = session
        self.lock = asyncio.Lock()

    async def invoke(self, query):
        async with self.lock:
            return await self.session.invoke(query, sleep_threshold=30)

    async def stop(self):
        try:
            await self.session.stop()
        except Exception:
            pass


# Monkey-patch Client.get_file to reuse media sessions and avoid connection overhead
async def _patched_get_file(
    self: Client,
    file_id: FileId,
    file_size: int = 0,
    limit: int = 0,
    offset: int = 0,
    progress: Callable = None,
    progress_args: tuple = ()
) -> Optional[AsyncGenerator[bytes, None]]:
    async with self.get_file_semaphore:
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id,
                    access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(
                        chat_id=-file_id.chat_id
                    )
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                photo_id=file_id.media_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )

        current = 0
        total = abs(limit) or (1 << 31) - 1
        chunk_size = 1024 * 1024  # 1 MB chunk size for maximum throughput
        offset_bytes = abs(offset) * chunk_size

        dc_id = file_id.dc_id

        # Initialize custom sessions dictionary if not present
        if not hasattr(self, "_custom_media_sessions"):
            self._custom_media_sessions = {}
            self._custom_sessions_lock = asyncio.Lock()
            
        pool_size = max(1, min(getattr(Config, "PARALLEL_CONNECTIONS", 3), 16))
        prefetch_window = max(1, pool_size - 1)
        
        async with self._custom_sessions_lock:
            if dc_id not in self._custom_media_sessions:
                self._custom_media_sessions[dc_id] = []
                
            sessions = self._custom_media_sessions[dc_id]
            while len(sessions) < pool_size:
                logger.info(f"Creating parallel media session {len(sessions) + 1}/{pool_size} for DC{dc_id}...")
                session = Session(
                    self, dc_id,
                    await Auth(self, dc_id, await self.storage.test_mode()).create()
                    if dc_id != await self.storage.dc_id()
                    else await self.storage.auth_key(),
                    await self.storage.test_mode(),
                    is_media=True
                )
                await session.start()

                if dc_id != await self.storage.dc_id():
                    exported_auth = await self.invoke(
                        raw.functions.auth.ExportAuthorization(
                            dc_id=dc_id
                        )
                    )

                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )
                sessions.append(LockedMediaSession(session))

        prefetch_tasks = {}
        try:
            # Helper to fetch a chunk asynchronously using a specific session
            async def fetch_chunk(off_bytes, sess):
                return await sess.invoke(
                    raw.functions.upload.GetFile(
                        location=location,
                        offset=off_bytes,
                        limit=chunk_size
                    )
                )

            # Request first chunk using session 0
            r = await fetch_chunk(offset_bytes, sessions[0])

            if isinstance(r, raw.types.upload.File):
                # Start pre-fetching the next chunks in parallel using different sessions
                for idx in range(1, prefetch_window + 1):
                    chunk_offset = offset_bytes + idx * chunk_size
                    if idx >= total:
                        break
                    sess = sessions[idx % pool_size]
                    prefetch_tasks[chunk_offset] = asyncio.create_task(fetch_chunk(chunk_offset, sess))

                while True:
                    chunk = r.bytes
                    yield chunk

                    current += 1
                    yielded_offset = offset_bytes
                    offset_bytes += chunk_size

                    # Trigger next pre-fetch ahead in pipeline
                    next_prefetch_offset = yielded_offset + (prefetch_window + 1) * chunk_size
                    if current + prefetch_window < total:
                        sess = sessions[(current + prefetch_window) % pool_size]
                        prefetch_tasks[next_prefetch_offset] = asyncio.create_task(fetch_chunk(next_prefetch_offset, sess))

                    if progress:
                        func = functools.partial(
                            progress,
                            min(offset_bytes, file_size)
                            if file_size != 0
                            else offset_bytes,
                            file_size,
                            *progress_args
                        )

                        if inspect.iscoroutinefunction(progress):
                            await func()
                        else:
                            await self.loop.run_in_executor(self.executor, func)

                    if len(chunk) < chunk_size or current >= total:
                        break

                    # Retrieve the next chunk from the prefetch dict
                    next_chunk_offset = yielded_offset + chunk_size
                    if next_chunk_offset in prefetch_tasks:
                        try:
                            r = await prefetch_tasks[next_chunk_offset]
                            del prefetch_tasks[next_chunk_offset]
                            if not isinstance(r, raw.types.upload.File):
                                break
                        except asyncio.CancelledError:
                            break
                        except Exception as pre_err:
                            logger.error(f"Error in parallel prefetch: {pre_err}")
                            raise pre_err
                    else:
                        break

            elif isinstance(r, raw.types.upload.FileCdnRedirect):
                cdn_session = Session(
                    self, r.dc_id, await Auth(self, r.dc_id, await self.storage.test_mode()).create(),
                    await self.storage.test_mode(), is_media=True, is_cdn=True
                )

                try:
                    await cdn_session.start()

                    while True:
                        r2 = await cdn_session.invoke(
                            raw.functions.upload.GetCdnFile(
                                file_token=r.file_token,
                                offset=offset_bytes,
                                limit=chunk_size
                            )
                        )

                        if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                            try:
                                await sessions[0].invoke(
                                    raw.functions.upload.ReuploadCdnFile(
                                        file_token=r.file_token,
                                        request_token=r2.request_token
                                    )
                                )
                            except VolumeLocNotFound:
                                break
                            else:
                                continue

                        chunk = r2.bytes

                        decrypted_chunk = aes.ctr256_decrypt(
                            chunk,
                            r.encryption_key,
                            bytearray(
                                r.encryption_iv[:-4]
                                + (offset_bytes // 16).to_bytes(4, "big")
                            )
                        )

                        hashes = await sessions[0].invoke(
                            raw.functions.upload.GetCdnFileHashes(
                                file_token=r.file_token,
                                offset=offset_bytes
                            )
                        )

                        for i, h in enumerate(hashes):
                            cdn_chunk = decrypted_chunk[h.limit * i: h.limit * (i + 1)]
                            CDNFileHashMismatch.check(
                                h.hash == sha256(cdn_chunk).digest(),
                                "h.hash == sha256(cdn_chunk).digest()"
                            )

                        yield decrypted_chunk

                        current += 1
                        offset_bytes += chunk_size

                        if progress:
                            func = functools.partial(
                                progress,
                                min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                                file_size,
                                *progress_args
                            )

                            if inspect.iscoroutinefunction(progress):
                                await func()
                            else:
                                await self.loop.run_in_executor(self.executor, func)

                        if len(chunk) < chunk_size or current >= total:
                            break
                finally:
                    await cdn_session.stop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not isinstance(e, (pyrogram.StopTransmission, asyncio.CancelledError)):
                logger.warning(f"Error in media sessions for DC{dc_id}: {e}")
                async with self._custom_sessions_lock:
                    if dc_id in self._custom_media_sessions:
                        for s in self._custom_media_sessions[dc_id]:
                            try:
                                await s.stop()
                            except Exception:
                                pass
                        self._custom_media_sessions.pop(dc_id, None)
            raise e
        finally:
            for task in prefetch_tasks.values():
                if not task.done():
                    task.cancel()

Client.get_file = _patched_get_file


class TelegramClientManager:
    def __init__(self):
        self.client = None
        self.is_running = False
        self._search_cache = {}
        self._message_cache = {}
        self._log_cache = {}

    def initialize(self):
        Config.validate()
        
        if Config.USER_SESSION_STRING:
            logger.info("Initializing User Client...")
            self.client = Client(
                name="tg_stremio_user",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=Config.USER_SESSION_STRING,
                in_memory=True,
                no_updates=True,
                max_concurrent_transmissions=20
            )
        elif Config.BOT_TOKEN:
            logger.info("Initializing Bot Client...")
            self.client = Client(
                name="tg_stremio_bot",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=Config.BOT_TOKEN,
                in_memory=True,
                no_updates=True,
                max_concurrent_transmissions=20
            )
        else:
            raise ValueError("Neither USER_SESSION_STRING nor BOT_TOKEN is configured!")

    def get_channel_ids(self) -> list:
        val = Config.TELEGRAM_CHANNEL_ID
        if not val:
            return []
        if isinstance(val, int):
            return [val]
        parts = [p.strip() for p in str(val).split(",")]
        ids = []
        for p in parts:
            if p.startswith("-") or p.isdigit():
                try:
                    ids.append(int(p))
                except ValueError:
                    ids.append(p)
            else:
                ids.append(p)
        return ids

    async def start(self):
        if not self.client:
            self.initialize()
        
        if not self.is_running:
            logger.info("Starting Pyrogram client...")
            try:
                is_authorized = await self.client.connect()
            except Exception as e:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                if "struct" in str(e) or "unpack" in str(e) or "binascii" in str(e):
                    raise RuntimeError(
                        "Telegram session string is malformed or invalid! "
                        "The USER_SESSION_STRING you pasted into your GitHub repository secrets is corrupted or incomplete. "
                        "Please regenerate it and copy-paste it carefully."
                    ) from e
                raise e

            if not is_authorized:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                raise RuntimeError(
                    "Telegram client is not authorized! "
                    "Your USER_SESSION_STRING or BOT_TOKEN is invalid, expired, or missing. "
                    "Please check your GitHub repository secrets / environment variables."
                )
            self.is_running = True
            
            # Pre-cache user dialogs to populate Pyrogram's in-memory peer table
            # This prevents PeerIdInvalid errors when streaming from channels found in global search
            if Config.USER_SESSION_STRING:
                try:
                    logger.info("Pre-caching user dialogs and channel access hashes...")
                    async for _ in self.client.get_dialogs(limit=300):
                        pass
                    logger.info("User dialogs cached successfully.")
                except Exception as e:
                    logger.warning(f"Failed to pre-cache dialogs: {e}")
            
            # Resolve target channels if configured
            try:
                chat_ids = self.get_channel_ids()
                for chat_id in chat_ids:
                    try:
                        await self.client.get_chat(chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to cache channel {chat_id}: {e}")
                        
                if Config.LOG_CHANNEL_ID:
                    try:
                        await self.client.get_chat(Config.LOG_CHANNEL_ID)
                    except Exception as e:
                        logger.warning(f"Failed to cache log channel {Config.LOG_CHANNEL_ID}: {e}")
            except Exception as e:
                logger.warning(f"Failed to resolve target channels on startup: {e}")
            
            logger.info("Using Telegram global search across all channels in the account.")

    async def stop(self):
        if self.is_running and self.client:
            logger.info("Stopping Pyrogram client...")
            try:
                await asyncio.wait_for(self.client.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Pyrogram client stop timed out, skipping...")
            except Exception as e:
                logger.warning(f"Error stopping Pyrogram client: {e}")
            self.is_running = False

    async def send_play_log(self, filename: str, chat_id: Union[str, int], message_id: int):
        if not Config.LOG_CHANNEL_ID:
            return
            
        key = (chat_id, message_id)
        now = time.time()
        
        # Avoid duplicate logs for the same file within 15 mins
        if key in self._log_cache and now - self._log_cache[key] < 900:
            return
                
        self._log_cache[key] = now
        
        try:
            import datetime
            from datetime import timezone, timedelta
            
            tz_str = getattr(Config, "TIMEZONE", "UTC") or "UTC"
            local_dt = None
            
            try:
                from zoneinfo import ZoneInfo
                local_dt = datetime.datetime.now(ZoneInfo(tz_str))
            except Exception:
                pass
                
            if local_dt is None:
                try:
                    tz_clean = tz_str.upper().replace("UTC", "").replace("GMT", "").strip()
                    if tz_clean and tz_clean[0] in ("+", "-"):
                        sign = 1 if tz_clean[0] == "+" else -1
                        time_parts = tz_clean[1:].split(":")
                        hours = int(time_parts[0])
                        minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
                        td = timedelta(hours=hours, minutes=minutes)
                        local_dt = datetime.datetime.now(timezone(sign * td))
                except Exception:
                    pass
            
            if local_dt is None:
                local_dt = datetime.datetime.now(timezone.utc)
                
            time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            year_str = local_dt.strftime("%Y")
            
            message_text = (
                f"🎬 **Media Stream Log**\n\n"
                f"📁 **File Name:** `{filename}`\n"
                f"📅 **Date & Time:** `{time_str}`\n"
                f"📆 **Year:** `{year_str}`\n"
                f"💬 **Source Channel:** `{chat_id}`\n"
                f"🆔 **Message ID:** `{message_id}`"
            )
            
            await self.client.send_message(
                chat_id=Config.LOG_CHANNEL_ID,
                text=message_text
            )
        except Exception as e:
            logger.error(f"Failed to send log to log channel: {e}")

    async def search_messages(self, query: str = "", limit: int = 50):
        if not self.is_running:
            await self.start()
        
        query_str = str(query).strip() if query else ""
        
        cache_key = f"{query_str}:{limit}"
        now = time.time()
        if cache_key in self._search_cache:
            cached_time, cached_results = self._search_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_results

        results = []
        per_channel_limit = max(100, limit)
        
        if query_str:
            # Use Telegram's global search across ALL channels in the account
            try:
                logger.info(f"Performing global search for: '{query_str}'")
                async for msg in self.client.search_global(query=query_str, limit=per_channel_limit):
                    if self._has_media(msg):
                        results.append(msg)
            except Exception as e:
                logger.warning(f"Telegram global search failed: {e}")
        else:
            # For no-query browsing, fall back to channel-specific history if channels are configured
            chat_ids = self.get_channel_ids()
            if chat_ids:
                for chat_id in chat_ids:
                    try:
                        async for msg in self.client.get_chat_history(chat_id=chat_id, limit=per_channel_limit):
                            if self._has_media(msg):
                                results.append(msg)
                    except Exception as e:
                        logger.warning(f"Telegram history fetch failed for {chat_id}: {e}")
            else:
                logger.info("No query and no channels configured, returning empty results.")
        
        results.sort(key=lambda m: m.date, reverse=True)
        
        # Resolve all split parts for detected split files to prevent missing segments
        split_bases = set()
        for msg in results:
            media = msg.video or msg.document or msg.audio
            if media:
                fn = getattr(media, "file_name", "") or msg.caption or ""
                base, part = parse_split_info(fn)
                if base:
                    # Generate a clean, truncated search query for the split base
                    search_query = re.sub(r'[^a-zA-Z0-9\s]', ' ', base)
                    search_query = re.sub(r'\s+', ' ', search_query).strip()
                    words = search_query.split()
                    if words and words[-1].lower() in ('mkv', 'mp4', 'avi', 'mov', 'wmv', 'flv', 'webm', 'ts', 'm4v', 'zip'):
                        words = words[:-1]
                    if len(words) > 5:
                        search_query = " ".join(words[:5])
                    else:
                        search_query = " ".join(words)
                    split_bases.add(search_query)
                        
        additional_messages = []
        for base_query in split_bases:
            try:
                logger.info(f"Fetching all split parts matching base: {base_query}")
                async for msg in self.client.search_global(query=base_query, limit=100):
                    if self._has_media(msg):
                        additional_messages.append(msg)
            except Exception as e:
                logger.warning(f"Failed to fetch additional split parts for {base_query}: {e}")
                
        # Merge and deduplicate by (chat_id, message_id) to handle results from multiple channels
        deduped = {(msg.chat.id, msg.id): msg for msg in results}
        for msg in additional_messages:
            deduped[(msg.chat.id, msg.id)] = msg
            
        final_results = list(deduped.values())
        final_results.sort(key=lambda m: m.date, reverse=True)
        final_results = final_results[:limit]
        
        # Pre-populate message cache with all discovered search results
        for msg in final_results:
            if msg.chat:
                self._message_cache[f"{msg.chat.id}:{msg.id}"] = (now, msg)
                try:
                    self._message_cache[f"{int(msg.chat.id)}:{int(msg.id)}"] = (now, msg)
                except Exception:
                    pass
        
        self._search_cache[cache_key] = (now, final_results)
        return final_results

    async def get_message(self, message_id: int, chat_id: int = None) -> Message:
        if not self.is_running:
            await self.start()
            
        if chat_id is not None:
            target_chat = chat_id
        else:
            channel_ids = self.get_channel_ids()
            if channel_ids:
                target_chat = channel_ids[0]
            else:
                raise ValueError("chat_id is required when TELEGRAM_CHANNEL_ID is not configured")
        
        cache_key = f"{target_chat}:{message_id}"
        now = time.time()
        if cache_key in self._message_cache:
            cached_time, cached_msg = self._message_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_msg

        try:
            msg = await self.client.get_messages(chat_id=target_chat, message_ids=message_id)
            if msg:
                self._message_cache[cache_key] = (now, msg)
                return msg
        except Exception as e:
            err_name = type(e).__name__
            if "PeerIdInvalid" in err_name or "PEER_ID_INVALID" in str(e).upper():
                try:
                    logger.info(f"PeerIdInvalid for {target_chat}, refreshing dialogs...")
                    async for _ in self.client.get_dialogs(limit=300):
                        pass
                    msg = await self.client.get_messages(chat_id=target_chat, message_ids=message_id)
                    if msg:
                        self._message_cache[cache_key] = (now, msg)
                        return msg
                except Exception as re_err:
                    logger.error(f"Failed to resolve dialogs after PeerIdInvalid: {re_err}")
            
            # Check if message is in search cache
            for k, (ctime, cached_m) in self._message_cache.items():
                if getattr(cached_m, "id", None) == message_id:
                    logger.info(f"Using search-cached message for {target_chat}:{message_id}")
                    return cached_m
                    
            logger.error(f"Failed to fetch message {message_id} in channel {target_chat}: {e}")
            raise e

    def _has_media(self, msg: Message) -> bool:
        return bool(msg.video or msg.document or msg.audio)

tg_client_manager = TelegramClientManager()
