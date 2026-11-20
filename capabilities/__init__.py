"""
capabilities/__init__.py
Shared cross-mode capability registry.
Every deployment mode (gaming, realtor, or custom modes) automatically inherits
the YouTube/Hybrid Music Conduit and the 4-method Zero-Quota Web Research Suite.
"""

from capabilities.music_conduit import (
    MusicConduit,
    AUDIO_OWNERSHIP_NOTICE,
    is_song_request,
    extract_song_queries,
    search_songs_hybrid,
    convert_audio_url_to_mp3_bytes,
    convert_m4a_url_to_mp3_bytes,
    convert_track_to_mp3_bytes,
    extract_and_convert_audio,
)

from capabilities.web_research import (
    WebResearchSuite,
    SEARCH_TRIGGERS,
)

__all__ = [
    "MusicConduit",
    "AUDIO_OWNERSHIP_NOTICE",
    "is_song_request",
    "extract_song_queries",
    "search_songs_hybrid",
    "convert_audio_url_to_mp3_bytes",
    "convert_m4a_url_to_mp3_bytes",
    "convert_track_to_mp3_bytes",
    "extract_and_convert_audio",
    "WebResearchSuite",
    "SEARCH_TRIGGERS",
]
