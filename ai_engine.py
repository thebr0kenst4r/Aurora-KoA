"""
ai_engine.py
Encapsulates the thread-safe Gemini API client rotation pool, retry backoff,
multi-turn conversation history builder (with strict user/model role alternation),
shared capability integration (MusicConduit & WebResearchSuite),
trusted-domain verification, voice drafting for Discord broadcasts,
and the Autonomous King of Avalon Update Scout.
"""

import asyncio
import threading
import itertools
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict, Any
from google import genai
from google.genai import types
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception

from profiles import BotProfile, get_secret_or_env
from database import DatabaseManager
from capabilities import (
    MusicConduit,
    WebResearchSuite,
    SEARCH_TRIGGERS,
    AUDIO_OWNERSHIP_NOTICE,
    is_song_request,
    search_songs_hybrid,
    convert_track_to_mp3_bytes,
    convert_audio_url_to_mp3_bytes,
    convert_m4a_url_to_mp3_bytes,
    extract_and_convert_audio,
)

REMEMBER_TRIGGERS = ["remember", "!remember", "recall our chat", "what did we discuss", "check memory"]
INSULT_KEYWORDS = ["stupid", "idiot", "sucks", "trash", "loser", "liar", "hate", "fake", "bad", "scam", "useless"]
SILENCE_TRIGGERS = ["be quiet", "shut up", "hush", "mute", "silence", "stop talking", "quiet"]


def is_rate_limit_error(exception: BaseException) -> bool:
    err = str(exception)
    return "429" in err or "RESOURCE_EXHAUSTED" in err


class GeminiEngine:
    """
    Thread-safe AI Engine managing key rotation, multi-turn history formatting,
    shared capabilities (MusicConduit + WebResearchSuite), trusted domain verification,
    voice drafting, and autonomous King of Avalon update scouting.
    """

    def __init__(self, profile: BotProfile, db: DatabaseManager):
        self.profile = profile
        self.db = db
        self.lock = threading.Lock()
        self.last_koa_scout_status: str = "Idle (Ready to scout)"
        self.music = MusicConduit()
        self.researcher = WebResearchSuite(mode_key=profile.mode_key)

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
        for d in domains:
            d_clean = d.lower()
            for td in self.profile.trusted_domains:
                if td.lower() in d_clean or d_clean in td.lower():
                    return True
        return False

    def build_conversation_contents(
        self,
        history_messages: Optional[List[Dict[str, Any]]],
        current_prompt: str,
        memory_context: str = "",
        max_turns: int = 14,
        char_limit: int = 2400,
    ) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []

        if memory_context:
            contents.append({
                "role": "user",
                "parts": [{"text": f"[Context & Memory Notes]\n{memory_context}"}],
            })
            contents.append({
                "role": "model",
                "parts": [{"text": "Understood. I have these records in mind."}],
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
                        "parts": [{"text": "[Conversation Started]"}],
                    })

                if contents and contents[-1]["role"] == sdk_role:
                    contents[-1]["parts"][0]["text"] += f"\n\n{text}"
                else:
                    contents.append({
                        "role": sdk_role,
                        "parts": [{"text": text}],
                    })

        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"][0]["text"] += f"\n\n{current_prompt}"
        else:
            contents.append({
                "role": "user",
                "parts": [{"text": current_prompt}],
            })

        return contents

    def execute_multi_source_research(self, query: str) -> Tuple[str, List[str]]:
        self.researcher.mode_key = self.profile.mode_key
        return self.researcher.execute_multi_source_research(query)

    def process_knowledge_ingestion(
        self,
        prompt: str,
        content: str,
        domains: List[str],
        is_sovereign_user: bool = False,
    ) -> Optional[str]:
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
                access_tier="Everyone",
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
                access_tier="Everyone",
            )
            return topic_snippet

        return None

    def run_koa_auto_scout(self, custom_topic: str = "") -> Tuple[bool, str]:
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
                "King of Avalon Fae season mechanics sanctum strategy",
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
                    config=types.GenerateContentConfig(temperature=0.25),
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
                        access_tier="Everyone",
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

    def draft_in_bot_voice(self, raw_announcement: str) -> str:
        if not raw_announcement.strip():
            return ""
        prompt = (
            f"Rewrite the following alliance/community announcement in your authentic voice as {self.profile.bot_name}. "
            f"Keep all tactical facts, times, coordinates, or rules 100% accurate and scannable, while infusing your "
            f"signature celestial poise:\n\n{raw_announcement}"
        )
        return self.call_gemini_sync(prompt)

    @retry(
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        retry=retry_if_exception(is_rate_limit_error),
        reraise=True,
    )
    async def call_gemini_async(self, contents: List[Dict[str, Any]]) -> Any:
        active_client = self.get_next_client()
        if not active_client:
            raise RuntimeError("No active Gemini client available.")

        config = types.GenerateContentConfig(
            system_instruction=self.profile.build_effective_system_prompt(),
            temperature=0.7,
        )
        try:
            return await active_client.aio.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config,
            )
        except Exception as e:
            if is_rate_limit_error(e):
                return await active_client.aio.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=contents,
                    config=config,
                )
            raise e

    def call_gemini_sync(
        self,
        prompt_payload: str,
        history_messages: Optional[List[Dict[str, Any]]] = None,
        memory_context: str = "",
    ) -> str:
        active_client = self.get_next_client()
        if not active_client:
            return "Configuration Notice: No Gemini API key found in Streamlit Secrets (`GEMINI_KEYS`)."

        config = types.GenerateContentConfig(
            system_instruction=self.profile.build_effective_system_prompt(),
            temperature=0.7,
        )
        contents = self.build_conversation_contents(
            history_messages=history_messages,
            current_prompt=prompt_payload,
            memory_context=memory_context,
        )
        try:
            resp = active_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config,
            )
            return resp.text or "The celestial threads waver... no response was formed."
        except Exception as e:
            if is_rate_limit_error(e):
                try:
                    resp2 = active_client.models.generate_content(
                        model="gemini-3.1-flash-lite",
                        contents=contents,
                        config=config,
                    )
                    return resp2.text or "No response formed."
                except Exception as e2:
                    return f"Resonance rate limit reached across pool: `{e2}`"
            return f"A rift occurred in the resonance stream: `{e}`"

    async def maybe_update_user_summary(self, user_id: str) -> None:
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
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                if res.text:
                    self.db.update_rolling_summary(str(user_id), res.text.strip())
        except Exception as e:
            print(f"Summary worker notice: {e}")
