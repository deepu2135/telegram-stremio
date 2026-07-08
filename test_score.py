import sys
from search_matcher import TelegramSearchMatcher

matcher = TelegramSearchMatcher()
filename = "Project.Hail.Mary.2026.480p.WEB-DL.IMAX.HIN-ENG.x264.AAC.2.0.ESub-SkymoviesHD.mkv"
title = "Project Hail Mary"
year = 2026

score = matcher.score(
    file_name=filename,
    caption="",
    title=title,
    year=year,
    season=None,
    episode=None
)

print(f"Score for {filename}: {score}")
