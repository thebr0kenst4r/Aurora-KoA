"""
capabilities/music_conduit.py
Shared Multi-Site Media, Video & Acoustic Extraction Capability available to ALL modes.
Fixes HTTP 403 errors via multi-client yt-dlp profile rotation, browser-grade HTTP headers,
and multi-source search across:
  1. Local Media Vault (Sovereign curated links)
  2. YouTube (yt-dlp multi-client + official embed URLs + MP3 & MP4 downloads)
  3. Dailymotion (Public REST API + yt-dlp + official embed URLs + MP3 & MP4 downloads)
  4. SoundCloud (yt-dlp full-length tracks + MP3 downloads)
  5. Audius Open Music Network (Full-length tracks + MP3 downloads)
  6. Internet Archive / archive.org (Full-length open audio & video + MP3/MP4 downloads)
  7. iTunes Search API (30-second commercial preview fallback)
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


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

SONG_EXPLICIT_TRIGGERS = [
    "mp3", "mp4", "m4a", "song", "music", "track", "audio", "video", "album",
    "listen to", "download song", "download video", "sing",
    "youtube.com", "youtu.be", "music.youtube.com", "dailymotion.com", "dai.ly",
    "soundcloud.com", "vimeo.com"
]

GAME_NON_MUSIC_WORDS = [
    "koa", "king of avalon", "kvk", "rally", "frost drake", "dragon", "troop", "march",
    "fae", "sanctum", "alliance", "excalibur", "spire", "emblem", "hero", "golem"
]

AUDIO_OWNERSHIP_NOTICE = (
    "\n\n---\n"
    "*⚖️ Media & Audio Notice: Streams, MP3 audio, and MP4 video conversions are provided for personal "
    "use of public-domain, Creative Commons, or personally owned/licensed recordings. Please ensure you "
    "own or hold valid rights to a recording before downloading.*"
)

_CACHED_AUDIUS_HOSTS: List[str] = []


def _get_ffmpeg_executable() -> Optional[str]:
    ffmpeg_exe = None
    if imageio_ffmpeg is not None:
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = None
    if not ffmpeg_exe:
        ffmpeg_exe = shutil.which("ffmpeg")
    return ffmpeg_exe


def _extract_direct_media_url(text: str) -> Optional[str]:
    match = re.search(
        r"(https?://(?:www\.|m\.|music\.)?(?:"
        r"youtube\.com/watch\?v=[\w-]+[^\s]*|"
        r"youtu\.be/[\w-]+[^\s]*|"
        r"youtube\.com/shorts/[\w-]+[^\s]*|"
        r"dailymotion\.com/video/[\w-]+[^\s]*|"
        r"dai\.ly/[\w-]+[^\s]*|"
        r"vimeo\.com/\d+[^\s]*|"
        r"soundcloud\.com/[\w-]+/[\w-]+[^\s]*"
        r"))",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def is_song_request(text: str) -> bool:
    clean = text.lower().strip()
    if _extract_direct_media_url(text):
        return True
    if any(g in clean for g in GAME_NON_MUSIC_WORDS) and not any(
        k in clean for k in ("mp3", "mp4", "m4a", "song", "music", "track", "audio", "video", "youtube", "dailymotion", "soundcloud")
    ):
        return False
    if any(k in clean for k in SONG_EXPLICIT_TRIGGERS):
        return True
    if re.match(r"^(play|download|find|watch)\s+.+\s+by\s+.+", clean):
        return True
    return False


def extract_song_queries(text: str) -> Tuple[str, str]:
    direct_url = _extract_direct_media_url(text)
    if direct_url:
        return direct_url, direct_url

    no_brackets = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)

    def _clean(raw: str) -> str:
        s = re.sub(
            r"\b(hey|hi|hello|aurora|aria|please|can you|could you|give me|get me|find|search|look up|lookup|download|direct|link|links|to|for|a|an|the|song|music|track|audio|video|mp3|mp4|m4a|file|files|like|mp3juice|play|watch|listen|full|by|feat|ft|featuring|official|lyrics|radio|edit|version|youtube|dailymotion|soundcloud)\b",
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
        r = requests.get("https://api.audius.co", headers=BROWSER_HEADERS, timeout=4)
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
        r = requests.get(url, headers={**BROWSER_HEADERS, "Range": "bytes=0-1"}, stream=True, allow_redirects=True, timeout=5)
        r.close()
        return r.status_code in (200, 206)
    except Exception:
        return False


def _score_track_relevance(track: Dict[str, Any], query_tokens: List[str]) -> int:
    if track.get("is_direct_url"):
        return 1000
    if not query_tokens:
        return 0
    haystack = f"{track.get('title', '')} {track.get('artist', '')}".lower()
    matched = sum(1 for tok in query_tokens if tok in haystack)
    bonus = 5 if not track.get("is_preview", True) else 0
    return (matched * 10) + bonus


class MusicConduit:
    """
    403-Resilient Multi-Site Video & Audio Conduit available to all modes.
    """

    def __init__(self, output_directory: str = "./media_vault", issue_logger: Any = None):
        self.output_directory = output_directory
        os.makedirs(self.output_directory, exist_ok=True)
        self.ffmpeg_exe = _get_ffmpeg_executable()
        self.issue_logger = issue_logger

    def _log_issue(self, summary: str, details: str) -> None:
        if callable(self.issue_logger):
            try:
                self.issue_logger("media_conduit", summary, details, "all")
            except Exception:
                pass

    def _build_base_ydl_opts(self, client_profile: Optional[List[str]] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "nocheckcertificate": True,
            "http_headers": BROWSER_HEADERS,
            "retries": 3,
            "fragment_retries": 3,
        }
        if client_profile:
            opts["extractor_args"] = {"youtube": {"player_client": client_profile}}
        if self.ffmpeg_exe:
            opts["ffmpeg_location"] = self.ffmpeg_exe
        return opts

    def extract_and_convert_audio(self, media_url: str, bitrate: str = "192") -> Optional[str]:
        if yt_dlp is None:
            return None
        target = media_url.strip()
        if not re.match(r"^https?://", target, re.IGNORECASE):
            target = f"ytsearch1:{target}"

        for profile in (["android_vr", "android"], ["ios", "web"], None):
            ydl_opts = self._build_base_ydl_opts(profile)
            ydl_opts.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(self.output_directory, "%(title)s.%(ext)s"),
                "restrictfilenames": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": bitrate,
                    }
                ],
            })
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target, download=True)
                    if "entries" in info and info["entries"]:
                        info = info["entries"][0]
                    filename = ydl.prepare_filename(info)
                    final_file = os.path.splitext(filename)[0] + ".mp3"
                    if os.path.exists(final_file):
                        return final_file
            except Exception as e:
                print(f"[MusicConduit Retry Notice]: {e}")
        return None

    def search_dailymotion_tracks(self, search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
        tracks: List[Dict[str, Any]] = []
        try:
            resp = requests.get(
                "https://api.dailymotion.com/videos",
                params={
                    "search": search_term,
                    "limit": limit,
                    "fields": "id,title,owner.screenname,duration,thumbnail_360_url,url",
                },
                headers=BROWSER_HEADERS,
                timeout=7,
            )
            if resp.status_code != 200:
                return []

            for item in resp.json().get("list", []):
                vid = item.get("id")
                if not vid:
                    continue
                title = item.get("title", "Dailymotion Video")
                uploader = item.get("owner.screenname") or "Dailymotion"
                page_url = item.get("url") or f"https://www.dailymotion.com/video/{vid}"
                thumb = item.get("thumbnail_360_url") or ""
                duration_sec = item.get("duration", 0)
                safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{uploader} - {title}").strip() or f"dailymotion_{vid}"

                tracks.append({
                    "track_id": f"dm_{vid}",
                    "title": title,
                    "artist": uploader,
                    "album": "Dailymotion Video & Audio",
                    "artwork": thumb,
                    "audio_url": page_url,
                    "webpage_url": page_url,
                    "m4a_url": page_url,
                    "mp3_filename": f"{safe_name}.mp3",
                    "mp4_filename": f"{safe_name}.mp4",
                    "is_preview": False,
                    "is_video": True,
                    "is_direct_url": False,
                    "extractor": "ytdlp",
                    "duration_str": _format_duration(duration_sec),
                    "source_label": "Full Video/Audio (Dailymotion)",
                })
        except Exception as e:
            print(f"Dailymotion search notice: {e}")
        return tracks

    def search_youtube_tracks(self, search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
        if yt_dlp is None:
            return []

        direct_url = _extract_direct_media_url(search_term)
        query_target = direct_url if direct_url else f"ytsearch{limit}:{search_term}"

        tracks: List[Dict[str, Any]] = []
        for profile in (["android_vr", "android"], ["ios", "web"], None):
            ydl_opts = self._build_base_ydl_opts(profile)
            ydl_opts["extract_flat"] = "in_playlist" if not direct_url else False
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(query_target, download=False)
                    entries = info.get("entries") if isinstance(info, dict) and "entries" in info else [info]
                    for entry in (entries or []):
                        if not entry:
                            continue
                        vid = entry.get("id", str(len(tracks)))
                        title = entry.get("title", "Video / Audio Track")
                        uploader = entry.get("uploader") or entry.get("channel") or "YouTube"
                        webpage_url = entry.get("webpage_url") or entry.get("url") or direct_url or f"https://www.youtube.com/watch?v={vid}"
                        if not str(webpage_url).startswith("http"):
                            webpage_url = f"https://www.youtube.com/watch?v={vid}"
                        thumbnail = entry.get("thumbnail") or ""
                        duration_sec = entry.get("duration", 0)
                        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{uploader} - {title}").strip() or f"media_{vid}"

                        is_vid_site = any(
                            host in webpage_url.lower()
                            for host in ("youtube.com", "youtu.be", "dailymotion.com", "dai.ly", "vimeo.com")
                        )
                        platform_label = (
                            "Dailymotion" if ("dailymotion.com" in webpage_url.lower() or "dai.ly" in webpage_url.lower())
                            else "SoundCloud" if "soundcloud.com" in webpage_url.lower()
                            else "Vimeo" if "vimeo.com" in webpage_url.lower()
                            else "YouTube"
                        )

                        tracks.append({
                            "track_id": f"ytdlp_{vid}",
                            "title": title,
                            "artist": uploader,
                            "album": f"{platform_label} Media Conduit",
                            "artwork": thumbnail,
                            "audio_url": webpage_url,
                            "webpage_url": webpage_url,
                            "m4a_url": webpage_url,
                            "mp3_filename": f"{safe_name}.mp3",
                            "mp4_filename": f"{safe_name}.mp4",
                            "is_preview": False,
                            "is_video": is_vid_site,
                            "is_direct_url": bool(direct_url),
                            "extractor": "ytdlp",
                            "duration_str": _format_duration(duration_sec),
                            "source_label": f"Full {'Video/Audio' if is_vid_site else 'Track'} ({platform_label})",
                        })
                        if len(tracks) >= limit:
                            break
                if tracks:
                    return tracks
            except Exception as e:
                print(f"yt-dlp metadata retry ({profile}): {e}")
        return tracks

    def search_soundcloud_tracks(self, search_term: str, limit: int = 2) -> List[Dict[str, Any]]:
        if yt_dlp is None:
            return []
        tracks: List[Dict[str, Any]] = []
        ydl_opts = self._build_base_ydl_opts(None)
        ydl_opts["extract_flat"] = "in_playlist"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"scsearch{limit}:{search_term}", download=False)
                for entry in (info.get("entries") or []):
                    if not entry:
                        continue
                    vid = entry.get("id", str(len(tracks)))
                    title = entry.get("title", "SoundCloud Track")
                    uploader = entry.get("uploader") or "SoundCloud Artist"
                    webpage_url = entry.get("webpage_url") or entry.get("url") or ""
                    if not webpage_url.startswith("http"):
                        continue
                    safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{uploader} - {title}").strip() or f"sc_{vid}"
                    tracks.append({
                        "track_id": f"sc_{vid}",
                        "title": title,
                        "artist": uploader,
                        "album": "SoundCloud Stream",
                        "artwork": entry.get("thumbnail") or "",
                        "audio_url": webpage_url,
                        "webpage_url": webpage_url,
                        "m4a_url": webpage_url,
                        "mp3_filename": f"{safe_name}.mp3",
                        "mp4_filename": f"{safe_name}.mp4",
                        "is_preview": False,
                        "is_video": False,
                        "extractor": "ytdlp",
                        "duration_str": _format_duration(entry.get("duration", 0)),
                        "source_label": "Full Track (SoundCloud)",
                    })
        except Exception as e:
            print(f"SoundCloud search notice: {e}")
        return tracks

    def convert_track_to_media_bytes(
        self,
        track_or_url: Any,
        output_format: str = "mp3",
        bitrate: str = "192k",
        max_bytes: Optional[int] = None,
    ) -> bytes:
        fmt = output_format.lower().strip().lstrip(".")
        if fmt not in ("mp3", "mp4"):
            fmt = "mp3"

        ffmpeg_exe = self.ffmpeg_exe or _get_ffmpeg_executable()
        if not ffmpeg_exe:
            raise RuntimeError("FFmpeg binary not available. Ensure 'imageio-ffmpeg' is installed.")

        if isinstance(track_or_url, dict):
            extractor = track_or_url.get("extractor", "http")
            target_url = (
                track_or_url.get("webpage_url")
                if extractor == "ytdlp"
                else (track_or_url.get("audio_url") or track_or_url.get("m4a_url", ""))
            )
        else:
            target_url = str(track_or_url).strip()
            extractor = "ytdlp" if _extract_direct_media_url(target_url) else "http"

        if not target_url:
            raise ValueError("No valid media URL provided.")

        if extractor == "ytdlp" and yt_dlp is not None:
            numeric_kbps = re.sub(r"[^\d]", "", str(bitrate)) or "192"
            last_error: Optional[Exception] = None

            for client_profile in (["android_vr", "android"], ["ios", "tv_embedded"], ["web"], None):
                try:
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        ydl_opts = self._build_base_ydl_opts(client_profile)
                        if fmt == "mp3":
                            ydl_opts.update({
                                "format": "bestaudio/best",
                                "outtmpl": os.path.join(tmp_dir, "media_export.%(ext)s"),
                                "postprocessors": [
                                    {
                                        "key": "FFmpegExtractAudio",
                                        "preferredcodec": "mp3",
                                        "preferredquality": numeric_kbps,
                                    }
                                ],
                            })
                        else:
                            ydl_opts.update({
                                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                                "merge_output_format": "mp4",
                                "outtmpl": os.path.join(tmp_dir, "media_export.%(ext)s"),
                            })

                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.extract_info(target_url, download=True)

                        expected_file = os.path.join(tmp_dir, f"media_export.{fmt}")
                        if not os.path.exists(expected_file):
                            produced = [
                                os.path.join(tmp_dir, fn)
                                for fn in os.listdir(tmp_dir)
                                if fn.startswith("media_export.")
                            ]
                            if produced:
                                expected_file = produced[0]

                        if os.path.exists(expected_file):
                            with open(expected_file, "rb") as f:
                                out_bytes = f.read()
                            if max_bytes and len(out_bytes) > max_bytes and fmt == "mp3" and numeric_kbps != "96":
                                return self.convert_track_to_media_bytes(track_or_url, output_format="mp3", bitrate="96k", max_bytes=None)
                            return out_bytes
                except Exception as exc:
                    last_error = exc
                    continue

            err_msg = f"All extractor profiles failed for {target_url}: {last_error}"
            self._log_issue("Media 403 / Extraction Failure", err_msg)
            raise RuntimeError(err_msg)

        audio_resp = requests.get(target_url, headers=BROWSER_HEADERS, allow_redirects=True, timeout=30)
        audio_resp.raise_for_status()

        if fmt == "mp4" and target_url.lower().endswith(".mp4"):
            return audio_resp.content

        def _encode_audio(target_bitrate: str) -> bytes:
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

        mp3_bytes = _encode_audio(bitrate)
        if max_bytes and len(mp3_bytes) > max_bytes:
            mp3_bytes = _encode_audio("96k")
        return mp3_bytes

    def convert_track_to_mp3_bytes(
        self,
        track_or_url: Any,
        bitrate: str = "192k",
        max_bytes: Optional[int] = None,
    ) -> bytes:
        return self.convert_track_to_media_bytes(track_or_url, output_format="mp3", bitrate=bitrate, max_bytes=max_bytes)


def _search_audius_full_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    for host in _get_audius_hosts():
        try:
            resp = requests.get(
                f"{host}/v1/tracks/search",
                params={"query": search_term, "app_name": "AuroraKoA"},
                headers=BROWSER_HEADERS,
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
                    "mp4_filename": f"{safe_name}.mp4",
                    "is_preview": False,
                    "is_video": False,
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
                "q": f"({search_term}) AND mediatype:(audio OR movies)",
                "fl[]": ["identifier", "title", "creator", "mediatype"],
                "sort[]": "downloads desc",
                "rows": 4,
                "page": 1,
                "output": "json",
            },
            headers=BROWSER_HEADERS,
            timeout=7,
        )
        if search_resp.status_code != 200:
            return []

        docs = search_resp.json().get("response", {}).get("docs", [])
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue

            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", headers=BROWSER_HEADERS, timeout=6)
            if meta_resp.status_code != 200:
                continue

            meta_json = meta_resp.json()
            item_meta = meta_json.get("metadata") or {}
            files = meta_json.get("files") or []

            media_files = [
                f for f in files
                if str(f.get("name", "")).lower().endswith((".mp3", ".mp4"))
                and "_64kb" not in str(f.get("name", "")).lower()
            ]
            if not media_files:
                continue

            chosen = media_files[0]
            fname = chosen["name"]
            encoded_fname = urllib.parse.quote(fname)
            direct_url = f"https://archive.org/download/{identifier}/{encoded_fname}"
            is_vid = fname.lower().endswith(".mp4")

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
                "album": "Internet Archive Media",
                "artwork": f"https://archive.org/services/img/{identifier}",
                "audio_url": direct_url,
                "webpage_url": direct_url,
                "m4a_url": direct_url,
                "mp3_filename": f"{safe_name}.mp3",
                "mp4_filename": f"{safe_name}.mp4",
                "is_preview": False,
                "is_video": is_vid,
                "extractor": "http",
                "duration_str": _format_duration(chosen.get("length")),
                "source_label": f"Full {'Video' if is_vid else 'Track'} (Internet Archive)",
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
        headers=BROWSER_HEADERS,
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
            "mp4_filename": f"{safe_name}.mp4",
            "is_preview": True,
            "is_video": False,
            "extractor": "http",
            "duration_str": "0:30",
            "source_label": "30s Preview",
        })
    return tracks


_DEFAULT_CONDUIT = MusicConduit()


def search_songs_hybrid(
    query: str,
    limit: int = 4,
    local_media: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
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

    if local_media:
        term_lower = compact_query.lower()
        for item in local_media:
            url = str(item.get("url", ""))
            title = str(item.get("title", ""))
            desc = str(item.get("description", ""))
            if term_lower in title.lower() or term_lower in desc.lower():
                if _extract_direct_media_url(url):
                    _add_unique(_DEFAULT_CONDUIT.search_youtube_tracks(url, limit=1))
                elif any(ext in url.lower() for ext in (".mp3", ".mp4", ".m4a", ".wav", ".ogg")):
                    safe_name = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "vault_track"
                    is_vid = url.lower().endswith(".mp4")
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
                        "mp4_filename": f"{safe_name}.mp4",
                        "is_preview": False,
                        "is_video": is_vid,
                        "extractor": "http",
                        "duration_str": "Full",
                        "source_label": "Vault Full Media",
                    }])

    _add_unique(_DEFAULT_CONDUIT.search_youtube_tracks(primary_query, limit=2))

    if not _extract_direct_media_url(primary_query):
        _add_unique(_DEFAULT_CONDUIT.search_dailymotion_tracks(compact_query, limit=2))

    if not _extract_direct_media_url(primary_query) and len(candidates) < limit:
        _add_unique(_DEFAULT_CONDUIT.search_soundcloud_tracks(compact_query, limit=2))

    if len(candidates) < limit:
        _add_unique(_search_audius_full_tracks(compact_query, limit=2))

    if len(candidates) < limit:
        _add_unique(_search_internet_archive_tracks(compact_query, limit=2))

    if len(candidates) < limit:
        try:
            _add_unique(_search_itunes_preview_tracks(compact_query, limit=2))
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


def convert_track_to_media_bytes(
    track_or_url: Any,
    output_format: str = "mp3",
    bitrate: str = "192k",
    max_bytes: Optional[int] = None,
) -> bytes:
    return _DEFAULT_CONDUIT.convert_track_to_media_bytes(
        track_or_url, output_format=output_format, bitrate=bitrate, max_bytes=max_bytes
    )


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
