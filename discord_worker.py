"""
discord_worker.py
Encapsulates the optional Discord bot instance, event listeners (on_ready, on_message,
on_raw_reaction_add), Zero-Token Reaction Pin Learning (📌 / 🌟 / ⭐), !learn command,
Silence Protocol, hostility defense overrides, and cross-thread message broadcaster.
"""

import random
import asyncio
import threading
from typing import Optional, Tuple
import discord
from discord.ext import commands

from profiles import BotProfile, get_secret_or_env
from database import DatabaseManager
from ai_engine import (
    GeminiEngine,
    SEARCH_TRIGGERS,
    REMEMBER_TRIGGERS,
    SILENCE_TRIGGERS
)

LEARN_REACTION_EMOJIS = {"📌", "🌟", "⭐"}


class DiscordWorker:
    """
    Manages the background Discord bot gateway thread, zero-token reaction learning,
    and thread-safe broadcasting from the Streamlit web interface.
    """

    def __init__(self, profile: BotProfile, db: DatabaseManager, engine: GeminiEngine):
        self.profile = profile
        self.db = db
        self.engine = engine
        self.token = get_secret_or_env("DISCORD_TOKEN", "")
        raw_channels = get_secret_or_env("ALLOWED_CHANNELS", "")
        self.allowed_channel_ids = [ch.strip() for ch in str(raw_channels).split(",") if ch.strip()]

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot: Optional[commands.Bot] = None

        if self.should_run:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.reactions = True
            self.bot = commands.Bot(command_prefix="!", intents=intents)
            self._register_events()

    @property
    def should_run(self) -> bool:
        return bool(self.profile.enable_discord and self.token)

    def _register_events(self) -> None:
        if not self.bot:
            return

        @self.bot.event
        async def on_ready():
            print(
                f"{self.profile.bot_name} online as {self.bot.user.name} (ID: {self.bot.user.id}). "
                f"Mode: {self.profile.mode_key} | Multi-Source Research + Reaction Learning Active."
            )

        @self.bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            """
            Zero-Token Reaction Learning:
            When Sovereign (Aerith) reacts to any Discord message with 📌, 🌟, or ⭐,
            Aurora automatically saves that message into strategy_docs at $0 token cost.
            """
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
                    access_tier="Everyone"
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

            # 1. Zero-Token Direct Commands (!profile, !note, !learn, !scout)
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
                    access_tier="Everyone"
                )
                await message.reply(f"✨ Inscribed into Permanent Strategy Archives: **{topic_part.title()}**")
                return

            if content.startswith("!scout") and is_sov:
                custom_q = content.replace("!scout", "").strip()
                await message.reply("🔭 Dispatching Autonomous KoA Update Scout across web, news, and wiki sources...")
                ok, report = await asyncio.to_thread(self.engine.run_koa_auto_scout, custom_q)
                await message.reply(f"✨ **Scout Complete:** {report}")
                return

            # Sovereign direct strategy ingestion
            if is_sov and self.profile.enable_auto_learn and any(k in clean_lower for k in ["guide:", "mechanic:", "strategy:", "formula:"]):
                topic_key = content.split(":", 1)[0].strip()[:30]
                self.db.add_verified_strategy(
                    topic_key, topic_key.title(), content,
                    f"{self.profile.creator_name} (Direct Discord)"
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

            # 5. Defense Trigger Override (Hostility toward Creator)
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

            clean_prompt = message.clean_content.replace(f"@{self.bot.user.name}", "").strip()
            if is_defense:
                clean_prompt = (
                    f"Someone just said this hostile remark about {self.profile.creator_name}: '{content}'. "
                    f"Deliver an immediate, razor-sharp verbal defense."
                )
            elif not clean_prompt:
                return

            # 8. Deep Memory Recall Trigger (!remember)
            if any(rt in clean_prompt.lower() for rt in REMEMBER_TRIGGERS):
                deep_context = self.db.get_deep_memory(user_id)
                clean_prompt = f"[User requested memory recall. Archived Records:\n{deep_context}\nAnswer user question: {clean_prompt}]"

            # 9. Local Strategy DB Lookup ($0 Token Lookup)
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

            # 11. Creator Recognition vs. Unprompted Name Restraint
            if is_sov:
                clean_prompt = (
                    f"[Speaking directly to your beloved Creator and Sovereign, {self.profile.creator_name}! "
                    f"Greet or answer her with warm, genuine excitement, pride, and devotion while keeping your counsel sharp]:\n"
                    f"{clean_prompt}"
                )

            # 12. Assemble Context & Generate
            user_history = self.db.get_active_user_context(user_id, clean_prompt, timeout_minutes=25, max_turns=2, char_limit=350)
            contents = user_history + [{"role": "user", "parts": [{"text": clean_prompt}]}]
            self.db.save_chat(user_id, channel_id, "user", clean_prompt)

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

                self.db.save_chat(user_id, channel_id, "model", reply_text)

                if "joke" in clean_prompt.lower() and len(reply_text) < 300:
                    self.db.add_joke(reply_text, added_by=self.profile.bot_name)

                if len(reply_text) <= 2000:
                    await message.reply(reply_text)
                else:
                    for chunk in [reply_text[i:i + 1990] for i in range(0, len(reply_text), 1990)]:
                        await message.channel.send(chunk)

            asyncio.create_task(self.engine.maybe_update_user_summary(user_id))
            await self.bot.process_commands(message)

    def start_background_thread(self) -> None:
        """Starts the Discord gateway daemon thread if enabled and not already running."""
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
        """Thread-safely dispatches an announcement message to a Discord channel from Streamlit."""
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