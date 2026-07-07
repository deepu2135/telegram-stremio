"""
Ported from ARVIO's TelegramSearchMatcher.
Provides matching and scoring logic for Telegram search results.
"""
import re
import unicodedata
import math
from typing import List, Optional, Tuple

SCORE_THRESHOLD = 55

SEP = r'[\s._\-x+,&:]{0,2}'
SEP_MID = r'[\s._\-x+,&:]{0,4}'

# ARVIO English/Hebrew + Spanish T01C01 + Standalone EP01/Cap01 patterns
EPISODE_PATTERN = re.compile(
    rf'[Ss][e]?(?:ason)?{SEP}(\d{{1,2}}){SEP_MID}[Ee][p]?(?:isode)?{SEP}(\d{{1,4}})' +
    rf'|ע(?:ונה)?{SEP}(\d{{1,2}}){SEP_MID}פ(?:רק)?{SEP}(\d{{1,4}})' +
    rf'|[Tt](?:emporada)?{SEP}(\d{{1,2}}){SEP_MID}[Cc](?:apitulo|apítulo)?{SEP}(\d{{1,4}})',
    re.IGNORECASE
)

EPISODE_ONLY_PATTERN = re.compile(
    rf'פ(?:רק)?{SEP}(\d{{1,4}})' +
    rf'|[Ee][p]?(?:isode)?{SEP}(\d{{1,4}})' +
    rf'|[Cc]ap(?:itulo|ítulo)?{SEP}(\d{{1,4}})',
    re.IGNORECASE
)

YEAR_PATTERN = re.compile(r'\b(?:19|20)\d{2}\b')
NOISE = re.compile(r'[._\-\[\]()\'",!?:]')
MULTI_SPACE = re.compile(r'\s+')
SIZE_SUFFIX = re.compile(r'\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts)$', re.IGNORECASE)

def is_hebrew(s: str) -> bool:
    return any(0x0590 <= ord(c) <= 0x05FF for c in s)

def clean_title(title: str) -> str:
    stripped = title.replace(":", "").replace("  ", " ").strip()
    # NFKD and remove Mn category (diacritics)
    normalized = "".join(c for c in unicodedata.normalize('NFKD', stripped) if unicodedata.category(c) != 'Mn')
    return normalized

def normalize(text: str) -> str:
    t = SIZE_SUFFIX.sub("", text)
    t = NOISE.sub(" ", t)
    t = MULTI_SPACE.sub(" ", t)
    return t.strip().lower()

class TelegramSearchMatcher:
    def score(
        self,
        file_name: str,
        caption: str,
        title: str,
        localized_title: Optional[str] = None,
        english_title: Optional[str] = None,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> int:
        caption = caption or ""
        file_name = file_name or ""
        combined = f"{file_name} {caption}"
        normalized_combined = normalize(combined)
        normalized_title = normalize(title)
        normalized_localized = normalize(localized_title) if localized_title else None
        normalized_english = normalize(english_title) if english_title else None

        eng_match = bool(normalized_english and normalized_english.strip() and normalized_english in normalized_combined)
        loc_match = bool(normalized_localized and normalized_localized.strip() and normalized_localized in normalized_combined)
        app_match = normalized_title in normalized_combined
        
        if not app_match:
            # Fallback to word-by-word match since we don't have TMDB aliases
            title_words = normalized_title.split()
            combined_words = normalized_combined.split()
            if title_words and all(w in combined_words for w in title_words):
                app_match = True

        if not eng_match and not loc_match and not app_match:
            return 0

        score = 60

        if year is not None:
            file_years = [int(m) for m in YEAR_PATTERN.findall(combined)]
            if year in file_years:
                score += 20
            elif any(abs(y - year) == 1 for y in file_years):
                score += 5
            elif not file_years:
                score += 5
            else:
                score -= 10

        if season is not None and episode is not None:
            se_file = self._extract_season_episode(file_name)
            se_caption = self._extract_season_episode(caption)
            right_se = (se_file and se_file[0] == season and se_file[1] == episode) or \
                       (se_caption and se_caption[0] == season and se_caption[1] == episode)

            if right_se:
                score += 20
            elif se_file or se_caption:
                return 0
            elif season == 1:
                ep_file = self._extract_episode_only(file_name)
                ep_caption = self._extract_episode_only(caption)
                if ep_file == episode or ep_caption == episode:
                    score += 20
                elif ep_file is not None or ep_caption is not None:
                    return 0
                else:
                    score -= 10
            else:
                score -= 10
        elif season is None:
            if EPISODE_PATTERN.search(combined) or EPISODE_PATTERN.search(normalized_combined):
                score -= 20

        return max(0, min(100, score))

    def _extract_season_episode(self, text: str) -> Optional[Tuple[int, int]]:
        m = EPISODE_PATTERN.search(text) or EPISODE_PATTERN.search(normalize(text))
        if not m:
            return None
        
        # Match groups: 
        # (1, 2) - English S/E
        # (3, 4) - Hebrew S/E
        # (5, 6) - Spanish S/E
        groups = m.groups()
        s = groups[0] or groups[2] or groups[4]
        e = groups[1] or groups[3] or groups[5]
        
        if s is not None and e is not None:
            try:
                return int(s), int(e)
            except ValueError:
                pass
        return None

    def _extract_episode_only(self, text: str) -> Optional[int]:
        m = EPISODE_ONLY_PATTERN.search(text) or EPISODE_ONLY_PATTERN.search(normalize(text))
        if not m:
            return None
        groups = m.groups()
        e = groups[0] or groups[1] or groups[2]
        if e is not None:
            try:
                return int(e)
            except ValueError:
                pass
        return None

    def build_movie_queries(
        self, 
        title: str, 
        year: Optional[int] = None, 
        localized_title: Optional[str] = None, 
        english_title: Optional[str] = None
    ) -> List[str]:
        primary = clean_title(english_title) if english_title else clean_title(title)
        localized = clean_title(localized_title) if localized_title else None
        
        queries = []
        if year is not None:
            queries.append(f"{primary} {year}")
        queries.append(primary)
        
        if localized and localized.lower() != primary.lower():
            if year is not None:
                queries.append(f"{localized} {year}")
            queries.append(localized)
            
        # Deduplicate while preserving order
        seen = set()
        return [q for q in queries if not (q in seen or seen.add(q))]

    def build_series_queries(
        self,
        title: str,
        season: int,
        episode: int,
        localized_title: Optional[str] = None,
        english_title: Optional[str] = None,
        language_code: str = "en"
    ) -> List[str]:
        eng_base = clean_title(english_title) if english_title else clean_title(title)
        loc_base = clean_title(localized_title) if localized_title else None
        titles_are_same = loc_base is None or loc_base.lower() == eng_base.lower()
        
        s = str(season)
        e = str(episode)
        s2 = str(season).zfill(2)
        e2 = str(episode).zfill(2)
        
        queries = []
        
        if language_code == "he":
            heb_title = eng_base if titles_are_same else (loc_base or eng_base)
            queries.extend([
                f"{heb_title} ע{s} פ{e}",
                f"{heb_title} ע{s}פ{e}",
                f"{heb_title} עונה {s} פרק {e}"
            ])
            if season == 1:
                queries.extend([f"{heb_title} פ{e}", f"{heb_title} פרק {e}"])
                
        if not titles_are_same and loc_base:
            queries.extend([
                f"{loc_base} s{s}e{e}",
                f"{loc_base} s{s2}e{e2}",
                f"{loc_base} s{s} e{e}",
                f"{loc_base} s{s2} e{e2}"
            ])
            
        queries.extend([
            f"{eng_base} s{s}e{e}",
            f"{eng_base} s{s2}e{e2}",
            f"{eng_base} s{s} e{e}",
            f"{eng_base} s{s2} e{e2}"
        ])
        
        seen = set()
        return [q.lower() for q in queries if not (q.lower() in seen or seen.add(q.lower()))]

def parse_quality(raw: str) -> str:
    t = raw.lower().replace(' ', '.')
    if any(x in t for x in ["dvdscr", "screener", ".scr."]):
        return "SCR"
    if any(x in t for x in [".cam.", "camrip", "hdcam", "hdts", "telesync"]):
        return "CAM"
    if any(x in t for x in ["360", "36o"]):
        return "360p"
    if any(x in t for x in ["480", "48o"]):
        return "480p"
    if any(x in t for x in ["720", "72o"]):
        return "720p"
    if any(x in t for x in ["1080", "1o8o", "108o", "1o80", ".fhd."]):
        return "1080p"
    if any(x in t for x in ["2160", "216o", ".4k.", ".uhd.", "ultrahd"]):
        return "4K"
    return "Unknown"

def quality_tier(quality: str) -> int:
    quality = quality.upper()
    if quality == "4K": return 6
    if quality == "1080P": return 5
    if quality == "720P": return 4
    if quality == "480P": return 3
    if quality == "360P": return 2
    if quality == "CAM": return -1
    if quality == "SCR": return -1
    return 0
