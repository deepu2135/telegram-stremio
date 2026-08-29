import logging
import asyncio

# Fix Pyrogram event loop crash on Python 3.12/3.14
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import urllib.parse
import markupsafe
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from config import Config
from tg_client import tg_client_manager
from utils import (
    format_size,
    matches_episode,
    get_metadata_from_cinemeta,
    matches_subtitle,
    get_search_query_from_filename,
    parse_split_info,
    is_video_file,
    matches_title
)
from zip_helper import (
    list_zip_files,
    TelegramSeekableReader,
    get_zip_entry_data_offset,
    zip_compressed_generator
)
from search_matcher import TelegramSearchMatcher, parse_quality, quality_tier, SCORE_THRESHOLD
from app_api import router as app_router


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] (%(name)s) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("stremio_addon")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        print("\n" + "=" * 60)
        print("   TELEGRAM ADDON")
        print("   For educational and personal testing only.")
        print("=" * 60 + "\n")
        
        try:
            Config.validate()
            await tg_client_manager.start()
        except ValueError as e:
            logger.warning(
                f"Configuration validation deferred: {e}. "
                "Please configure your API credentials through the web setup panel."
            )
        yield
    finally:
        await tg_client_manager.stop()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(app_router)

@app.middleware("http")
async def disable_proxy_buffering(request: Request, call_next):
    """Disable nginx/reverse-proxy buffering for streaming endpoints.
    HF Spaces uses nginx which buffers responses by default, causing
    multi-minute delays before the video player receives any data."""
    response = await call_next(request)
    if "/stream/" in request.url.path:
        response.headers["X-Accel-Buffering"] = "no"
    return response

def group_tg_messages(messages: list) -> list:
    grouped = {}
    standalone = []
    
    for msg in messages:
        media = msg.video or msg.document or msg.audio
        if not media:
            continue
            
        fn = getattr(media, "file_name", "") or msg.caption or f"Telegram File {msg.id}"
        base, part = parse_split_info(fn)
        
        if base and part is not None:
            key = base.lower()
            if key not in grouped:
                grouped[key] = {
                    "base_name": base,
                    "parts": {}
                }
            grouped[key]["parts"][part] = msg
        else:
            standalone.append(msg)
            
    results = []
    for key, data in grouped.items():
        parts = data["parts"]
        base_name = data["base_name"]
        
        if len(parts) == 1:
            results.append(list(parts.values())[0])
        else:
            sorted_parts = [msg for part, msg in sorted(parts.items())]
            results.append((base_name, sorted_parts))
            
    for msg in standalone:
        results.append(msg)
        
    return results

def verify_api_key(request: Request):
    if Config.API_KEY:
        api_key = request.query_params.get("api_key", "") or request.path_params.get("api_key", "")
        if api_key != Config.API_KEY:
            raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")

def get_manifest(api_key: str = ""):
    query_suffix = f"?api_key={api_key}" if api_key else ""
    catalogs = []

    # Browsable catalog for configured channel(s)
    if Config.TELEGRAM_CHANNEL_ID:
        catalogs.append({
            "type": "movie",
            "id": "telegram_channel",
            "name": "Telegram Channel",
            "extra": [{"name": "skip"}]
        })

    # Global search catalog (always available)
    catalogs.append({
        "type": "movie",
        "id": "telegram_search",
        "name": "Telegram Search",
        "extra": [{"name": "search", "isRequired": True}, {"name": "skip"}],
    })

    return {
        "id": "community.telegram.stremio.addon",
        "version": "1.0.0",
        "name": "Telegram",
        "description": "Personal Telegram streaming proxy. For educational & personal testing only. Do not use for unauthorized hosting of copyrighted media.",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg",
        "resources": ["meta", "stream", "subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tgfile_", "tt"],
        "catalogs": catalogs,
        "behaviorHints": {
            "configurable": False,
            "configurationRequired": False
        }
    }

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing(request: Request):
    template_path = "/data/data/com.termux/files/home/Telegram-stremio/templates/dashboard.html"
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except Exception as e:
        logger.error(f"Failed to read dashboard template: {e}")
        return HTMLResponse(
            content=f"<h1>Error loading application dashboard</h1><p>{str(e)}</p>", 
            status_code=500
        )

@app.api_route("/manifest.json", methods=["GET", "HEAD"])
@app.api_route("/{api_key}/manifest.json", methods=["GET", "HEAD"])
async def manifest_endpoint(api_key: str = ""):
    if Config.API_KEY and api_key != Config.API_KEY:
        return JSONResponse({"detail": "Unauthorized: Invalid API Key"}, status_code=403)
    return get_manifest(api_key)

@app.get("/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
async def catalog_handler(
    type: str, 
    catalog_id: str, 
    extra: str = None,
    api_key: str = ""
):
    if type not in ["movie", "series", "other"]:
        return {"metas": []}
        
    query = ""
    skip = 0
    if extra:
        params = urllib.parse.parse_qs(extra)
        if "search" in params:
            query = params["search"][0]
        if "skip" in params:
            try:
                skip = int(params["skip"][0])
            except ValueError:
                pass

    browse_limit = 500 if catalog_id == "telegram_channel" else 100
    try:
        messages = await tg_client_manager.search_messages(query=query, limit=browse_limit)
    except Exception as e:
        logger.error(f"Catalog search failed: {e}")
        return {"metas": []}

    grouped_items = group_tg_messages(messages)

    # Sort grouped items by date (newest first)
    def get_item_date(item):
        if isinstance(item, tuple):
            base_name, parts = item
            return max((msg.date for msg in parts if msg.date), default=0)
        else:
            return item.date if item.date else 0

    grouped_items.sort(key=get_item_date, reverse=True)

    # Slice for pagination (50 items per page)
    sliced_items = grouped_items[skip : skip + 50]

    metas = []
    logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None

    def get_poster_url(msg):
        """Use video thumbnail as poster if available, otherwise fall back to logo."""
        media = msg.video or msg.document or msg.audio
        if media and getattr(media, 'thumbs', None):
            return f"{Config.ADDON_URL}/poster/{msg.chat.id}/{msg.id}"
        return logo_url
    
    for item in sliced_items:
        if isinstance(item, tuple):
            base_name, parts = item
            total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
            first_msg = parts[0]
            chat_id = first_msg.chat.id
            msg_ids = ",".join(str(x.id) for x in parts)
            
            is_zip = False
            if base_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, parts)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            tg_id = f"tgfile_splitzip_{chat_id}_{msg_ids}//{entry.filename}"
                            metas.append({
                                "id": tg_id,
                                "type": type,
                                "name": entry.filename,
                                "description": f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {base_name}",
                                "poster": get_poster_url(first_msg),
                            })
                except Exception as e:
                    logger.error(f"Error reading split ZIP archive: {e}")
                    
            if not is_zip:
                tg_id = f"tgfile_split_{chat_id}_{msg_ids}"
                metas.append({
                    "id": tg_id,
                    "type": type,
                    "name": base_name,
                    "description": f"💾 Telegram File (Split Parts: {len(parts)})\n📦 Total Size: {format_size(total_size)}",
                    "poster": get_poster_url(first_msg),
                })
        else:
            msg = item
            media = msg.video or msg.document or msg.audio
            file_name = getattr(media, "file_name", None) or msg.caption or f"Telegram File {msg.id}"
            file_size = media.file_size
            caption = msg.caption or ""
            
            is_zip = False
            if file_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, msg)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            tg_id = f"tgfile_zip_{msg.chat.id}_{msg.id}//{entry.filename}"
                            metas.append({
                                "id": tg_id,
                                "type": type,
                                "name": entry.filename,
                                "description": f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {file_name}",
                                "poster": get_poster_url(msg),
                            })
                except Exception as e:
                    logger.error(f"Error reading standalone ZIP archive: {e}")
                    
            if not is_zip:
                tg_id = f"tgfile_{msg.chat.id}_{msg.id}"
                metas.append({
                    "id": tg_id,
                    "type": type,
                    "name": file_name,
                    "description": f"💾 Telegram File\n📦 Size: {format_size(file_size)}\n💬 {caption}" if caption else f"💾 Telegram File\n📦 Size: {format_size(file_size)}",
                    "poster": get_poster_url(msg),
                })
            
    return {"metas": metas}

from fastapi.responses import FileResponse
import os

@app.get("/stremio_telegram_logo.png")
async def get_logo():
    if os.path.exists("stremio_telegram_logo.png"):
        return FileResponse("stremio_telegram_logo.png")
    return Response(status_code=404)

@app.get("/stremio_telegram_banner.png")
async def get_banner():
    if os.path.exists("stremio_telegram_banner.png"):
        return FileResponse("stremio_telegram_banner.png")
    return Response(status_code=404)

_poster_cache = {}

@app.get("/poster/{chat_id}/{msg_id}")
async def poster_handler(chat_id: str, msg_id: int):
    """Serve video thumbnail from Telegram as poster image."""
    cache_key = f"{chat_id}:{msg_id}"
    if cache_key in _poster_cache:
        return Response(content=_poster_cache[cache_key], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id

        msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
        if not msg:
            return Response(status_code=404)

        media = msg.video or msg.document or msg.audio
        if not media or not getattr(media, 'thumbs', None):
            return Response(status_code=404)

        thumb = sorted(media.thumbs, key=lambda t: (t.width or 0) * (t.height or 0))[-1]
        file = await tg_client_manager.client.download_media(thumb.file_id, in_memory=True)
        if file:
            data = file.getvalue() if hasattr(file, 'getvalue') else bytes(file)
            _poster_cache[cache_key] = data
            return Response(content=data, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
        return Response(status_code=404)
    except Exception as e:
        logger.error(f"Failed to serve poster for {chat_id}/{msg_id}: {e}")
        return Response(status_code=404)

@app.get("/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
async def meta_handler(type: str, meta_id: str, api_key: str = ""):
    if not meta_id.startswith("tgfile_"):
        return {"meta": {}}
        
    try:
        is_zip_entry = False
        zip_entry_filename = ""
        base_meta_id = meta_id
        if "//" in meta_id:
            is_zip_entry = True
            base_meta_id, zip_entry_filename = meta_id.split("//", 1)
            
        chat_id_val = None
        msg_ids_str = ""
        is_split = False
        
        if base_meta_id.startswith("tgfile_splitzip_"):
            is_split = True
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        elif base_meta_id.startswith("tgfile_split_"):
            is_split = True
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        elif base_meta_id.startswith("tgfile_zip_"):
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        else:
            parts = base_meta_id.split("_")
            chat_id = parts[1]
            msg_ids_str = parts[2]
            
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
            
        msg_id_list = [int(x) for x in msg_ids_str.split(",") if x.strip().isdigit()]
        
        messages = []
        for msg_id in msg_id_list:
            msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
            if msg:
                messages.append(msg)
                
        if not messages:
            return {"meta": {}}
            
        first_msg = messages[0]
        media = first_msg.video or first_msg.document or first_msg.audio
        first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"
        
        if is_zip_entry and zip_entry_filename:
            file_name = zip_entry_filename
            zip_entries = await list_zip_files(tg_client_manager.client, messages)
            file_size = 0
            for entry in zip_entries:
                if entry.filename == zip_entry_filename:
                    file_size = entry.file_size
                    break
            description = f"💾 Telegram ZIP Entry\n📦 Size: {format_size(file_size)}\n📂 ZIP Archive: {first_fn}"
        else:
            file_name = first_fn
            if is_split:
                base_name, _ = parse_split_info(first_fn)
                file_name = base_name or first_fn
                total_size = sum((x.video or x.document or x.audio).file_size for x in messages if (x.video or x.document or x.audio))
                description = f"💾 Telegram File (Split Parts: {len(messages)})\n📦 Total Size: {format_size(total_size)}"
            else:
                total_size = media.file_size
                caption = first_msg.caption or ""
                description = f"💾 Telegram File\n📦 Size: {format_size(total_size)}\n💬 {caption}" if caption else f"💾 Telegram File\n📦 Size: {format_size(total_size)}"
                
        logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None
        poster_url = logo_url
        if media and getattr(media, 'thumbs', None):
            poster_url = f"{Config.ADDON_URL}/poster/{chat_id_val}/{first_msg.id}"

        meta = {
            "id": meta_id,
            "type": type,
            "name": file_name,
            "description": description,
            "poster": poster_url,
            "background": f"{Config.ADDON_URL}/stremio_telegram_banner.png" if getattr(Config, "ADDON_URL", None) else None,
            "logo": logo_url,
        }
        
        if type == "series":
            meta["videos"] = [
                {
                    "id": meta_id,
                    "title": file_name,
                    "season": 1,
                    "episode": 1
                }
            ]
            
        return {"meta": meta}
    except Exception as e:
        logger.error(f"Failed to generate metadata for {meta_id}: {e}")
        return {"meta": {}}


async def find_subtitles_for_video(video_filename: str, api_key: str = "", cached_messages=None) -> list:
    subtitles = []
    search_results = cached_messages or []
    query_param = f"?api_key={api_key}" if api_key else ""
    
    if not search_results:
        query = get_search_query_from_filename(video_filename)
        if query:
            try:
                search_results = await tg_client_manager.search_messages(query=query, limit=20)
            except Exception as e:
                logger.error(f"Subtitle search failed for '{query}': {e}")
                
    seen_msg_ids = set()
    for msg in search_results:
        if msg.id in seen_msg_ids:
            continue
            
        doc = msg.document or msg.audio or msg.video
        if not doc:
            continue
            
        sub_fn = getattr(doc, "file_name", "") or ""
        if sub_fn.lower().endswith(('.srt', '.vtt', '.ass')):
            if matches_subtitle(video_filename, sub_fn):
                seen_msg_ids.add(msg.id)
                
                lang = "eng"
                sub_fn_lower = sub_fn.lower()
                if ".spa" in sub_fn_lower or "spanish" in sub_fn_lower:
                    lang = "spa"
                elif ".fre" in sub_fn_lower or "french" in sub_fn_lower:
                    lang = "fre"
                
                subtitles.append({
                    "id": f"tgsub_{msg.chat.id}_{msg.id}",
                    "url": f"{Config.ADDON_URL}/stream/subtitle/{msg.chat.id}/{msg.id}/{urllib.parse.quote(sub_fn)}{query_param}",
                    "lang": lang
                })
                
    return subtitles

@app.get("/stream/{type}/{stream_id}.json")
@app.get("/{api_key}/stream/{type}/{stream_id}.json")
async def stream_handler(
    type: str, 
    stream_id: str,
    request: Request,
    api_key: str = ""
):
    if Config.API_KEY:
        actual_key = api_key or request.query_params.get("api_key", "")
        if actual_key != Config.API_KEY:
            raise HTTPException(status_code=403, detail="Unauthorized")
        
    streams = []
    query_param = f"?api_key={api_key}" if api_key else ""

    if stream_id.startswith("tgfile_"):
        if "//" in stream_id:
            base_stream_id, zip_entry_filename = stream_id.split("//", 1)
            is_split = False
            if base_stream_id.startswith("tgfile_splitzip_"):
                is_split = True
                parts = base_stream_id.split("_")
                chat_id = parts[2]
                msg_ids = parts[3]
            elif base_stream_id.startswith("tgfile_split_"):
                is_split = True
                parts = base_stream_id.split("_")
                chat_id = parts[2]
                msg_ids = parts[3]
            elif base_stream_id.startswith("tgfile_zip_"):
                parts = base_stream_id.split("_")
                chat_id = parts[2]
                msg_ids = parts[3]
            else:
                parts = base_stream_id.split("_")
                chat_id = parts[1]
                msg_ids = parts[2]
                
            try:
                chat_id_val = int(chat_id)
            except ValueError:
                chat_id_val = chat_id
                
            msg_id_list = [int(x) for x in msg_ids.split(",") if x.strip().isdigit()]
            
            try:
                messages = []
                for msg_id in msg_id_list:
                    msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
                    if msg:
                        messages.append(msg)
                        
                if messages:
                    zip_entries = await list_zip_files(tg_client_manager.client, messages)
                    file_size = 0
                    for entry in zip_entries:
                        if entry.filename == zip_entry_filename:
                            file_size = entry.file_size
                            break
                            
                    stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(zip_entry_filename)}{query_param}"
                    subtitles = await find_subtitles_for_video(zip_entry_filename, api_key=api_key)
                    
                    streams.append({
                        "name": "▶ TG ZIP Play",
                        "title": f"{zip_entry_filename}\n💾 Stream ZIP entry | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
            except Exception as e:
                logger.error(f"Failed resolving zip stream for {stream_id}: {e}")
        elif stream_id.startswith("tgfile_split_"):
            parts = stream_id.split("_")
            if len(parts) >= 4:
                chat_id = parts[2]
                msg_ids = parts[3]
                try:
                    msg_id_list = [int(x) for x in msg_ids.split(",") if x.isdigit()]
                    try:
                        chat_id_val = int(chat_id)
                    except ValueError:
                        chat_id_val = chat_id
                    
                    first_msg = await tg_client_manager.get_message(msg_id_list[0], chat_id=chat_id_val)
                    media = first_msg.video or first_msg.document or first_msg.audio
                    first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    base_name, _ = parse_split_info(first_fn)
                    if not base_name:
                        base_name = first_fn
                        
                    total_size = 0
                    for m_id in msg_id_list:
                        m = await tg_client_manager.get_message(m_id, chat_id=chat_id_val)
                        if m:
                            med = m.video or m.document or m.audio
                            if med:
                                total_size += med.file_size
                                
                    stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{query_param}"
                    
                    streams.append({
                        "name": "▶ TG Play (Split)",
                        "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                        "url": stream_url,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving split stream for {stream_id}: {e}")
        else:
            parts = stream_id.split("_")
            if len(parts) >= 3:
                chat_id = parts[1]
                msg_id = parts[2]
                try:
                    try:
                        chat_id_val = int(chat_id)
                    except ValueError:
                        chat_id_val = chat_id
                    msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                    media = msg.video or msg.document or msg.audio
                    file_name = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    file_size = media.file_size
                    
                    stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg_id}/{urllib.parse.quote(file_name)}{query_param}"
                    subtitles = await find_subtitles_for_video(file_name, api_key=api_key)
                    
                    streams.append({
                        "name": "▶ TG Play",
                        "title": f"{file_name}\n💾 Direct stream | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving direct stream for {stream_id}: {e}")

    elif stream_id.startswith("tt"):
        imdb_id = stream_id
        season = None
        episode = None
        
        if ":" in stream_id:
            parts = stream_id.split(":")
            imdb_id = parts[0]
            season = int(parts[1])
            episode = int(parts[2])
            
        try:
            meta = await get_metadata_from_cinemeta(type, imdb_id)
            movie_name = meta.get("name")
            year_str = meta.get("year")
            year = None
            if year_str:
                try:
                    year = int(str(year_str).split("-")[0])
                except Exception:
                    pass
            
            if movie_name:
                matcher = TelegramSearchMatcher()
                if type == "series" and season is not None and episode is not None:
                    queries = matcher.build_series_queries(movie_name, season, episode)
                else:
                    queries = matcher.build_movie_queries(movie_name, year)
                
                logger.info(f"Resolved IMDb {imdb_id} to '{movie_name}'. Searching Telegram with {len(queries)} queries...")
                
                search_tasks = [tg_client_manager.search_messages(query=q, limit=500) for q in queries]
                search_results_lists = await asyncio.gather(*search_tasks, return_exceptions=True)
                
                all_messages = {}
                tg_results_flat = []
                for res_list in search_results_lists:
                    if isinstance(res_list, list):
                        for msg in res_list:
                            if msg and (msg.chat.id, msg.id) not in all_messages:
                                all_messages[(msg.chat.id, msg.id)] = msg
                                tg_results_flat.append(msg)
                
                grouped_results = group_tg_messages(tg_results_flat)
                valid_streams = []
                
                for item in grouped_results:
                    if isinstance(item, tuple):
                        base_name, parts = item
                        first_msg = parts[0]
                        media = first_msg.video or first_msg.document or first_msg.audio
                        file_name = getattr(media, "file_name", "") or ""
                        caption = first_msg.caption or ""
                        
                        score = matcher.score(
                            file_name=base_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        
                        if score < SCORE_THRESHOLD:
                            continue
                            
                        total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
                        msg_ids = ",".join(str(x.id) for x in parts)
                        chat_id = first_msg.chat.id
                        quality_str = parse_quality(f"{base_name} {caption}")
                        
                        is_zip = False
                        if base_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, parts)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.score(
                                            file_name=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < SCORE_THRESHOLD:
                                            continue
                                            
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(entry.filename)}{query_param}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=api_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": f"▶ TG ZIP {quality_str}",
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_quality": quality_tier(quality_str),
                                            "_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking split ZIP for IMDB: {e}")
                                
                        if not is_zip:
                            if not is_video_file(base_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{query_param}"
                            valid_streams.append({
                                "name": f"▶ TG Split {quality_str}",
                                "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                                "url": stream_url,
                                "behaviorHints": {"notWebReady": True},
                                "_quality": quality_tier(quality_str),
                                "_size": total_size
                            })
                    else:
                        msg = item
                        media = msg.video or msg.document or msg.audio
                        file_name = getattr(media, "file_name", None) or msg.caption or ""
                        caption = msg.caption or ""
                        
                        score = matcher.score(
                            file_name=file_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        
                        if score < SCORE_THRESHOLD:
                            continue
                            
                        file_size = media.file_size
                        chat_id = msg.chat.id
                        quality_str = parse_quality(f"{file_name} {caption}")
                        
                        is_zip = False
                        if file_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, msg)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.score(
                                            file_name=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < SCORE_THRESHOLD:
                                            continue
                                            
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg.id}/{urllib.parse.quote(entry.filename)}{query_param}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=api_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": f"▶ TG ZIP {quality_str}",
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_quality": quality_tier(quality_str),
                                            "_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking standalone ZIP for IMDB: {e}")
                                
                        if not is_zip:
                            if not is_video_file(file_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg.id}/{urllib.parse.quote(file_name)}{query_param}"
                            subtitles = await find_subtitles_for_video(file_name, api_key=api_key, cached_messages=tg_results_flat)
                            
                            valid_streams.append({
                                "name": f"▶ TG Play {quality_str}",
                                "title": f"{file_name}\n💾 Telegram File | 📦 {format_size(file_size)}",
                                "url": stream_url,
                                "subtitles": subtitles,
                                "behaviorHints": {"notWebReady": True},
                                "_quality": quality_tier(quality_str),
                                "_size": file_size
                            })
                            
                valid_streams.sort(key=lambda x: (x.get("_size", 0), x.get("_quality", 0)), reverse=True)
                for s in valid_streams:
                    s.pop("_quality", None)
                    s.pop("_size", None)
                    streams.append(s)
                    
        except Exception as e:
            logger.error(f"Cinemeta search/resolve failed: {e}")

    return {"streams": streams}

@app.get("/subtitles/{type}/{id}.json")
@app.get("/subtitles/{type}/{id}/{extra}.json")
@app.get("/{api_key}/subtitles/{type}/{id}.json")
@app.get("/{api_key}/subtitles/{type}/{id}/{extra}.json")
async def subtitles_handler(
    type: str,
    id: str,
    request: Request,
    extra: str = None,
    api_key: str = ""
):
    if Config.API_KEY:
        actual_key = api_key or request.query_params.get("api_key", "")
        if actual_key != Config.API_KEY:
            raise HTTPException(status_code=403, detail="Unauthorized")
        
    subtitles = []
    
    if id.startswith("tgfile_"):
        parts = id.split("_")
        if len(parts) >= 3:
            chat_id = parts[1]
            msg_id = parts[2]
            try:
                try:
                    chat_id_val = int(chat_id)
                except ValueError:
                    chat_id_val = chat_id
                msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                media = msg.video or msg.document or msg.audio
                video_filename = getattr(media, "file_name", "") or ""
                if video_filename:
                    subtitles = await find_subtitles_for_video(video_filename, api_key=api_key)
            except Exception as e:
                logger.error(f"Failed to resolve subtitles for direct catalog ID {id}: {e}")
                
    elif id.startswith("tt"):
        imdb_id = id
        season = None
        episode = None
        if ":" in id:
            parts = id.split(":")
            imdb_id = parts[0]
            season = int(parts[1])
            episode = int(parts[2])
            
        try:
            video_filename = None
            if extra:
                decoded_extra = urllib.parse.unquote(extra)
                if "?" in decoded_extra:
                    decoded_extra = decoded_extra.split("?", 1)[0]
                params = urllib.parse.parse_qs(decoded_extra)
                if "filename" in params:
                    video_filename = params["filename"][0]

            if video_filename:
                logger.info(f"Resolving subtitles directly for filename: '{video_filename}'")
                subtitles = await find_subtitles_for_video(video_filename, api_key=api_key)
            else:
                meta = await get_metadata_from_cinemeta(type, imdb_id)
                movie_name = meta.get("name")
                if movie_name:
                    tg_results = await tg_client_manager.search_messages(query=movie_name, limit=50)
                    for msg in tg_results:
                        media = msg.video or msg.document or msg.audio
                        fn = getattr(media, "file_name", "") or msg.caption or ""
                        if type == "series" and not matches_episode(fn, season, episode):
                            continue
                        video_filename = fn
                        break
                    
                    if video_filename:
                        subtitles = await find_subtitles_for_video(video_filename, api_key=api_key, cached_messages=tg_results)
        except Exception as e:
            logger.error(f"Failed to resolve subtitles for IMDb ID {id}: {e}")
            
    return {"subtitles": subtitles}

@app.api_route("/stream/subtitle/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_subtitle_proxy(
    chat_id: str, 
    message_id: int, 
    filename: str,
    request: Request,
    api_key: str = ""
):
    if Config.API_KEY and api_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
        msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch subtitle message: {e}")
        raise HTTPException(status_code=404, detail="Subtitle file not found")
        
    if not msg:
        raise HTTPException(status_code=404, detail="Subtitle message not found")
        
    media = msg.document or msg.audio or msg.video
    if not media:
        raise HTTPException(status_code=404, detail="No media found in subtitle message")
        
    content_type = "text/plain"
    filename_lower = filename.lower()
    if filename_lower.endswith(".srt"):
        content_type = "application/x-subrip"
    elif filename_lower.endswith(".vtt"):
        content_type = "text/vtt"
    elif filename_lower.endswith(".ass"):
        content_type = "text/plain"
        
    safe_filename = filename.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
    if len(safe_filename) > 100:
        safe_filename = safe_filename[:97] + "..."
        
    headers = {
        "Content-Disposition": f'inline; filename="{safe_filename}"',
        "Access-Control-Allow-Origin": "*",
        "Content-Length": str(media.file_size),
    }
    
    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=content_type,
            headers=headers
        )
        
    try:
        logger.info(f"Downloading subtitle file from Telegram: {filename} (msg ID {message_id})")
        file_buffer = await tg_client_manager.client.download_media(msg, in_memory=True)
        content = file_buffer.getvalue()
    except Exception as e:
        logger.error(f"Failed to download subtitle file: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve subtitle media")
        
    return Response(
        content=content,
        media_type=content_type,
        headers=headers
    )

@app.api_route("/stream/file/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_stream_proxy(
    chat_id: str, 
    message_id: int, 
    filename: str, 
    request: Request,
    api_key: str = ""
):
    if Config.API_KEY and api_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
        # Always fetch a FRESH message to get a valid file_reference for streaming.
        # Cached messages have stale file_references that cause Telegram API errors.
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
        except Exception:
            # Fallback to cached version if direct fetch fails
            msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch message: {e}")
        raise HTTPException(status_code=404, detail="Media file not found")
        
    if not msg:
        raise HTTPException(status_code=404, detail="Media message not found")
        
    media = msg.video or msg.document or msg.audio
    if not media:
        raise HTTPException(status_code=404, detail="No playable media found in message")
        
    file_size = media.file_size
    mime_type = media.mime_type or "video/mp4"
    
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, message_id)
        )
    
    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1
    
    if range_header:
        try:
            bytes_range = range_header.replace("bytes=", "").split("-")
            if bytes_range[0]:
                start = int(bytes_range[0])
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass
            
    content_length = end - start + 1
    
    chunk_size = 512 * 1024
    offset = start // chunk_size
    skip_bytes = start % chunk_size
    
    status_code = 206 if range_header else 200
    
    safe_filename = filename.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
    if len(safe_filename) > 100:
        safe_filename = safe_filename[:97] + "..."
        
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{safe_filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    # Content-Range MUST only be sent with 206 Partial Content, not 200.
    # Sending it on 200 confuses video players and causes playback failures.
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    if request.method == "HEAD":
        logger.info(f"HEAD request for media '{filename}' (bytes {start}-{end}/{file_size}) - Status {status_code}")
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    async def file_generator():
        nonlocal msg
        bytes_sent = 0
        bytes_to_skip = skip_bytes
        retries = 0
        max_retries = 2
        current_offset = offset
        
        while retries <= max_retries:
            try:
                async for chunk in tg_client_manager.client.stream_media(msg, offset=current_offset):
                    if bytes_to_skip > 0:
                        if bytes_to_skip < len(chunk):
                            chunk = chunk[bytes_to_skip:]
                            bytes_to_skip = 0
                        else:
                            bytes_to_skip -= len(chunk)
                            continue
                            
                    if bytes_sent + len(chunk) > content_length:
                        chunk = chunk[:content_length - bytes_sent]
                        
                    yield chunk
                    bytes_sent += len(chunk)
                    
                    if bytes_sent >= content_length:
                        break
                # Completed successfully
                break
            except asyncio.CancelledError:
                logger.info(f"Stream cancelled by client for message {message_id}")
                break
            except Exception as e:
                err_name = type(e).__name__
                if "FileReferenceExpired" in err_name or "FILE_REFERENCE" in str(e).upper():
                    retries += 1
                    if retries <= max_retries:
                        logger.warning(f"File reference expired for msg {message_id}, refreshing (retry {retries}/{max_retries})...")
                        try:
                            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
                            # Calculate new offset based on bytes already sent
                            current_offset = (start + bytes_sent) // chunk_size
                            bytes_to_skip = (start + bytes_sent) % chunk_size
                            continue
                        except Exception as re_err:
                            logger.error(f"Failed to refresh message: {re_err}")
                            break
                    else:
                        logger.error(f"Max retries exceeded for file reference on msg {message_id}")
                        break
                else:
                    logger.error(f"Streaming error on message {message_id}: {e}")
                    break
            
    logger.info(f"Streaming media '{filename}' (bytes {start}-{end}/{file_size}) - Status {status_code}")
    
    return StreamingResponse(
        file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/split/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_split_stream_proxy(
    chat_id: str, 
    message_ids: str, 
    filename: str, 
    request: Request,
    api_key: str = ""
):
    if Config.API_KEY and api_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    msg_id_list = [int(x) for x in message_ids.split(",") if x.strip().isdigit()]
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")
        
    try:
        chat_id_val = int(chat_id)
    except ValueError:
        chat_id_val = chat_id
        
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, msg_id_list[0])
        )
        
    chunks_info = []
    total_size = 0
    
    for msg_id in msg_id_list:
        try:
            # Fetch fresh message for valid file_reference
            try:
                msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
            except Exception:
                msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
            if not msg:
                raise HTTPException(status_code=404, detail=f"Message {msg_id} not found")
            media = msg.video or msg.document or msg.audio
            if not media:
                raise HTTPException(status_code=400, detail=f"No media in message {msg_id}")
                
            chunks_info.append({
                "msg": msg,
                "media": media,
                "size": media.file_size,
                "start_byte": total_size,
                "end_byte": total_size + media.file_size - 1
            })
            total_size += media.file_size
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching metadata for msg {msg_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed resolving split file metadata")
            
    range_header = request.headers.get("Range")
    start = 0
    end = total_size - 1
    
    if range_header:
        try:
            bytes_range = range_header.replace("bytes=", "").split("-")
            if bytes_range[0]:
                start = int(bytes_range[0])
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass
            
    content_length = end - start + 1
    mime_type = chunks_info[0]["media"].mime_type or "video/mp4"
    
    status_code = 206 if range_header else 200
    
    safe_filename = filename.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
    if len(safe_filename) > 100:
        safe_filename = safe_filename[:97] + "..."
        
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{safe_filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    async def split_file_generator():
        bytes_sent = 0
        block_size = 1024 * 1024  # 1 MB blocks
        
        for chunk in chunks_info:
            c_start = chunk["start_byte"]
            c_end = chunk["end_byte"]
            
            if c_end < start or c_start > end:
                continue
                
            read_start = max(c_start, start)
            read_end = min(c_end, end)
            chunk_read_len = read_end - read_start + 1
            
            local_offset = read_start - c_start
            offset_blocks = local_offset // block_size
            skip_bytes = local_offset % block_size
            
            chunk_bytes_sent = 0
            bytes_to_skip = skip_bytes
            
            try:
                async for block in tg_client_manager.client.stream_media(chunk["msg"], offset=offset_blocks):
                    if bytes_to_skip > 0:
                        if bytes_to_skip < len(block):
                            block = block[bytes_to_skip:]
                            bytes_to_skip = 0
                        else:
                            bytes_to_skip -= len(block)
                            continue
                            
                    if chunk_bytes_sent + len(block) > chunk_read_len:
                        block = block[:chunk_read_len - chunk_bytes_sent]
                        
                    yield block
                    chunk_bytes_sent += len(block)
                    bytes_sent += len(block)
                    
                    if chunk_bytes_sent >= chunk_read_len:
                        break
            except Exception as e:
                logger.error(f"Error streaming split chunk: {e}")
                break
                
            if bytes_sent >= content_length:
                break
                
    logger.info(f"Streaming split media '{filename}' (bytes {start}-{end}/{total_size}) - Status {status_code}")
    
    return StreamingResponse(
        split_file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/zip/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_zip_stream_proxy(
    chat_id: str,
    message_ids: str,
    filename: str,
    request: Request,
    api_key: str = ""
):
    if Config.API_KEY and api_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    msg_id_list = [int(x) for x in message_ids.split(",") if x.strip().isdigit()]
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")
        
    try:
        chat_id_val = int(chat_id)
    except ValueError:
        chat_id_val = chat_id
        
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, msg_id_list[0])
        )
        
    messages = []
    for msg_id in msg_id_list:
        # Fetch fresh messages for valid file_reference
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
        except Exception:
            msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
        if msg:
            messages.append(msg)
            
    if not messages:
        raise HTTPException(status_code=404, detail="Messages not found")
        
    zip_entries = await list_zip_files(tg_client_manager.client, messages)
    target_entry = None
    for entry in zip_entries:
        if entry.filename == filename:
            target_entry = entry
            break
            
    if not target_entry:
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found in ZIP archive")
        
    file_size = target_entry.file_size
    mime_type = "video/mp4"
    filename_lower = filename.lower()
    if filename_lower.endswith(".mkv"):
        mime_type = "video/x-matroska"
    elif filename_lower.endswith(".mp4"):
        mime_type = "video/mp4"
    elif filename_lower.endswith(".avi"):
        mime_type = "video/x-msvideo"
        
    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1
    
    if range_header:
        try:
            bytes_range = range_header.replace("bytes=", "").split("-")
            if bytes_range[0]:
                start = int(bytes_range[0])
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass
            
    content_length = end - start + 1
    
    status_code = 206 if range_header else 200
    
    safe_filename = filename.replace("\r", " ").replace("\n", " ").replace('"', '').replace("'", '').strip()
    if len(safe_filename) > 100:
        safe_filename = safe_filename[:97] + "..."
        
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{safe_filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    import zipfile
    if target_entry.compress_type == zipfile.ZIP_STORED:
        logger.info(f"ZIP entry '{filename}' is STORED (uncompressed). Using direct offset proxy.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        data_start = await get_zip_entry_data_offset(reader, target_entry.header_offset)
        
        stream_start = data_start + start
        stream_end = data_start + end
        stream_len = stream_end - stream_start + 1
        
        chunks_info = []
        total_size = 0
        
        for part in reader.parts:
            chunks_info.append({
                "media": part["media"],
                "size": part["size"],
                "start_byte": part["start"],
                "end_byte": part["end"] - 1
            })
            total_size += part["size"]
            
        async def split_file_generator():
            bytes_sent = 0
            block_size = 1024 * 1024
            
            for chunk in chunks_info:
                c_start = chunk["start_byte"]
                c_end = chunk["end_byte"]
                
                if c_end < stream_start or c_start > stream_end:
                    continue
                    
                read_start = max(c_start, stream_start)
                read_end = min(c_end, stream_end)
                chunk_read_len = read_end - read_start + 1
                
                local_offset = read_start - c_start
                offset_blocks = local_offset // block_size
                skip_bytes = local_offset % block_size
                
                chunk_bytes_sent = 0
                bytes_to_skip = skip_bytes
                
                try:
                    async for block in tg_client_manager.client.stream_media(chunk["media"], offset=offset_blocks):
                        if bytes_to_skip > 0:
                            if bytes_to_skip < len(block):
                                block = block[bytes_to_skip:]
                                bytes_to_skip = 0
                            else:
                                bytes_to_skip -= len(block)
                                continue
                                
                        if chunk_bytes_sent + len(block) > chunk_read_len:
                            block = block[:chunk_read_len - chunk_bytes_sent]
                            
                        yield block
                        chunk_bytes_sent += len(block)
                        bytes_sent += len(block)
                        
                        if chunk_bytes_sent >= chunk_read_len:
                            break
                except Exception as e:
                    logger.error(f"Error streaming split ZIP chunk: {e}")
                    break
                    
                if bytes_sent >= stream_len:
                    break
                    
        logger.info(f"Streaming uncompressed ZIP entry '{filename}' (raw bytes {stream_start}-{stream_end}/{total_size}) - Status {status_code}")
        return StreamingResponse(
            split_file_generator(),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
    else:
        logger.info(f"ZIP entry '{filename}' is COMPRESSED (type {target_entry.compress_type}). Streaming on-the-fly decompression.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        return StreamingResponse(
            zip_compressed_generator(reader, filename, start, end),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("addon:app", host="0.0.0.0", port=Config.PORT, reload=True, timeout_keep_alive=300)
