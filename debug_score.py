import sys
import re
from search_matcher import TelegramSearchMatcher, normalize, YEAR_PATTERN, EPISODE_PATTERN, EPISODE_ONLY_PATTERN

filename = "Project.Hail.Mary.2026.480p.WEB-DL.IMAX.HIN-ENG.x264.AAC.2.0.ESub-SkymoviesHD.mkv"
title = "Project Hail Mary"
year = 2026
combined = filename
normalized_combined = normalize(combined)
normalized_title = normalize(title)

print(f"normalized_combined: {normalized_combined}")

# Title match
app_match = normalized_title in normalized_combined
title_words = normalized_title.split()
combined_words = normalized_combined.split()
if not app_match and title_words and all(w in combined_words for w in title_words):
    app_match = True

print(f"app_match: {app_match}")

score = 60
print(f"Base score: {score}")

if year is not None:
    file_years = [int(m.group()) for m in YEAR_PATTERN.finditer(combined)]
    print(f"file_years: {file_years}")
    if year in file_years:
        score += 20
        print("+20 for year match")
    elif any(abs(y - year) == 1 for y in file_years):
        score += 5
        print("+5 for off-by-one year")
    elif not file_years:
        score += 5
        print("+5 for no year")
    else:
        score -= 10
        print("-10 for wrong year")

if EPISODE_PATTERN.search(combined):
    print("-20 for EPISODE_PATTERN in combined:", EPISODE_PATTERN.search(combined).group())
    score -= 20
elif EPISODE_PATTERN.search(normalized_combined):
    print("-20 for EPISODE_PATTERN in normalized_combined:", EPISODE_PATTERN.search(normalized_combined).group())
    score -= 20

print(f"Final score: {score}")
