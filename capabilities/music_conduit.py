"""
capabilities/music_conduit.py
Shared Music & Acoustic Extraction Capability available to ALL modes.
Integrates yt-dlp + FFmpeg (imageio-ffmpeg) for YouTube URL & song search MP3 extraction,
alongside a 5-tier hybrid lookup (Local Media Vault -> YouTube via yt-dlp -> Audius ->
Internet Archive -> iTunes 30s Preview Fallback) with HTTP reachability validation
and in-memory MP3 transcoding.
"""

import os
import re
import shutil
import tempfile
import subprocess
import urllib.parse
from typing import List, Tuple, Optional, Dict, Any
import requests

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


SONG_EXPLICIT_TRIGGERS = [
    "mp3", "m4a", "song", "music", "track", "audio", "album",
    "listen to", "download song", "sing", "youtube.com", "youtu.be", "music.youtube.com"
]

GAME_NON_MUSIC_WORDS = [
    "koa", "king of avalon", "kvk", "rally", "frost drake", "dragon", "troop", "march",
    "fae", "sanctum", "alliance", "excalibur", "spire", "emblem", "hero", "golem"
]

AUDIO_OWNERSHIP_NOTICE = (
    "\n\n---\n"
    "*⚖️ Audio Notice: Streams and MP3 conversions are provided for personal listening of "
    "public-domain, Creative Commons, or personally owned/licensed recordings. Please ensure you "
    "own or hold valid rights to a track before downloading.*"
)

_CACHED_AUDIUS_HOSTS: List[str] = []


def _get_ffmpeg_executable() -> Optional[str]:
    """Resolves the bundled imageio-ffmpeg binary or system ffmpeg executable."""
    ffmpeg_exe = None
    if imageio_ffmpeg is not None:
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = None
    if not ffmpeg_exe:
        ffmpeg_exe = shutil.which("ffmpeg")
    return ffmpeg_exe


def _extract_youtube_url(text: str) -> Optional[str]:
    """Extracts a direct YouTube / YouTube Music URL from user text if present."""
    match = re.search(
        r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com/watch\?v=[\w-]+[^\s]*|youtu\.be/[\w-]+[^\s]*|youtube\.com/shorts/[\w-]+[^\s]*))",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def is_song_request(text: str) -> bool:
    """Detects whether the user is requesting a song/audio track or sharing a YouTube music link."""
    clean = text.lower().strip()
    if _extract_youtube_url(text):
        return True
    if any(g in clean for g in GAME_NON_MUSIC_WORDS) and not any(
        k in clean for k in ("mp3", "m4a", "song", "music", "track", "audio", "youtube")
    ):
        return False
    if any(k in clean for k in SONG_EXPLICIT_TRIGGERS):
        return True
    if re.match(r"^(play|download|find)\s+.+\s+by\s+.+", clean):
        return True
    return False


def extract_song_queries(text: str) -> Tuple[str, str]:
    """
    Returns (primary_query, compact_query) with conversational filler removed.
    Example: 'Play Blue (Da Ba Dee) by Eiffel 65' -> ('Blue Da Ba Dee Eiffel 65', 'Blue Eiffel 65')
    """
    yt_url = _extract_youtube_url(text)
    if yt_url:
        return yt_url, yt_url

    no_brackets = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)

    def _clean(raw: str) -> str:
        s = re.sub(
            r"\b(hey|hi|hello|aurora|aria|please|can you|could you|give me|get me|find|search|look up|lookup|download|direct|link|links|to|for|a|an|the|song|music|track|audio|mp3|m4a|file|files|like|mp3juice|play|listen|full|by|feat|ft|featuring|official|video|lyrics|radio|edit|version|youtube)\b",
            " ",
            raw,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"[^\w\s&'-]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    primary = _clean(text) or text.strip()
    compact = _clean(no_brackets) or primary
    return primary, compact


def _format_duration(seconds: Any) -> str:
    try:
        if isinstance(seconds, str) and ":" in seconds:
            return seconds.strip()
        sec = int(float(seconds or 0))
        if sec <= 0:
            return ""
        mins, rem = divmod(sec, 60)
        return f"{mins}:{rem:02d}"
    except Exception:
        return ""


def _get_audius_hosts() -> List[str]:
    global _CACHED_AUDIUS_HOSTS
    if _CACHED_AUDIUS_HOSTS:
        return _CACHED_AUDIUS_HOSTS
    try:
        r = requests.get("https://api.audius.co", timeout=4)
        if r.status_code == 200:
            hosts = [h.rstrip("/") for h in r.json().get("data", []) if isinstance(h, str)]
            if hosts:
                _CACHED_AUDIUS_HOSTS = hosts[:3]
                return _CACHED_AUDIUS_HOSTS
    except Exception:
        pass
    return ["https://api.audius.co"]


def _is_stream_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        r = requests.head(url, allow_redirects=True, timeout=4)
        if r.status_code in (200, 206):
            return True
        r_get = requests.get(url, headers={"Range": "bytes=0-1"}, stream=True, timeout=5)
        r_get.close()
        return r_get.status_code in (200, 206)
    except Exception:
        return False


def _score_track_relevance(track: Dict[str, Any], query_tokens: List[str]) -> int:
    if track.get("is_direct_url"):
        return 1000
    if not query_tokens:
        return 0
    haystack = f"{track.get('title', '')} {track.get('artist', '')}".lower()
    matched = sum(1 for tok in query_tokens if tok in haystack)
    bonus = 4 if not track.get("is_preview", True) else 0
    return (matched * 10) + bonus


class MusicConduit:
    """
    Unified Acoustic Extraction & MP3 Transcoding Engine available to all modes.
    Supports YouTube extraction via yt-dlp + FFmpeg as well as multi-tier open catalog search.
    """

    def __init__(self, output_directory: str = "./media_vault"):
        self.output_directory = output_directory
        os.makedirs(self.output_directory, exist_ok=True)
        self.ffmpeg_exe = _get_ffmpeg_executable()

    def extract_and_convert_audio(self, youtube_url: str, bitrate: str = "192") -> Optional[str]:
        """
        Extracts audio from a YouTube URL or search string and saves a high-quality MP3 to disk.
        """
        if yt_dlp is None:
            print("[TACTICAL ERROR]: yt-dlp is not installed.")
            return None

        target = youtube_url.strip()
        if not re.match(r"^https?://", target, re.IGNORECASE):
            target = f"ytsearch1:{target}"

        ydl_opts: Dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(self.output_directory, "%(title)s.%(ext)s"),
            "restrictfilenames": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }
            ],
        }
        if self.ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_exe

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                filename = ydl.prepare_filename(info)
                final_file = os.path.splitext(filename)[0] + ".mp3"
                return final_file if os.path.exists(final_file) else None
        except Exception as e:
            print(f"[TACTICAL ERROR]: Stream extraction failed: {e}")
            return None

    def search_youtube_tracks(self, search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
        """
        Uses yt-dlp metadata extraction (without pre-downloading) to resolve YouTube URLs
        or search queries into playable stream metadata ready for 1-click MP3 transcoding.
        """
        if yt_dlp is None:
            return []

        direct_url = _extract_youtube_url(search_term)
        query_target = direct_url if direct_url else f"ytsearch{limit}:{search_term}"

        ydl_opts: Dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        if self.ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_exe

        tracks: List[Dict[str, Any]] = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query_target, download=False)
                entries = info.get("entries") if isinstance(info, dict) and "entries" in info else [info]
                for entry in (entries or []):
                    if not entry:
                        continue
                    vid = entry.get("id", str(len(tracks)))
                    title = entry.get("title", "YouTube Audio Track")
                    uploader = entry.get("uploader") or entry.get("channel") or "YouTube"
                    webpage_url = entry.get("webpage_url") or direct_url or f"https://www.youtube.com/watch?v={vid}"
                    stream_url = entry.get("url") or webpage_url
                    thumbnail = entry.get("thumbnail") or ""
                    duration_sec = entry.get("duration", 0)
                    safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{uploader} - {title}").strip() or "youtube_track"

                    tracks.append({
                        "track_id": f"yt_{vid}",
                        "title": title,
                        "artist": uploader,
                        "album": "YouTube Audio Conduit",
                        "artwork": thumbnail,
                        "audio_url": stream_url,
                        "webpage_url": webpage_url,
                        "m4a_url": stream_url,
                        "mp3_filename": f"{safe_name}.mp3",
                        "is_preview": False,
                        "is_direct_url": bool(direct_url),
                        "extractor": "ytdlp",
                        "duration_str": _format_duration(duration_sec),
                        "source_label": "Full Track (YouTube)",
                    })
                    if len(tracks) >= limit:
                        break
        except Exception as e:
            print(f"YouTube yt-dlp metadata notice: {e}")
        return tracks

    def convert_track_to_mp3_bytes(
        self,
        track_or_url: Any,
        bitrate: str = "192k",
        max_bytes: Optional[int] = None,
    ) -> bytes:
        """
        Converts either a track dictionary (YouTube yt-dlp or direct HTTP stream)
        or a raw audio/YouTube URL into playable .mp3 bytes via FFmpeg.
        """
        ffmpeg_exe = self.ffmpeg_exe or _get_ffmpeg_executable()
        if not ffmpeg_exe:
            raise RuntimeError("FFmpeg binary not available. Ensure 'imageio-ffmpeg' is in requirements.txt.")

        if isinstance(track_or_url, dict):
            extractor = track_or_url.get("extractor", "http")
            target_url = (
                track_or_url.get("webpage_url")
                if extractor == "ytdlp"
                else (track_or_url.get("audio_url") or track_or_url.get("m4a_url", ""))
            )
        else:
            target_url = str(track_or_url).strip()
            extractor = "ytdlp" if _extract_youtube_url(target_url) else "http"

        if not target_url:
            raise ValueError("No valid audio or YouTube URL provided for MP3 conversion.")

        # Path A: YouTube source via yt-dlp + FFmpeg postprocessor into temp directory
        if extractor == "ytdlp" and yt_dlp is not None:
            numeric_kbps = re.sub(r"[^\d]", "", str(bitrate)) or "192"
            with tempfile.TemporaryDirectory() as tmp_dir:
                ydl_opts: Dict[str, Any] = {
                    "format": "bestaudio/best",
                    "ffmpeg_location": ffmpeg_exe,
                    "outtmpl": os.path.join(tmp_dir, "audio_export.%(ext)s"),
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": numeric_kbps,
                        }
                    ],
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(target_url, download=True)

                mp3_path = os.path.join(tmp_dir, "audio_export.mp3")
                if not os.path.exists(mp3_path):
                    raise RuntimeError("yt-dlp completed but MP3 output file was not produced.")
                with open(mp3_path, "rb") as f:
                    mp3_bytes = f.read()

                if max_bytes and len(mp3_bytes) > max_bytes and numeric_kbps != "96":
                    return self.convert_track_to_mp3_bytes(track_or_url, bitrate="96k", max_bytes=None)
                return mp3_bytes

        # Path B: Direct HTTP stream (Audius / Internet Archive / Vault / iTunes) via FFmpeg pipe
        audio_resp = requests.get(target_url, allow_redirects=True, timeout=30)
        audio_resp.raise_for_status()

        def _encode(target_bitrate: str) -> bytes:
            cmd = [
                ffmpeg_exe,
                "-y",
                "-i", "pipe:0",
                "-vn",
                "-acodec", "libmp3lame",
                "-b:a", target_bitrate,
                "-f", "mp3",
                "pipe:1",
            ]
            proc = subprocess.run(
                cmd,
                input=audio_resp.content,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return proc.stdout

        mp3_bytes = _encode(bitrate)
        if max_bytes and len(mp3_bytes) > max_bytes:
            mp3_bytes = _encode("96k")
        return mp3_bytes


def _search_audius_full_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    for host in _get_audius_hosts():
        try:
            resp = requests.get(
                f"{host}/v1/tracks/search",
                params={"query": search_term, "app_name": "AuroraKoA"},
                timeout=6,
            )
            if resp.status_code != 200:
                continue

            tracks: List[Dict[str, Any]] = []
            for item in resp.json().get("data", []):
                tid = item.get("id")
                if not tid or item.get("is_streamable") is False:
                    continue
                title = item.get("title", "Unknown Title")
                user_obj = item.get("user") or {}
                artist = user_obj.get("name", "Unknown Artist")
                artwork_obj = item.get("artwork") or {}
                artwork_url = artwork_obj.get("150x150") or artwork_obj.get("480x480") or ""
                duration_sec = item.get("duration", 0)
                stream_url = f"{host}/v1/tracks/{tid}/stream?app_name=AuroraKoA"
                safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}").strip() or "aurora_track"

                tracks.append({
                    "track_id": f"audius_{tid}",
                    "title": title,
                    "artist": artist,
                    "album": item.get("genre") or "Audius Open Stream",
                    "artwork": artwork_url,
                    "audio_url": stream_url,
                    "webpage_url": stream_url,
                    "m4a_url": stream_url,
                    "mp3_filename": f"{safe_name}.mp3",
                    "is_preview": False,
                    "extractor": "http",
                    "duration_str": _format_duration(duration_sec),
                    "source_label": "Full Track (Audius)",
                })
                if len(tracks) >= limit * 2:
                    break
            if tracks:
                return tracks
        except Exception:
            continue
    return []


def _search_internet_archive_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    try:
        search_resp = requests.get(
            "https://archive.org/advancedsearch.php",
            params={
                "q": f"({search_term}) AND mediatype:(audio)",
                "fl[]": ["identifier", "title", "creator"],
                "sort[]": "downloads desc",
                "rows": 4,
                "page": 1,
                "output": "json",
            },
            timeout=7,
        )
        if search_resp.status_code != 200:
            return []

        docs = search_resp.json().get("response", {}).get("docs", [])
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue

            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", timeout=6)
            if meta_resp.status_code != 200:
                continue

            meta_json = meta_resp.json()
            item_meta = meta_json.get("metadata") or {}
            files = meta_json.get("files") or []

            mp3_files = [
                f for f in files
                if str(f.get("name", "")).lower().endswith(".mp3")
                and "_64kb" not in str(f.get("name", "")).lower()
            ]
            if not mp3_files:
                continue

            chosen = mp3_files[0]
            fname = chosen["name"]
            encoded_fname = urllib.parse.quote(fname)
            direct_mp3_url = f"https://archive.org/download/{identifier}/{encoded_fname}"

            raw_creator = doc.get("creator") or item_meta.get("creator") or "Internet Archive"
            if isinstance(raw_creator, list):
                raw_creator = ", ".join(str(c) for c in raw_creator[:2])

            raw_title = chosen.get("title") or doc.get("title") or fname.rsplit(".", 1)[0]
            if isinstance(raw_title, list):
                raw_title = raw_title[0]

            safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{raw_creator} - {raw_title}").strip() or "archive_track"
            tracks.append({
                "track_id": f"ia_{identifier}_{len(tracks)}",
                "title": str(raw_title),
                "artist": str(raw_creator),
                "album": "Internet Archive Audio",
                "artwork": f"https://archive.org/services/img/{identifier}",
                "audio_url": direct_mp3_url,
                "webpage_url": direct_mp3_url,
                "m4a_url": direct_mp3_url,
                "mp3_filename": f"{safe_name}.mp3",
                "is_preview": False,
                "extractor": "http",
                "duration_str": _format_duration(chosen.get("length")),
                "source_label": "Full Track (Internet Archive)",
            })
            if len(tracks) >= limit:
                break
    except Exception as e:
        print(f"Internet Archive search notice: {e}")
    return tracks


def _search_itunes_preview_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    resp = requests.get(
        "https://itunes.apple.com/search",
        params={"term": search_term, "media": "music", "entity": "song", "limit": limit * 2},
        timeout=8,
    )
    resp.raise_for_status()

    tracks: List[Dict[str, Any]] = []
    for item in resp.json().get("results", []):
        preview_url = item.get("previewUrl")
        if not preview_url:
            continue
        artist = item.get("artistName", "Unknown Artist")
        title = item.get("trackName", "Unknown Title")
        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}").strip() or "aurora_preview"
        tracks.append({
            "track_id": f"itunes_{item.get('trackId', len(tracks))}",
            "title": title,
            "artist": artist,
            "album": item.get("collectionName", ""),
            "artwork": item.get("artworkUrl100", ""),
            "audio_url": preview_url,
            "webpage_url": preview_url,
            "m4a_url": preview_url,
            "mp3_filename": f"{safe_name}.mp3",
            "is_preview": True,
            "extractor": "http",
            "duration_str": "0:30",
            "source_label": "30s Preview",
        })
    return tracks


_DEFAULT_CONDUIT = MusicConduit()


def search_songs_hybrid(
    query: str,
    limit: int = 3,
    local_media: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    5-Tier Song Discovery with Relevance Ranking & HTTP Reachability Filtering:
    1) Local SQLite media_vault direct audio or YouTube URLs.
    2) YouTube URL / search via yt-dlp + FFmpeg conduit.
    3) Audius Open Music API full-length tracks.
    4) Internet Archive (archive.org) full-length open/public-domain MP3s.
    5) iTunes Search API 30-second previews fallback.
    """
    primary_query, compact_query = extract_song_queries(query)
    if not primary_query:
        return []

    query_tokens = [w.lower() for w in re.findall(r"\w+", primary_query) if len(w) >= 2]
    candidates: List[Dict[str, Any]] = []
    seen_ids = set()

    def _add_unique(items: List[Dict[str, Any]]) -> None:
        for t in items:
            tid = t.get("track_id")
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                candidates.append(t)

    # Tier 1: Local Media Vault
    if local_media:
        term_lower = compact_query.lower()
        for item in local_media:
            url = str(item.get("url", ""))
            title = str(item.get("title", ""))
            desc = str(item.get("description", ""))
            if term_lower in title.lower() or term_lower in desc.lower():
                if _extract_youtube_url(url):
                    _add_unique(_DEFAULT_CONDUIT.search_youtube_tracks(url, limit=1))
                elif any(ext in url.lower() for ext in (".mp3", ".m4a", ".wav", ".ogg")):
                    safe_name = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "vault_track"
                    _add_unique([{
                        "track_id": f"vault_{item.get('id', len(candidates))}",
                        "title": title,
                        "artist": item.get("added_by", "The Broken Star"),
                        "album": item.get("category", "Vault Archive"),
                        "artwork": "",
                        "audio_url": url,
                        "webpage_url": url,
                        "m4a_url": url,
                        "mp3_filename": f"{safe_name}.mp3",
                        "is_preview": False,
                        "extractor": "http",
                        "duration_str": "Full",
                        "source_label": "Vault Full Track",
                    }])

    # Tier 2: YouTube via yt-dlp
    _add_unique(_DEFAULT_CONDUIT.search_youtube_tracks(primary_query, limit=limit))

    # Tier 3: Audius full-length open catalog
    if len(candidates) < limit:
        _add_unique(_search_audius_full_tracks(primary_query, limit=limit))
        if compact_query.lower() != primary_query.lower() and len(candidates) < limit:
            _add_unique(_search_audius_full_tracks(compact_query, limit=limit))

    # Tier 4: Internet Archive full-length public domain / Creative Commons audio
    if len(candidates) < limit:
        _add_unique(_search_internet_archive_tracks(compact_query, limit=limit))

    # Tier 5: iTunes 30-second commercial preview fallback
    if len(candidates) < limit:
        try:
            _add_unique(_search_itunes_preview_tracks(primary_query, limit=limit))
            if compact_query.lower() != primary_query.lower():
                _add_unique(_search_itunes_preview_tracks(compact_query, limit=limit))
        except Exception as e:
            print(f"iTunes fallback search notice: {e}")

    candidates.sort(key=lambda t: _score_track_relevance(t, query_tokens), reverse=True)

    verified: List[Dict[str, Any]] = []
    for track in candidates[: limit + 4]:
        if track.get("extractor") == "ytdlp" or _is_stream_reachable(track.get("audio_url", "")):
            verified.append(track)
        if len(verified) >= limit:
            break

    return verified


def convert_track_to_mp3_bytes(
    track_or_url: Any,
    bitrate: str = "192k",
    max_bytes: Optional[int] = None,
) -> bytes:
    return _DEFAULT_CONDUIT.convert_track_to_mp3_bytes(track_or_url, bitrate=bitrate, max_bytes=max_bytes)


def convert_audio_url_to_mp3_bytes(
    audio_url: str,
    bitrate: str = "192k",
    max_bytes: Optional[int] = None,
) -> bytes:
    return _DEFAULT_CONDUIT.convert_track_to_mp3_bytes(audio_url, bitrate=bitrate, max_bytes=max_bytes)


convert_m4a_url_to_mp3_bytes = convert_audio_url_to_mp3_bytes


def extract_and_convert_audio(youtube_url: str, output_directory: str = "./media_vault") -> Optional[str]:
    conduit = MusicConduit(output_directory=output_directory)
    return conduit.extract_and_convert_audio(youtube_url)
