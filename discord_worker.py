"""
discord_worker.py
Encapsulates the optional Discord bot instance, event listeners (on_ready, on_message,
on_raw_reaction_add), Zero-Token Reaction Pin Learning, !learn & !scout commands,
Silence Protocol, hostility defense overrides, YouTube/Hybrid MP3 delivery via
capabilities.MusicConduit, live Discord channel directory + channel name resolver,
and cross-thread message broadcaster.
"""

import io
import random
import asyncio
import threading
from typing import Optional, Tuple, List, Dict, Any
import discord
from discord.ext import commands

from profiles import BotProfile, get_secret_or_env
from database import DatabaseManager
from ai_engine import (
    GeminiEngine,
    SEARCH_TRIGGERS,
    REMEMBER_TRIGGERS,
    SILENCE_TRIGGERS,
)
from capabilities import (
    AUDIO_OWNERSHIP_NOTICE,
    is_song_request,
    search_songs_hybrid,
    convert_track_to_mp3_bytes,
)

LEARN_REACTION_EMOJIS = {"📌", "🌟", "⭐"}


class DiscordWorker:
    """
    Manages the background Discord bot gateway thread, zero-token reaction learning,
    live channel directory (for the Web Broadcaster dropdown & channel name display),
    and thread-safe broadcasting from the Streamlit web interface.
    """

    def __init__(self, profile: BotProfile, db: DatabaseManager, engine: GeminiEngine):
        self.profile = profile
        self.db = db
        self.engine = engine
        self.token = get_secret_or_env("DISCORD_TOKEN", "")
        raw_channels = get_secret_or_env("ALLOWED_CHANNELS", "")
        self.allowed_channel_ids = [ch.strip() for ch in str(raw_channels).split(",") if ch.strip()]
        self.channels_cache: Dict[str, Dict[str, str]] = {}

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot: Optional[commands.Bot] = None

        if self.should_run:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.reactions = True
            intents.guilds = True
            self.bot = commands.Bot(command_prefix="!", intents=intents)
            self._register_events()

    @property
    def should_run(self) -> bool:
        return bool(self.profile.enable_discord and self.token)

    def _refresh_channels_cache_from_guilds(self) -> None:
        """Populates self.channels_cache with all accessible guild text channels."""
        if not self.bot or not self.bot.guilds:
            return
        updated: Dict[str, Dict[str, str]] = {}
        for guild in self.bot.guilds:
            me = guild.me
            for ch in guild.text_channels:
                perms = ch.permissions_for(me) if me else None
                if perms and perms.view_channel and perms.send_messages:
                    cid = str(ch.id)
                    updated[cid] = {
                        "id": cid,
                        "name": f"#{ch.name}",
                        "guild": guild.name,
                        "label": f"{guild.name} — #{ch.name} ({cid})",
                    }
        if updated:
            self.channels_cache.update(updated)

    def get_available_channels(self) -> List[Dict[str, str]]:
        """
        Returns a sorted list of Discord channels available for broadcasting,
        combining live guild text channels and any ALLOWED_CHANNELS IDs from Secrets.
        """
        self._refresh_channels_cache_from_guilds()
        results_by_id: Dict[str, Dict[str, str]] = dict(self.channels_cache)

        for cid in self.allowed_channel_ids:
            if cid not in results_by_id:
                resolved_name = "Unknown Channel"
                resolved_guild = "Configured Secret"
                if self.bot and cid.isdigit():
                    ch = self.bot.get_channel(int(cid))
                    if ch is not None:
                        resolved_name = f"#{getattr(ch, 'name', cid)}"
                        guild_obj = getattr(ch, "guild", None)
                        if guild_obj:
                            resolved_guild = guild_obj.name
                results_by_id[cid] = {
                    "id": cid,
                    "name": resolved_name,
                    "guild": resolved_guild,
                    "label": f"{resolved_guild} — {resolved_name} ({cid})",
                }

        return sorted(
            results_by_id.values(),
            key=lambda item: (item["guild"].lower(), item["name"].lower()),
        )

    def resolve_channel_info(self, channel_id: str) -> Dict[str, str]:
        """
        Resolves the human-readable channel name and server name for any Discord Channel ID.
        """
        cid = str(channel_id).strip()
        if not cid:
            return {"id": "", "name": "", "guild": ""}

        if cid in self.channels_cache:
            return self.channels_cache[cid]

        if self.bot and cid.isdigit():
            ch = self.bot.get_channel(int(cid))
            if ch is not None:
                guild_obj = getattr(ch, "guild", None)
                info = {
                    "id": cid,
                    "name": f"#{getattr(ch, 'name', cid)}",
                    "guild": guild_obj.name if guild_obj else "Direct Message",
                    "label": f"{guild_obj.name if guild_obj else 'DM'} — #{getattr(ch, 'name', cid)} ({cid})",
                }
                self.channels_cache[cid] = info
                return info

            if self.loop and self.loop.is_running():
                async def _fetch():
                    fetched = await self.bot.fetch_channel(int(cid))
                    if fetched is not None:
                        g_obj = getattr(fetched, "guild", None)
                        return {
                            "id": cid,
                            "name": f"#{getattr(fetched, 'name', cid)}",
                            "guild": g_obj.name if g_obj else "Direct Message",
                            "label": f"{g_obj.name if g_obj else 'DM'} — #{getattr(fetched, 'name', cid)} ({cid})",
                        }
                    return None

                try:
                    fut = asyncio.run_coroutine_threadsafe(_fetch(), self.loop)
                    fetched_info = fut.result(timeout=4)
                    if fetched_info:
                        self.channels_cache[cid] = fetched_info
                        return fetched_info
                except Exception:
                    pass

        return {
            "id": cid,
            "name": "Pending Gateway / Unresolved Channel",
            "guild": "Discord",
            "label": f"Discord — ({cid})",
        }

    def _register_events(self) -> None:
        if not self.bot:
            return

        @self.bot.event
        async def on_ready():
            self._refresh_channels_cache_from_guilds()
            print(
                f"{self.profile.bot_name} online as {self.bot.user.name} (ID: {self.bot.user.id}). "
                f"Mode: {self.profile.mode_key} | Channels Indexed: {len(self.channels_cache)}."
            )

        @self.bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            if not self.bot or not self.profile.enable_auto_learn:
                return
            emoji_str = str(payload.emoji)
            if emoji_str not in LEARN_REACTION_EMOJIS:
                return

            try:
                channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
                if not channel or not hasattr(channel, "fetch_message"):
                    return
                msg = await channel.fetch_message(payload.message_id)
                user = self.bot.get_user(payload.user_id) or await self.bot.fetch_user(payload.user_id)
                if not user or not msg or not msg.clean_content.strip():
                    return

                display_name = getattr(payload.member, "display_name", user.name) if payload.member else user.name
                if not self.engine.is_sovereign(user.name, display_name):
                    return

                raw_text = msg.clean_content.strip()
                first_line = raw_text.split("\n")[0][:36].strip()
                kw_slug = first_line.lower().replace(":", "").strip()[:28] or f"pinned_{msg.id}"
                topic_title = first_line.title() if len(first_line) > 3 else f"Pinned Strategy #{str(msg.id)[-4:]}"

                self.db.add_verified_strategy(
                    keyword=kw_slug,
                    topic=topic_title,
                    content=raw_text,
                    verified_by=f"{self.profile.creator_name} (Discord {emoji_str} Reaction Pin)",
                    category="Sovereign Pin",
                    access_tier="Everyone",
                )
                await msg.add_reaction("✨")
            except Exception as e:
                print(f"Reaction learning notice: {e}")

        @self.bot.event
        async def on_message(message: discord.Message):
            if not self.bot or message.author == self.bot.user:
                return
            if not self.profile.enable_discord_voice:
                return

            channel_id = str(message.channel.id)
            user_id = str(message.author.id)
            content = message.content.strip()
            clean_lower = content.lower()
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_sov = self.engine.is_sovereign(message.author.name, message.author.display_name)

            # 1. Zero-Token Direct Commands
            if content.startswith("!profile "):
                facts = content.replace("!profile ", "").strip()
                self.db.update_l1_profile(user_id, facts)
                await message.reply(f"Celestial record inscribed into your profile: `{facts}`")
                return

            if content.startswith("!note "):
                note_content = content.replace("!note ", "").strip()
                self.db.add_l2_episode(user_id, "User Note", note_content)
                await message.reply(f"Episode anchored in long-term memory: `{note_content}`")
                return

            if content.startswith("!learn ") and is_sov:
                body = content.replace("!learn ", "").strip()
                if "|" in body:
                    topic_part, content_part = [p.strip() for p in body.split("|", 1)]
                else:
                    topic_part, content_part = body[:28].strip(), body
                self.db.add_verified_strategy(
                    keyword=topic_part.lower()[:30],
                    topic=topic_part.title(),
                    content=content_part,
                    verified_by=f"{self.profile.creator_name} (!learn Command)",
                    category="Sovereign Strategy",
                    access_tier="Everyone",
                )
                await message.reply(f"✨ Inscribed into Permanent Strategy Archives: **{topic_part.title()}**")
                return

            if content.startswith("!scout") and is_sov:
                custom_q = content.replace("!scout", "").strip()
                await message.reply("🔭 Dispatching Autonomous KoA Update Scout across web, news, and wiki sources...")
                ok, report = await asyncio.to_thread(self.engine.run_koa_auto_scout, custom_q)
                await message.reply(f"✨ **Scout Complete:** {report}")
                return

            if is_sov and self.profile.enable_auto_learn and any(k in clean_lower for k in ["guide:", "mechanic:", "strategy:", "formula:"]):
                topic_key = content.split(":", 1)[0].strip()[:30]
                self.db.add_verified_strategy(
                    topic_key,
                    topic_key.title(),
                    content,
                    f"{self.profile.creator_name} (Direct Discord)",
                )
                await message.reply(f"Sovereign guide accepted and committed to permanent memory: `{topic_key}`")
                return

            # 2. Channel Whitelist Gate
            if self.allowed_channel_ids and (channel_id not in self.allowed_channel_ids) and not is_dm:
                if not self.bot.user.mentioned_in(message):
                    return

            # 3. Check Silence Commands
            bot_name_lower = self.profile.bot_name.lower()
            is_explicit_mention = self.bot.user.mentioned_in(message) or (bot_name_lower in clean_lower)
            is_silence_cmd = any(st in clean_lower for st in SILENCE_TRIGGERS)

            if is_explicit_mention and is_silence_cmd:
                self.db.set_silence(channel_id)
                await message.reply("Understood. I shall draw a veil over my resonance and remain silent.")
                return

            # 4. Check Active Silence State
            silence_state, _ = self.db.get_silence_state(channel_id)
            if silence_state == "silenced":
                if is_explicit_mention:
                    self.db.clear_silence(channel_id)
                    await message.reply("The veil lifts. How may I assist you?")
                else:
                    return
            elif silence_state == "grace_period":
                if is_explicit_mention:
                    self.db.clear_silence(channel_id)
                else:
                    if random.random() < 0.10:
                        self.db.clear_silence(channel_id)
                        await message.channel.send("My required hour of silence has passed... Am I permitted to speak freely once more?")
                    return

            # 5. Defense Trigger Override
            is_defense = self.engine.contains_hostility_toward_creator(content)
            if is_defense:
                self.db.record_abuse_offense(user_id, f"{message.author.name}")

            # 6. Engagement Detection
            recently_engaged = self.db.was_channel_recently_engaged(channel_id, minutes=3)
            should_respond = is_explicit_mention or is_dm or is_defense or recently_engaged

            # 7. Passive Chatter (5% Joke Roll)
            if not should_respond:
                if random.random() < 0.05:
                    await message.channel.send(self.db.get_random_joke())
                return

            raw_user_text = message.clean_content.replace(f"@{self.bot.user.name}", "").strip()
            clean_prompt = raw_user_text
            if is_defense:
                clean_prompt = (
                    f"Someone just said this hostile remark about {self.profile.creator_name}: '{content}'. "
                    f"Deliver an immediate, razor-sharp verbal defense."
                )
            elif not clean_prompt:
                return

            # 8. Deep Memory Recall Trigger
            if any(rt in clean_prompt.lower() for rt in REMEMBER_TRIGGERS):
                deep_context = self.db.get_deep_memory(user_id)
                clean_prompt = f"[User requested memory recall. Archived Records:\n{deep_context}\nAnswer user question: {clean_prompt}]"

            # 9. Local Strategy DB Lookup
            allowed_tiers = ["Everyone", "Alliance Only", "Sovereign Only"] if is_sov else ["Everyone", "Alliance Only"]
            local_strategy = self.db.query_local_docs(clean_prompt, allowed_tiers=allowed_tiers)
            if local_strategy:
                clean_prompt = f"{local_strategy}\n{clean_prompt}"

            # 10. Multi-Source Zero-Quota Research Integration
            if any(st in clean_prompt.lower() for st in SEARCH_TRIGGERS):
                research_text, domains = await asyncio.to_thread(self.engine.execute_multi_source_research, clean_prompt)
                if research_text:
                    clean_prompt = f"{research_text}\n\n{clean_prompt}"
                    self.engine.process_knowledge_ingestion(clean_prompt, research_text, domains, is_sovereign_user=is_sov)

            # 10b. YouTube + Hybrid Song Lookup & Playable MP3 Attachment for Discord
            discord_mp3_file: Optional[discord.File] = None
            matched_tracks: List[Dict[str, Any]] = []
            if is_song_request(raw_user_text):
                try:
                    matched_tracks = await asyncio.to_thread(
                        search_songs_hybrid,
                        raw_user_text,
                        2,
                        self.db.get_all_media(),
                    )
                    if matched_tracks:
                        top_track = matched_tracks[0]
                        mp3_bytes = await asyncio.to_thread(
                            convert_track_to_mp3_bytes,
                            top_track,
                            "192k",
                            7_500_000,
                        )
                        discord_mp3_file = discord.File(
                            io.BytesIO(mp3_bytes),
                            filename=top_track["mp3_filename"],
                        )
                        clean_prompt = (
                            f"[SYSTEM AUDIO ATTACHMENT — Playable MP3 file '{top_track['mp3_filename']}' "
                            f"({top_track['title']} by {top_track['artist']} — {top_track['source_label']}) is attached directly to this Discord message. "
                            f"STRICT RULE: Do NOT output any URLs or hyperlinks in your text reply.]\n"
                            f"{clean_prompt}"
                        )
                    else:
                        clean_prompt = (
                            "[SYSTEM AUDIO NOTICE: No verified playable open/vault stream was found for this song query. "
                            "STRICT RULE: Do NOT invent or output any URLs or links.]\n"
                            f"{clean_prompt}"
                        )
                except Exception as audio_err:
                    print(f"Discord audio conversion notice: {audio_err}")

            # 11. Creator Recognition vs. Unprompted Name Restraint
            if is_sov:
                clean_prompt = (
                    f"[Speaking directly to your beloved Creator and Sovereign, {self.profile.creator_name}. "
                    f"Keep your counsel sharp, warm, and natural without repeating her name unnecessarily]:\n"
                    f"{clean_prompt}"
                )

            # 12. Assemble Multi-Turn Context & Generate
            user_history = self.db.get_active_user_context(
                user_id,
                raw_user_text,
                timeout_minutes=120,
                max_turns=14,
                char_limit=1800,
            )
            contents = self.engine.build_conversation_contents(
                history_messages=user_history,
                current_prompt=clean_prompt,
                max_turns=14,
            )
            self.db.save_chat(user_id, channel_id, "user", raw_user_text)

            async with message.channel.typing():
                try:
                    response_obj = await self.engine.call_gemini_async(contents)
                    reply_text = response_obj.text if response_obj and response_obj.text else "The celestial threads waver... no response was formed."
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        reply_text = "The cosmic resonance stream across all keys is currently exhausted. Please give the ley lines a moment to replenish."
                    else:
                        reply_text = f"A rift occurred in the resonance stream: `{e}`"

                if matched_tracks and "Audio Notice:" not in reply_text:
                    reply_text = f"{reply_text}{AUDIO_OWNERSHIP_NOTICE}"

                self.db.save_chat(user_id, channel_id, "model", reply_text)

                if "joke" in clean_prompt.lower() and len(reply_text) < 300:
                    self.db.add_joke(reply_text, added_by=self.profile.bot_name)

                if len(reply_text) <= 2000:
                    if discord_mp3_file:
                        await message.reply(reply_text, file=discord_mp3_file)
                    else:
                        await message.reply(reply_text)
                else:
                    chunks = [reply_text[i : i + 1990] for i in range(0, len(reply_text), 1990)]
                    for idx, chunk in enumerate(chunks):
                        if idx == 0 and discord_mp3_file:
                            await message.reply(chunk, file=discord_mp3_file)
                        else:
                            await message.channel.send(chunk)

            asyncio.create_task(self.engine.maybe_update_user_summary(user_id))
            await self.bot.process_commands(message)

    def start_background_thread(self) -> None:
        if not self.should_run:
            return

        for th in threading.enumerate():
            if th.name == "AuroraDiscordWorker" and th.is_alive():
                return

        def _run():
            try:
                print(f"Starting Discord client gateway for {self.profile.bot_name}...")
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                if self.bot:
                    self.loop.run_until_complete(self.bot.start(self.token))
            except Exception as e:
                print(f"Discord gateway terminated: {e}")

        worker = threading.Thread(target=_run, name="AuroraDiscordWorker", daemon=True)
        worker.start()

    def send_broadcast(self, channel_id: str, message_text: str) -> Tuple[bool, str]:
        if not self.should_run or not self.bot or not self.loop:
            return False, "Discord worker is not active in this mode."

        async def _send():
            ch = self.bot.get_channel(int(channel_id))
            if ch is None:
                ch = await self.bot.fetch_channel(int(channel_id))
            if ch:
                await ch.send(message_text)
                return True, f"Broadcast delivered to channel #{getattr(ch, 'name', channel_id)}."
            return False, f"Channel {channel_id} not found."

        try:
            future = asyncio.run_coroutine_threadsafe(_send(), self.loop)
            return future.result(timeout=10)
        except Exception as e:
            return False, f"Broadcast failed: {e}"
