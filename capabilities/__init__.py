"""
capabilities/__init__.py
Shared cross-mode capability registry.
Every deployment mode (gaming, realtor, counselling, or newly synthesized modes)
automatically inherits:
  - Multi-Site Music & Video Conduit (YouTube, Dailymotion, SoundCloud, Audius, Archive.org, MP3 & MP4)
  - 4-Method Zero-Quota Web Research Suite
  - Cloudflare R2 Dated Storage Manager (YYYY/MM/DD)
  - Aurora Self-Upgrade & GitHub REST/GraphQL/Actions CI/CD Core
"""

from capabilities.music_conduit import (
    MusicConduit,
    AUDIO_OWNERSHIP_NOTICE,
    is_song_request,
    extract_song_queries,
    search_songs_hybrid,
    convert_track_to_media_bytes,
    convert_track_to_mp3_bytes,
    convert_audio_url_to_mp3_bytes,
    convert_m4a_url_to_mp3_bytes,
    extract_and_convert_audio,
)

from capabilities.web_research import (
    WebResearchSuite,
    SEARCH_TRIGGERS,
)

from capabilities.r2_storage import CloudflareR2Manager
from capabilities.self_upgrade import (
    GitHubGraphQLAndRestClient,
    AuroraSelfUpgradeEngine,
)

__all__ = [
    "MusicConduit",
    "AUDIO_OWNERSHIP_NOTICE",
    "is_song_request",
    "extract_song_queries",
    "search_songs_hybrid",
    "convert_track_to_media_bytes",
    "convert_track_to_mp3_bytes",
    "convert_audio_url_to_mp3_bytes",
    "convert_m4a_url_to_mp3_bytes",
    "extract_and_convert_audio",
    "WebResearchSuite",
    "SEARCH_TRIGGERS",
    "CloudflareR2Manager",
    "GitHubGraphQLAndRestClient",
    "AuroraSelfUpgradeEngine",
]
