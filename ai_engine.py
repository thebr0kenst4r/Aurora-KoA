"""
ai_engine.py
Encapsulates the thread-safe Gemini API client rotation pool, retry backoff,
multi-turn conversation history builder (with strict user/model role alternation),
full-length hybrid song lookup (Local Vault -> Audius Full Tracks -> iTunes 30s Fallback)
& in-memory FFmpeg .mp3 transcoder, background rolling summarization,
4-method zero-quota web verification suite, trusted-domain verification,
voice drafting for Discord broadcasts, and the Autonomous King of Avalon Update Scout.
"""

import re
import shutil
import asyncio
import threading
import itertools
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict, Any
import requests
from google import genai
from google.genai import types
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception
from ddgs import DDGS

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

from profiles import BotProfile, get_secret_or_env
from database import DatabaseManager

SEARCH_TRIGGERS = [
    "search", "google", "look up", "lookup", "online", "latest news",
    "patch notes", "recent update", "current event", "new hero", "meta", "verify"
]
REMEMBER_TRIGGERS = ["remember", "!remember", "recall our chat", "what did we discuss", "check memory"]
INSULT_KEYWORDS = ["stupid", "idiot", "sucks", "trash", "loser", "liar", "hate", "fake", "bad", "scam", "useless"]
SILENCE_TRIGGERS = ["be quiet", "shut up", "hush", "mute", "silence", "stop talking", "quiet"]

SONG_EXPLICIT_TRIGGERS = [
    "mp3", "m4a", "song", "music", "track", "audio", "album", "listen to", "download song", "sing"
]
GAME_NON_MUSIC_WORDS = [
    "koa", "king of avalon", "kvk", "rally", "frost drake", "dragon", "troop", "march",
    "fae", "sanctum", "alliance", "excalibur", "spire", "emblem", "hero", "golem"
]

_CACHED_AUDIUS_HOST: Optional[str] = None


def is_rate_limit_error(exception: BaseException) -> bool:
    err = str(exception)
    return "429" in err or "RESOURCE_EXHAUSTED" in err


def is_song_request(text: str) -> bool:
    """Detects whether the user is requesting a song/audio track without colliding with KoA terms."""
    clean = text.lower().strip()
    if any(g in clean for g in GAME_NON_MUSIC_WORDS) and not any(k in clean for k in ("mp3", "m4a", "song", "music", "track", "audio")):
        return False
    if any(k in clean for k in SONG_EXPLICIT_TRIGGERS):
        return True
    if re.match(r"^(play|download|find)\s+.+\s+by\s+.+", clean):
        return True
    return False


def extract_song_query(text: str) -> str:
    """Strips conversational filler so audio search APIs receive a clean artist/song title."""
    cleaned = re.sub(
        r"\b(hey|hi|hello|aurora|please|can you|could you|give me|get me|find|search|look up|lookup|download|direct|link|links|to|for|a|an|the|song|music|track|audio|mp3|m4a|file|files|like|mp3juice|play|listen|full)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^\w\s\-&'().]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if len(cleaned) >= 2 else text.strip()


def _format_duration(seconds: Any) -> str:
    try:
        sec = int(seconds or 0)
        if sec <= 0:
            return ""
        mins, rem = divmod(sec, 60)
        return f"{mins}:{rem:02d}"
    except Exception:
        return ""


def _get_audius_host() -> str:
    """Discovers a healthy Audius discovery node with instant fallback to api.audius.co."""
    global _CACHED_AUDIUS_HOST
    if _CACHED_AUDIUS_HOST:
        return _CACHED_AUDIUS_HOST
    try:
        r = requests.get("https://api.audius.co", timeout=4)
        if r.status_code == 200:
            hosts = r.json().get("data", [])
            if hosts and isinstance(hosts[0], str):
                _CACHED_AUDIUS_HOST = hosts[0].rstrip("/")
                return _CACHED_AUDIUS_HOST
    except Exception:
        pass
    return "https://api.audius.co"


def _search_audius_full_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Queries Audius Open Music API for full-length downloadable/streamable songs."""
    host = _get_audius_host()
    resp = requests.get(
        f"{host}/v1/tracks/search",
        params={"query": search_term, "app_name": "AuroraKoA"},
        timeout=8,
    )
    resp.raise_for_status()

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
            "album": item.get("genre") or "Full Track Stream",
            "artwork": artwork_url,
            "audio_url": stream_url,
            "m4a_url": stream_url,
            "mp3_filename": f"{safe_name}.mp3",
            "is_preview": False,
            "duration_str": _format_duration(duration_sec),
            "source_label": "Full Track",
        })
        if len(tracks) >= limit:
            break
    return tracks


def _search_itunes_preview_tracks(search_term: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Fallback lookup using Apple's iTunes Search API (provides 30-second commercial previews)."""
    resp = requests.get(
        "https://itunes.apple.com/search",
        params={"term": search_term, "media": "music", "entity": "song", "limit": limit},
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
            "m4a_url": preview_url,
            "mp3_filename": f"{safe_name}.mp3",
            "is_preview": True,
            "duration_str": "0:30",
            "source_label": "30s Preview",
        })
    return tracks


def search_songs_hybrid(
    query: str,
    limit: int = 3,
    local_media: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """
    3-Tier Song Discovery:
    1) Checks local SQLite media_vault for direct full-length audio URLs.
    2) Searches Audius Open Music API for full-length tracks (no 30s limit).
    3) Falls back to iTunes Search API (30s preview) if fewer than `limit` full tracks match.
    """
    search_term = extract_song_query(query)
    if not search_term:
        return []

    results: List[Dict[str, Any]] = []
    term_lower = search_term.lower()

    # Tier 1: Local Media Vault direct audio URLs
    if local_media:
        for item in local_media:
            url = str(item.get("url", ""))
            title = str(item.get("title", ""))
            desc = str(item.get("description", ""))
            if any(ext in url.lower() for ext in (".mp3", ".m4a", ".wav", ".ogg")) and (
                term_lower in title.lower() or term_lower in desc.lower()
            ):
                safe_name = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "vault_track"
                results.append({
                    "track_id": f"vault_{item.get('id', len(results))}",
                    "title": title,
                    "artist": item.get("added_by", "The Broken Star"),
                    "album": item.get("category", "Vault Archive"),
                    "artwork": "",
                    "audio_url": url,
                    "m4a_url": url,
                    "mp3_filename": f"{safe_name}.mp3",
                    "is_preview": False,
                    "duration_str": "Full",
                    "source_label": "Vault Full Track",
                })

    # Tier 2: Audius full-length open catalog
    if len(results) < limit:
        try:
            audius_hits = _search_audius_full_tracks(search_term, limit=limit - len(results))
            results.extend(audius_hits)
        except Exception as e:
            print(f"Audius search notice: {e}")

    # Tier 3: iTunes 30-second commercial preview fallback
    if len(results) < limit:
        try:
            itunes_hits = _search_itunes_preview_tracks(search_term, limit=limit - len(results))
            results.extend(itunes_hits)
        except Exception as e:
            print(f"iTunes fallback search notice: {e}")

    return results[:limit]


def convert_audio_url_to_mp3_bytes(
    audio_url: str,
    bitrate: str = "192k",
    max_bytes: Optional[int] = None
) -> bytes:
    """
    Downloads a full-length or preview audio stream and transcodes it in memory
    into playable .mp3 bytes via bundled FFmpeg (imageio-ffmpeg).
    If max_bytes is set (e.g., for Discord upload caps), re-encodes at 112k if needed.
    """
    audio_resp = requests.get(audio_url, timeout=30)
    audio_resp.raise_for_status()

    ffmpeg_exe = None
    if imageio_ffmpeg is not None:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not ffmpeg_exe:
        ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        raise RuntimeError("FFmpeg binary not available. Ensure 'imageio-ffmpeg' is in requirements.txt.")

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
        mp3_bytes = _encode("112k")
    return mp3_bytes


# Backward-compatible alias
convert_m4a_url_to_mp3_bytes = convert_audio_url_to_mp3_bytes


class GeminiEngine:
    """
    Thread-safe AI Engine managing key rotation, multi-turn history formatting,
    multi-source zero-quota web research, trusted domain verification,
    voice drafting, and autonomous King of Avalon update scouting.
    """

    def __init__(self, profile: BotProfile, db: DatabaseManager):
        self.profile = profile
        self.db = db
        self.lock = threading.Lock()
        self.last_koa_scout_status: str = "Idle (Ready to scout)"

        raw_keys = (
            get_secret_or_env("GEMINI_KEYS")
            or get_secret_or_env("GEMINI_KEY")
        )
        self.key_pool = [k.strip() for k in str(raw_keys).split(",") if k.strip()]
        self.client_pool = [genai.Client(api_key=k) for k in self.key_pool] if self.key_pool else []
        self.client_cycle = itertools.cycle(self.client_pool) if self.key_pool else None

    def get_next_client(self) -> Optional[genai.Client]:
        with self.lock:
            if not self.client_cycle:
                return None
            return next(self.client_cycle)

    def is_sovereign(self, author_name: str, display_name: str = "") -> bool:
        clean = f"{author_name} {display_name}".lower()
        return any(ident in clean for ident in self.profile.creator_identifiers)

    def contains_hostility_toward_creator(self, text: str) -> bool:
        clean = text.lower()
        return (
            any(ident in clean for ident in self.profile.creator_identifiers)
            and any(bad in clean for bad in INSULT_KEYWORDS)
        )

    def is_domain_trusted(self, domains: List[str]) -> bool:
        """Checks if any discovered domain matches the profile's trusted_domains list."""
        for d in domains:
            d_clean = d.lower()
            for td in self.profile.trusted_domains:
                if td.lower() in d_clean or d_clean in td.lower():
                    return True
        return False

    # --- MULTI-TURN CONVERSATION HISTORY FORMATTER ---

    def build_conversation_contents(
        self,
        history_messages: Optional[List[Dict[str, Any]]],
        current_prompt: str,
        memory_context: str = "",
        max_turns: int = 14,
        char_limit: int = 2400
    ) -> List[Dict[str, Any]]:
        """
        Converts prior conversation messages (either Streamlit session dicts or SDK contents dicts)
        into valid, strictly alternating user/model turns for google-genai so Aurora remembers
        multi-turn discussions and complex multi-step tasks.
        """
        contents: List[Dict[str, Any]] = []

        if memory_context:
            contents.append({
                "role": "user",
                "parts": [{"text": f"[Context & Memory Notes]\n{memory_context}"}]
            })
            contents.append({
                "role": "model",
                "parts": [{"text": "Understood. I have these records in mind."}]
            })

        if history_messages:
            for msg in history_messages[-max_turns:]:
                raw_role = msg.get("role", "user")
                sdk_role = "model" if raw_role in ("assistant", "model") else "user"

                if "content" in msg:
                    text = str(msg["content"]).strip()
                elif "parts" in msg and msg["parts"]:
                    text = " ".join(
                        str(p.get("text", "")) for p in msg["parts"] if isinstance(p, dict)
                    ).strip()
                else:
                    text = ""

                if not text:
                    continue

                if len(text) > char_limit:
                    text = text[:char_limit] + "..."

                if not contents and sdk_role == "model":
                    contents.append({
                        "role": "user",
                        "parts": [{"text": "[Conversation Started]"}]
                    })

                if contents and contents[-1]["role"] == sdk_role:
                    contents[-1]["parts"][0]["text"] += f"\n\n{text}"
                else:
                    contents.append({
                        "role": sdk_role,
                        "parts": [{"text": text}]
                    })

        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"][0]["text"] += f"\n\n{current_prompt}"
        else:
            contents.append({
                "role": "user",
                "parts": [{"text": current_prompt}]
            })

        return contents

    # --- ZERO-QUOTA MULTI-SOURCE RESEARCH SUITE ($0 TOKEN / $0 API QUOTA) ---

    def search_ddg_text(self, query: str, max_results: int = 3) -> Tuple[List[str], List[str]]:
        """Method 1: Keyless DuckDuckGo general web search."""
        snippets, domains = [], []
        try:
            with DDGS() as ddgs:
                for r in list(ddgs.text(query, max_results=max_results)):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    href = r.get("href", "")
                    if body:
                        snippets.append(f"[Web: {title}] {body} ({href})")
                    if href and "/" in href:
                        parts = href.split("/")
                        if len(parts) > 2:
                            domains.append(parts[2])
        except Exception as e:
            print(f"DDG text search notice: {e}")
        return snippets, domains

    def search_ddg_news(self, query: str, max_results: int = 2) -> Tuple[List[str], List[str]]:
        """Method 2: Keyless DuckDuckGo live news search for recent patches & market updates."""
        snippets, domains = [], []
        try:
            with DDGS() as ddgs:
                for r in list(ddgs.news(query, max_results=max_results)):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    url = r.get("url", "")
                    date = r.get("date", "")
                    if body:
                        snippets.append(f"[News ({date}): {title}] {body} ({url})")
                    if url and "/" in url:
                        parts = url.split("/")
                        if len(parts) > 2:
                            domains.append(parts[2])
        except Exception as e:
            print(f"DDG news search notice: {e}")
        return snippets, domains

    def search_mediawiki(self, query: str) -> Tuple[List[str], List[str]]:
        """
        Method 3: Keyless MediaWiki / Fandom JSON API lookup.
        Queries the King of Avalon Fandom Wiki (in gaming mode) and Wikipedia for verified lore/stats.
        """
        snippets, domains = [], []
        endpoints = []
        if self.profile.mode_key == "gaming":
            endpoints.append(("kingofavalon.fandom.com", "https://kingofavalon.fandom.com/api.php"))
        endpoints.append(("en.wikipedia.org", "https://en.wikipedia.org/w/api.php"))

        headers = {"User-Agent": "AuroraStrategicBot/2.0 (Educational & Tactical Research)"}
        for domain_label, api_url in endpoints:
            try:
                params = {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "utf8": "1",
                    "format": "json",
                    "srlimit": 2
                }
                r = requests.get(api_url, params=params, headers=headers, timeout=6)
                if r.status_code == 200:
                    search_hits = r.json().get("query", {}).get("search", [])
                    for hit in search_hits:
                        title = hit.get("title", "")
                        raw_snip = hit.get("snippet", "")
                        clean_snip = (
                            raw_snip.replace('<span class="searchmatch">', "")
                            .replace("</span>", "")
                            .replace("&quot;", '"')
                        )
                        if clean_snip:
                            snippets.append(f"[Wiki ({domain_label}) - {title}]: {clean_snip}")
                            domains.append(domain_label)
            except Exception:
                pass
        return snippets, domains

    def search_reddit_public(self, query: str) -> Tuple[List[str], List[str]]:
        """
        Method 4: Keyless Reddit Public JSON search for community-verified patch changes & discussions.
        """
        snippets, domains = [], []
        headers = {"User-Agent": "AuroraStrategicBot/2.0"}
        try:
            encoded = urllib.parse.quote_plus(query)
            if self.profile.mode_key == "gaming":
                url = f"https://www.reddit.com/r/KingOfAvalon/search.json?q={encoded}&restrict_sr=1&sort=relevance&limit=2"
            else:
                url = f"https://www.reddit.com/search.json?q={encoded}&sort=relevance&limit=2"

            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                children = r.json().get("data", {}).get("children", [])
                for child in children:
                    post = child.get("data", {})
                    title = post.get("title", "")
                    selftext = (post.get("selftext", "") or "")[:280].replace("\n", " ")
                    if title:
                        snippets.append(f"[Community Thread: {title}] {selftext}")
                        domains.append("reddit.com/r/kingofavalon" if self.profile.mode_key == "gaming" else "reddit.com")
        except Exception:
            pass
        return snippets, domains

    def execute_multi_source_research(self, query: str) -> Tuple[str, List[str]]:
        """
        Combines all 4 zero-token research channels (DDG Web, DDG News, Wiki JSON, Reddit JSON)
        to cross-verify facts without spending any Google Search API quota.
        """
        clean_q = query
        for trig in ["search", "lookup", "look up", "google", "verify"]:
            clean_q = clean_q.replace(trig, "")
        clean_q = clean_q.strip() or query

        all_snippets: List[str] = []
        all_domains: List[str] = []

        s1, d1 = self.search_ddg_text(clean_q, max_results=3)
        s2, d2 = self.search_ddg_news(clean_q, max_results=2)
        s3, d3 = self.search_mediawiki(clean_q)
        s4, d4 = self.search_reddit_public(clean_q)

        all_snippets.extend(s1 + s2 + s3 + s4)
        for d in (d1 + d2 + d3 + d4):
            if d and d not in all_domains:
                all_domains.append(d)

        if not all_snippets:
            return "", []

        formatted = "[Live Verified Research Results]:\n" + "\n".join(all_snippets[:7])
        return formatted, all_domains

    def process_knowledge_ingestion(self, prompt: str, content: str, domains: List[str], is_sovereign_user: bool = False) -> Optional[str]:
        """
        Auto-inscribes knowledge into strategy_docs if:
        1) Authored by Sovereign (Aerith), OR
        2) Matches at least 1 Trusted Domain, OR
        3) Corroborated by 2+ independent web domains.
        """
        if not self.profile.enable_auto_learn and not is_sovereign_user:
            return None

        clean_p = prompt.lower()
        strat_triggers = ["guide", "mechanic", "strategy", "formula", "fae", "event", "tactic", "checklist"]

        if is_sovereign_user and any(k in clean_p for k in strat_triggers):
            topic_key = prompt.replace("guide", "").replace("strategy", "").strip()[:32]
            self.db.add_verified_strategy(
                keyword=topic_key,
                topic=topic_key.title(),
                content=content,
                verified_by=f"{self.profile.creator_name} (Direct Sovereign Authority)",
                category="Sovereign Strategy",
                access_tier="Everyone"
            )
            return topic_key

        trusted_hit = self.is_domain_trusted(domains)
        if trusted_hit or len(domains) >= 2:
            topic_snippet = prompt.replace("search", "").replace("lookup", "").replace("google", "").strip()[:32]
            verifier_label = (
                f"Trusted Source ({', '.join(domains[:2])})"
                if trusted_hit
                else f"Verified by {len(domains)} Sources ({', '.join(domains[:3])})"
            )
            self.db.add_verified_strategy(
                keyword=topic_snippet,
                topic=topic_snippet.title(),
                content=content[:650],
                verified_by=verifier_label,
                category="Web Verified",
                access_tier="Everyone"
            )
            return topic_snippet

        return None

    # --- AUTONOMOUS KING OF AVALON UPDATE SCOUT ---

    def run_koa_auto_scout(self, custom_topic: str = "") -> Tuple[bool, str]:
        """
        Searches across News, Wiki, and Web for new King of Avalon updates, patch notes,
        heroes, and Fae season shifts, synthesizes verified tactical guides via Gemini,
        and stores them automatically into strategy_docs.
        """
        if not self.profile.enable_auto_scout and not custom_topic:
            return False, "Auto-Scout is disabled for this profile mode."

        client = self.get_next_client()
        if not client:
            return False, "No Gemini API key available for synthesis."

        current_year = datetime.now(timezone.utc).year
        topics_to_scout = (
            [custom_topic.strip()]
            if custom_topic.strip()
            else [
                f"King of Avalon latest patch notes update {current_year}",
                f"King of Avalon new generation heroes tier list {current_year}",
                f"King of Avalon Fae season mechanics sanctum strategy"
            ]
        )

        ingested_topics: List[str] = []
        for scout_query in topics_to_scout:
            raw_research, domains = self.execute_multi_source_research(scout_query)
            if not raw_research or (len(domains) < 2 and not self.is_domain_trusted(domains)):
                continue

            synthesis_prompt = (
                f"You are {self.profile.bot_name}'s Tactical Intelligence Scout for King of Avalon.\n"
                f"Analyze the following multi-source research results for '{scout_query}'.\n"
                f"Write a concise, high-density tactical summary (3-5 bullet points) covering actionable mechanics, "
                f"numbers, troop/hero priorities, or update notes. Do NOT include fluff.\n\n"
                f"{raw_research}"
            )
            try:
                resp = client.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=synthesis_prompt,
                    config=types.GenerateContentConfig(temperature=0.25)
                )
                summary_text = (resp.text or "").strip()
                if summary_text:
                    kw_slug = f"koa scout {scout_query[:22].lower()}"
                    title_clean = scout_query.replace("King of Avalon", "KoA").strip().title()
                    self.db.add_verified_strategy(
                        keyword=kw_slug,
                        topic=f"Scout Report: {title_clean}",
                        content=summary_text,
                        verified_by=f"Aurora Auto-Scout ({len(domains)} Sources: {', '.join(domains[:2])})",
                        category="Auto-Scout Updates",
                        access_tier="Everyone"
                    )
                    ingested_topics.append(title_clean)
            except Exception as e:
                print(f"Auto-scout synthesis notice: {e}")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if ingested_topics:
            msg = f"Scouted & inscribed {len(ingested_topics)} update(s) at {ts}: {', '.join(ingested_topics)}"
            self.last_koa_scout_status = msg
            return True, msg
        else:
            msg = f"Scout completed at {ts} (No new multi-domain changes required inscription)."
            self.last_koa_scout_status = msg
            return False, msg

    # --- VOICE DRAFTER FOR DISCORD BROADCASTER ---

    def draft_in_bot_voice(self, raw_announcement: str) -> str:
        """Rewrites a plain admin draft into Aurora's ethereal, strategic voice."""
        if not raw_announcement.strip():
            return ""
        prompt = (
            f"Rewrite the following alliance/community announcement in your authentic voice as {self.profile.bot_name}. "
            f"Keep all tactical facts, times, coordinates, or rules 100% accurate and scannable, while infusing your "
            f"signature celestial poise:\n\n{raw_announcement}"
        )
        return self.call_gemini_sync(prompt)

    # --- GEMINI GENERATION & BACKGROUND SUMMARIZER ---

    @retry(
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        retry=retry_if_exception(is_rate_limit_error),
        reraise=True
    )
    async def call_gemini_async(self, contents: List[Dict[str, Any]]) -> Any:
        active_client = self.get_next_client()
        if not active_client:
            raise RuntimeError("No active Gemini client available.")

        config = types.GenerateContentConfig(
            system_instruction=self.profile.build_effective_system_prompt(),
            temperature=0.7
        )
        try:
            return await active_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config
            )
        except Exception as e:
            if is_rate_limit_error(e):
                return await active_client.aio.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=contents,
                    config=config
                )
            raise e

    def call_gemini_sync(
        self,
        prompt_payload: str,
        history_messages: Optional[List[Dict[str, Any]]] = None,
        memory_context: str = ""
    ) -> str:
        """
        Synchronous generation helper for the Streamlit Web UI.
        Accepts prior conversation turns + L1/L2 memory context so Aurora retains full multi-turn context.
        """
        active_client = self.get_next_client()
        if not active_client:
            return "Configuration Notice: No Gemini API key found in Streamlit Secrets (`GEMINI_KEYS`)."

        config = types.GenerateContentConfig(
            system_instruction=self.profile.build_effective_system_prompt(),
            temperature=0.7
        )
        contents = self.build_conversation_contents(
            history_messages=history_messages,
            current_prompt=prompt_payload,
            memory_context=memory_context
        )
        try:
            resp = active_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config
            )
            return resp.text or "The celestial threads waver... no response was formed."
        except Exception as e:
            if is_rate_limit_error(e):
                try:
                    resp2 = active_client.models.generate_content(
                        model="gemini-3.1-flash-lite",
                        contents=contents,
                        config=config
                    )
                    return resp2.text or "No response formed."
                except Exception as e2:
                    return f"Resonance rate limit reached across pool: `{e2}`"
            return f"A rift occurred in the resonance stream: `{e}`"

    async def maybe_update_user_summary(self, user_id: str) -> None:
        """Asynchronously condenses recent user dialogue into a rolling summary after a 6s delay."""
        await asyncio.sleep(6)
        conn = self.db._get_conn()
        c = conn.cursor()
        c.execute("SELECT role, content FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 8", (str(user_id),))
        rows = c.fetchall()
        conn.close()

        if len(rows) < 4:
            return

        dialogue = "\n".join([f"{r}: {txt[:200]}" for r, txt in reversed(rows)])
        summary_prompt = (
            "Condense this dialogue into 1-2 factual sentences capturing key user details, "
            "preferences, or active strategy topics. Do not write filler:\n" + dialogue
        )
        try:
            active_client = self.get_next_client()
            if active_client:
                res = await active_client.aio.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=summary_prompt,
                    config=types.GenerateContentConfig(temperature=0.2)
                )
                if res.text:
                    self.db.update_rolling_summary(str(user_id), res.text.strip())
        except Exception as e:
            print(f"Summary worker notice: {e}")
