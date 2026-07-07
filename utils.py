import re
import logging
import unicodedata
import httpx

logger = logging.getLogger("utils")

def format_size(bytes_size: int) -> str:
    if not bytes_size:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while bytes_size >= 1024 and i < len(units) - 1:
        bytes_size /= 1024.0
        i += 1
    return f"{bytes_size:.2f} {units[i]}"

# Normalize common numbers and terminology for reliable matching
_NORM_MAP = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
    "temporada": "season", "temp": "season", "capitulo": "episode", 
    "capítulo": "episode", "cap": "episode", "ep": "episode", "ch": "episode", "chapter": "episode"
}

def _normalize_filename(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'\.[a-z0-9]{2,5}$', '', t)  # strip file extension
    t = re.sub(r'[._\-]', ' ', t)
    
    words = t.split()
    new_words = []
    for w in words:
        if w in _NORM_MAP:
            w = _NORM_MAP[w]
        new_words.append(w)
    return " ".join(new_words)

# Regex lists for identifying seasons/episodes in both orders
_SEASON_EPISODE_PATTERNS = [
    re.compile(r'\bs\s*(?P<s>\d{1,2})\s*[.\-_ ]?\s*e\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bseason\s*(?P<s>\d{1,2})\D{0,10}?episode\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bt\s*(?P<s>\d{1,2})\s*[.\-_ ]?\s*c\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'(?<!\d)(?P<s>\d{1,2})\s*[xX]\s*(?P<e>\d{1,3})(?!\d)'),
]

_EPISODE_SEASON_PATTERNS = [
    re.compile(r'\be\s*(?P<e>\d{1,3})\s*[.\-_ xX]?\s*s\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
    re.compile(r'\bepisode\s*(?P<e>\d{1,3})\D{0,10}?season\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
    re.compile(r'\bc\s*(?P<e>\d{1,3})\s*[.\-_ ]?\s*t\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
]

_STANDALONE_EPISODE_PATTERNS = [
    re.compile(r'\bep(?:isode)?\s*[.\-_ ]?\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bcap(?:itulo|ítulo)?\s*[.\-_ ]?\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\[(?P<e>\d{2,3})\]'),
    re.compile(r'(?:^|[\s\-_])[-–]\s*(?P<e>\d{2,3})\s*(?:[-–]|$|\.)'),
]

def parse_season_episode(filename: str) -> tuple:
    if not filename:
        return None, None
    
    fn = _normalize_filename(filename)
    
    for pat in _SEASON_EPISODE_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                s = int(m.group('s'))
                e = int(m.group('e'))
                return s, e
            except (ValueError, KeyError, IndexError):
                pass
                
    for pat in _EPISODE_SEASON_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                s = int(m.group('s'))
                e = int(m.group('e'))
                return s, e
            except (ValueError, KeyError, IndexError):
                pass
                
    for pat in _STANDALONE_EPISODE_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                e = int(m.group('e'))
                return 1, e
            except (ValueError, KeyError, IndexError):
                pass
                
    return None, None

def matches_episode(filename: str, season: int, episode: int) -> bool:
    if season is None or episode is None:
        return True
        
    f_season, f_episode = parse_season_episode(filename)
    if f_season == season and f_episode == episode:
        return True
        
    fn = _normalize_filename(filename)
    
    patterns = [
        rf'\bs\s*{season:02d}\s*[.\-_ ]?\s*e\s*{episode:02d}\b',
        rf'\bs\s*{season}\s*[.\-_ ]?\s*e\s*{episode:02d}\b',
        rf'(?<!\d){season}[xX]{episode:02d}(?!\d)',
        rf'(?<!\d){season}[xX]{episode}(?!\d)',
        rf'\[season\s*0*{season}\].*?\[episode\s*0*{episode}\]',
        rf'season\s*0*{season}\D{{0,20}}?episode\s*0*{episode}(?!\d)',
        rf'\bt\s*{season:02d}\s*c\s*{episode:02d}\b',
        rf'\bt\s*{season}\s*c\s*{episode}\b',
        rf'(?<!\d){season}{episode:02d}(?!\d)',
    ]
    
    # Allow fallback standalone episode checks for Season 1
    has_explicit_season = any(re.search(p, fn, re.IGNORECASE) for p in [r'\bs\d', r'\bseason\s*\d', r'\bt\d', r'\d+[xX]'])
    if season == 1 and not has_explicit_season:
        patterns += [
            rf'\bepisode\s*0*{episode}\b',
            rf'\bcap\s*0*{episode}\b',
            rf'\[0*{episode}\]',
            rf'[-–]\s*0*{episode:02d}\s*(?:[-–]|$)',
        ]
    
    for pat in patterns:
        if re.search(pat, fn, re.IGNORECASE):
            return True
            
    return False

def matches_subtitle(video_filename: str, sub_filename: str) -> bool:
    if not video_filename or not sub_filename:
        return False
        
    v_fn = video_filename.lower()
    s_fn = sub_filename.lower()
    
    v_base = v_fn.rsplit('.', 1)[0]
    s_base = s_fn.rsplit('.', 1)[0]
    
    s_base_clean = re.sub(r'\.(eng|en|english|sub|subtitle|srt|vtt)$', '', s_base)
    
    if s_base_clean in v_base or v_base in s_base_clean:
        return True
        
    return False

def get_search_query_from_filename(filename: str) -> str:
    if not filename:
        return ""
    name = filename.lower()
    name = name.rsplit('.', 1)[0]
    name = re.sub(r'[._\-]', ' ', name)
    
    terms = r'\b(2160p|1080p|720p|480p|360p|4k|8k|10bit|h264|x264|h265|x265|hevc|web[- ]?rip|bluray|brrip|hdrip)\b'
    match = re.search(terms, name)
    if match:
        name = name[:match.start()]
    
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def parse_split_info(filename: str) -> tuple:
    if not filename:
        return None, None
        
    # Match suffix .001, .002 etc.
    m1 = re.search(r'\.(\d{3,4})$', filename)
    if m1:
        part = int(m1.group(1))
        base = filename[:m1.start()]
        return base, part
        
    # Match part1, part01, part_1 etc.
    m2 = re.search(r'[._\- ]part_?(\d+)(?:\.([^.]+))?$', filename, re.IGNORECASE)
    if m2:
        part = int(m2.group(1))
        ext = m2.group(2) or ""
        base = filename[:m2.start()]
        if ext:
            base += f".{ext}"
        return base, part
        
    return None, None

_metadata_cache = {}

async def get_metadata_from_cinemeta(meta_type: str, imdb_id: str) -> dict:
    cache_key = f"{meta_type}:{imdb_id}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]

    url = f"https://v3-cinemeta.strem.io/meta/{meta_type}/{imdb_id}.json"
    logger.info(f"Fetching metadata from Cinemeta: {url}")
    
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("meta", {})
                if meta:
                    result = {
                        "name": meta.get("name"),
                        "year": meta.get("year"),
                        "genres": meta.get("genres", []),
                        "poster": meta.get("poster")
                    }
                    _metadata_cache[cache_key] = result
                    return result
    except Exception as e:
        logger.error(f"Cinemeta metadata lookup failed: {e}")
        
    return {}

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m4v')

def is_video_file(filename: str) -> bool:
    return filename.lower().endswith(VIDEO_EXTENSIONS)

def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = "".join(c for c in unicodedata.normalize('NFD', title) if unicodedata.category(c) != 'Mn')
    t = t.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\bii\b', '2', t)
    t = re.sub(r'\biii\b', '3', t)
    t = re.sub(r'\biv\b', '4', t)
    t = re.sub(r'\bv\b', '5', t)
    t = re.sub(r'\bvi\b', '6', t)
    t = re.sub(r'\bvii\b', '7', t)
    t = re.sub(r'\bviii\b', '8', t)
    t = re.sub(r'\bix\b', '9', t)
    t = re.sub(r'\bx\b', '10', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def _clean_title_prefix(filename: str) -> str:
    if not filename:
        return ""
    fn_lower = filename.lower()
    first_match_idx = len(filename)
    
    # Locate season/episode split point
    all_patterns = _SEASON_EPISODE_PATTERNS + _EPISODE_SEASON_PATTERNS + _STANDALONE_EPISODE_PATTERNS
    for pat in all_patterns:
        m = pat.search(fn_lower)
        if m:
            first_match_idx = min(first_match_idx, m.start())
            
    # Locate year split point
    year_match = re.search(r'\b(19\d{2}|20[0-2]\d)\b', filename)
    if year_match:
        first_match_idx = min(first_match_idx, year_match.start())
        
    prefix = filename[:first_match_idx]
    return prefix.strip()

def matches_title(filename: str, title: str) -> bool:
    if not title:
        return True
        
    norm_title = normalize_title(title)
    prefix = _clean_title_prefix(filename)
    norm_prefix = normalize_title(prefix)
    
    if not norm_prefix:
        norm_prefix = normalize_title(filename)
        
    if norm_title in norm_prefix:
        return True
        
    # Check if all major words of the title are in the prefix
    words = [w for w in norm_title.split() if w not in ('a', 'an', 'the', 'and', 'or', 'of', 'in', 'to', 'for', 'with')]
    if not words:
        words = norm_title.split()
        
    return all(word in norm_prefix for word in words)


