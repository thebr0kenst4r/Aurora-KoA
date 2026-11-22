"""
web_ui.py
Encapsulates the Streamlit multi-page application:
  - Multi-turn conversation memory
  - Multi-site Media & Music cards with Embedded Video Players ABOVE download links,
    both MP3 Audio and MP4 Video conversions, and automatic Sovereign R2 archival
  - Delegated modular domain calculators
  - Site-wide persistent mode switching
  - Restored Discord Broadcaster Channel Dropdown + Channel Name field
  - Self-Upgrade & GitHub Actions CI/CD Studio (unresolved problems ledger, autonomous
    capability/mode generation, PR & CI status inspection, Sovereign approval/merge)
  - Cloudflare R2 & GitHub Dated Backup Vault (YYYY/MM/DD)
"""

import os
import io
import zipfile
from datetime import datetime, timezone
from typing import List, Dict, Any
import pandas as pd
import streamlit as st

from profiles import BotProfile, DomainConfig, get_secret_or_env
from database import DatabaseManager
from ai_engine import (
    GeminiEngine,
    REMEMBER_TRIGGERS,
)
from capabilities import (
    SEARCH_TRIGGERS,
    AUDIO_OWNERSHIP_NOTICE,
    is_song_request,
    search_songs_hybrid,
    convert_track_to_media_bytes,
)
from discord_worker import DiscordWorker


class StreamlitUI:
    def __init__(self, profile: BotProfile, db: DatabaseManager, engine: GeminiEngine, discord_worker: DiscordWorker):
        self.profile = profile
        self.db = db
        self.engine = engine
        self.discord_worker = discord_worker

        self.sovereign_key = get_secret_or_env("AERITH_KEY", "cosmic_resonance_key")
        self.alliance_key = get_secret_or_env("ALLIANCE_KEY", "alliance_honor_key")
        self._init_session_state()

    def _init_session_state(self) -> None:
        if "auth_role" not in st.session_state:
            token = st.query_params.get("resonance_token", "")
            if token == "sovereign":
                st.session_state.auth_role = "Sovereign"
            elif token == "member":
                st.session_state.auth_role = "Member"
            else:
                st.session_state.auth_role = "Guest"

        if "web_messages" not in st.session_state or st.session_state.get("last_mode") != self.profile.mode_key:
            st.session_state.web_messages = [
                {"role": "assistant", "content": self.profile.welcome_message}
            ]
            st.session_state.last_mode = self.profile.mode_key

        if "converted_mp3s" not in st.session_state:
            st.session_state.converted_mp3s = {}

        if "converted_mp4s" not in st.session_state:
            st.session_state.converted_mp4s = {}

        if "chat_cleared_at" not in st.session_state:
            st.session_state.chat_cleared_at = None

    @property
    def is_admin(self) -> bool:
        return st.session_state.auth_role == "Sovereign"

    @property
    def is_member(self) -> bool:
        return st.session_state.auth_role in ("Member", "Sovereign")

    @property
    def active_web_user_id(self) -> str:
        if self.is_admin:
            return "sovereign_aerith"
        if self.is_member:
            return "alliance_member"
        return "web_guest"

    def get_allowed_tiers(self) -> List[str]:
        if self.is_admin:
            return self.profile.access_tiers
        elif self.is_member:
            return self.profile.access_tiers[:2]
        return [self.profile.access_tiers[0]]

    def render_banner(self) -> None:
        banner = self.profile.render_dynamic_banner()
        if banner:
            st.markdown(banner, unsafe_allow_html=True)

    def _archive_to_r2_if_sovereign(self, filename: str, raw_bytes: bytes, mime_type: str, title: str, media_type: str) -> None:
        if self.is_admin and self.db.r2.is_configured and raw_bytes:
            ok, r2_key, r2_url = self.db.r2.archive_sovereign_media(filename, raw_bytes, mime_type=mime_type)
            if ok:
                self.db.log_r2_media_download(title, media_type, r2_key, r2_url, len(raw_bytes))
                st.toast(f"☁️ Archived to Sovereign R2: {r2_key}")

    def _render_track_cards(self, tracks: List[Dict[str, Any]], msg_index: int) -> None:
        for idx, track in enumerate(tracks):
            tid = str(track.get("track_id", f"{msg_index}_{idx}"))
            extractor = track.get("extractor", "http")
            stream_url = track.get("audio_url") or track.get("m4a_url", "")
            link_url = track.get("webpage_url") or stream_url
            is_preview = bool(track.get("is_preview", False))
            is_video = bool(track.get("is_video", False))
            dur = track.get("duration_str", "")
            src_label = track.get("source_label", "Full Track")
            badge = f"🟡 {src_label}" if is_preview else f"🟢 {src_label}{f' ({dur})' if dur else ''}"
            audio_fmt = "audio/mp4" if is_preview else "audio/mpeg"

            with st.container(border=True):
                col_art, col_info = st.columns([1, 6])
                with col_art:
                    if track.get("artwork"):
                        st.image(track["artwork"], width=68)
                with col_info:
                    st.markdown(f"**{track['title']}** — {track['artist']}  `{badge}`")
                    if track.get("album"):
                        st.caption(track["album"])

                if is_video and link_url:
                    try:
                        st.video(link_url)
                    except Exception:
                        pass
                elif extractor != "ytdlp" and stream_url:
                    st.audio(stream_url, format=audio_fmt)

                if is_video:
                    b_src, b_mp3, b_mp4 = st.columns(3)
                else:
                    b_src, b_mp3 = st.columns(2)
                    b_mp4 = None

                with b_src:
                    st.link_button("🔗 Open Source", link_url, width="stretch")

                with b_mp3:
                    if tid not in st.session_state.converted_mp3s:
                        if st.button("🎵 Prepare MP3", key=f"conv_mp3_{msg_index}_{idx}_{tid}", width="stretch"):
                            with st.spinner("Extracting & encoding MP3 audio..."):
                                try:
                                    mp3_bytes = convert_track_to_media_bytes(track, output_format="mp3")
                                    st.session_state.converted_mp3s[tid] = mp3_bytes
                                    self._archive_to_r2_if_sovereign(
                                        track.get("mp3_filename", "track.mp3"),
                                        mp3_bytes,
                                        "audio/mpeg",
                                        track["title"],
                                        "MP3 Audio",
                                    )
                                    st.rerun()
                                except Exception as conv_err:
                                    self.db.record_unresolved_issue("mp3_conversion", f"MP3 failed: {track['title']}", str(conv_err), self.profile.mode_key)
                                    st.error(f"MP3 extraction notice: {conv_err}")
                    else:
                        st.success("MP3 Ready ✅")

                if b_mp4 is not None:
                    with b_mp4:
                        if tid not in st.session_state.converted_mp4s:
                            if st.button("🎬 Prepare MP4", key=f"conv_mp4_{msg_index}_{idx}_{tid}", width="stretch"):
                                with st.spinner("Downloading MP4 video..."):
                                    try:
                                        mp4_bytes = convert_track_to_media_bytes(track, output_format="mp4")
                                        st.session_state.converted_mp4s[tid] = mp4_bytes
                                        self._archive_to_r2_if_sovereign(
                                            track.get("mp4_filename", "video.mp4"),
                                            mp4_bytes,
                                            "video/mp4",
                                            track["title"],
                                            "MP4 Video",
                                        )
                                        st.rerun()
                                    except Exception as vid_err:
                                        self.db.record_unresolved_issue("mp4_conversion", f"MP4 failed: {track['title']}", str(vid_err), self.profile.mode_key)
                                        st.error(f"MP4 download notice: {vid_err}")
                        else:
                            st.success("MP4 Ready ✅")

                if tid in st.session_state.converted_mp3s:
                    mp3_data = st.session_state.converted_mp3s[tid]
                    st.audio(mp3_data, format="audio/mpeg")
                    st.download_button(
                        label="⬇️ Download MP3 Audio",
                        data=mp3_data,
                        file_name=track.get("mp3_filename", "track.mp3"),
                        mime="audio/mpeg",
                        key=f"dl_mp3_{msg_index}_{idx}_{tid}",
                        width="stretch",
                    )

                if tid in st.session_state.converted_mp4s:
                    mp4_data = st.session_state.converted_mp4s[tid]
                    st.download_button(
                        label="⬇️ Download MP4 Video",
                        data=mp4_data,
                        file_name=track.get("mp4_filename", "video.mp4"),
                        mime="video/mp4",
                        key=f"dl_mp4_{msg_index}_{idx}_{tid}",
                        width="stretch",
                    )

    def render_sidebar_auth(self) -> None:
        with st.sidebar:
            st.subheader(self.profile.get_str("sidebar_header", "Identity & Resonance"))

            if st.session_state.auth_role == "Guest":
                st.info(self.profile.get_str("guest_badge", "Browsing Public Sanctum as Guest"))

                with st.container(border=True):
                    st.markdown(f"**{self.profile.get_str('alliance_section_title', '🛡️ Alliance Key Access')}**")
                    alliance_pass = st.text_input(
                        self.profile.get_str("alliance_login_prompt", "Enter Alliance Resonance Key:"),
                        type="password",
                        key="sidebar_alliance_pass",
                    )
                    if st.button(self.profile.get_str("alliance_login_button", "Attune Alliance Access"), width="stretch"):
                        if alliance_pass == self.alliance_key:
                            st.session_state.auth_role = "Member"
                            st.query_params["resonance_token"] = "member"
                            st.rerun()
                        elif alliance_pass == self.sovereign_key:
                            st.session_state.auth_role = "Sovereign"
                            st.query_params["resonance_token"] = "sovereign"
                            st.rerun()
                        else:
                            st.error("Alliance key mismatch.")

                with st.expander(self.profile.get_str("sovereign_section_title", "👑 Sovereign Sanctum Login"), expanded=False):
                    sov_pass = st.text_input(
                        self.profile.get_str("sovereign_login_prompt", "Enter Sovereign Creator Key:"),
                        type="password",
                        key="sidebar_sovereign_pass",
                    )
                    if st.button(self.profile.get_str("sovereign_login_button", "Awaken Sovereign Command"), width="stretch"):
                        if sov_pass == self.sovereign_key:
                            st.session_state.auth_role = "Sovereign"
                            st.query_params["resonance_token"] = "sovereign"
                            st.rerun()
                        else:
                            st.error("Sovereign resonance mismatch.")
            else:
                badge_text = (
                    self.profile.get_str("admin_badge", "👑 Sovereign Recognized: Aerith")
                    if self.is_admin
                    else self.profile.get_str("member_badge", "🛡️ Alliance Resonance Verified")
                )
                st.success(badge_text)

                if not self.is_admin:
                    with st.expander(self.profile.get_str("sovereign_section_title", "👑 Sovereign Sanctum Login"), expanded=False):
                        sov_pass = st.text_input(
                            self.profile.get_str("sovereign_login_prompt", "Enter Sovereign Creator Key:"),
                            type="password",
                            key="sidebar_sovereign_elevate",
                        )
                        if st.button(self.profile.get_str("sovereign_login_button", "Awaken Sovereign Command"), width="stretch"):
                            if sov_pass == self.sovereign_key:
                                st.session_state.auth_role = "Sovereign"
                                st.query_params["resonance_token"] = "sovereign"
                                st.rerun()
                            else:
                                st.error("Sovereign resonance mismatch.")

                if st.button(self.profile.get_str("logout_button", "Seal Resonance (Log Out)"), width="stretch"):
                    st.session_state.auth_role = "Guest"
                    st.query_params.clear()
                    st.rerun()

            if self.is_admin:
                st.divider()
                st.caption("🎛️ **Site-Wide Mode Switcher (Applies to Everyone)**")
                custom_keys = self.db.get_all_custom_mode_keys()
                modes = DomainConfig.get_available_modes(custom_keys)
                current_idx = modes.index(self.profile.mode_key) if self.profile.mode_key in modes else 0
                selected_mode = st.selectbox(
                    "Active Site Mode:",
                    options=modes,
                    index=current_idx,
                    key="sidebar_mode_select",
                )
                if selected_mode != self.profile.mode_key:
                    DomainConfig.set_active_mode_key(selected_mode, db=self.db)
                    st.rerun()

                st.caption(f"🗄️ Mode DB: `{self.db.db_basename}`")
                if self.db.r2.is_configured:
                    st.caption(f"🪣 **Cloudflare R2:** `{self.db.r2.bucket}`")
                if self.db.vault.is_configured:
                    st.caption(f"☁️ **GitHub Vault:** `{self.db.vault.branch}`")

    def page_resonance_chat(self) -> None:
        self.render_banner()
        col_h, col_btn = st.columns([3, 1])
        with col_h:
            st.header(self.profile.get_str("chat_header", "Celestial Resonance Chamber"))
            st.caption(self.profile.get_str("chat_caption", "Eternal Strategist, Guardian & Celestial Lorekeeper"))
        with col_btn:
            st.write("")
            if st.button(self.profile.get_str("clear_chat_button", "✨ Resolve Astral Echoes"), width="stretch"):
                st.session_state.web_messages = [
                    {"role": "assistant", "content": self.profile.welcome_message}
                ]
                st.session_state.chat_cleared_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                st.rerun()

        for idx, msg in enumerate(st.session_state.web_messages):
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("tracks"):
                    self._render_track_cards(msg["tracks"], msg_index=idx)

        user_input = st.chat_input(self.profile.get_str("chat_input_placeholder", "Speak with Aurora..."))
        if user_input:
            st.session_state.web_messages.append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            web_uid = self.active_web_user_id

            if user_input.startswith("!profile "):
                facts = user_input.replace("!profile ", "").strip()
                self.db.update_l1_profile(web_uid, facts)
                reply = f"Celestial record inscribed into your profile: `{facts}`"
                with st.chat_message("assistant"):
                    st.markdown(reply)
                st.session_state.web_messages.append({"role": "assistant", "content": reply})
                st.rerun()

            if user_input.startswith("!note "):
                note_text = user_input.replace("!note ", "").strip()
                self.db.add_l2_episode(web_uid, "Web Note", note_text)
                reply = f"Episode anchored in long-term memory: `{note_text}`"
                with st.chat_message("assistant"):
                    st.markdown(reply)
                st.session_state.web_messages.append({"role": "assistant", "content": reply})
                st.rerun()

            strat_triggers = ["guide:", "mechanic:", "strategy:", "formula:"]
            if self.is_admin and self.profile.enable_auto_learn and any(k in user_input.lower() for k in strat_triggers):
                topic_key = user_input.split(":", 1)[0].strip()[:32]
                self.db.add_verified_strategy(
                    topic_key,
                    topic_key.title(),
                    user_input,
                    f"{self.profile.creator_name} (Web Direct)",
                    category="Sovereign Strategy",
                )
                st.toast(f"✨ Inscribed into Permanent Knowledge Records: {topic_key}")

            prompt_parts: List[str] = []

            if any(rt in user_input.lower() for rt in REMEMBER_TRIGGERS):
                deep_mem = self.db.get_deep_memory(web_uid)
                prompt_parts.append(f"[Archived Memory Records:\n{deep_mem}]")

            local_strat = self.db.query_local_docs(user_input, allowed_tiers=self.get_allowed_tiers())
            if local_strat:
                prompt_parts.append(local_strat)

            if any(s in user_input.lower() for s in SEARCH_TRIGGERS):
                with st.status("🌐 Cross-referencing trusted sources & multi-domain intelligence...", expanded=False):
                    research_text, domains = self.engine.execute_multi_source_research(user_input)
                    if research_text:
                        prompt_parts.append(research_text)
                        inscribed = self.engine.process_knowledge_ingestion(
                            user_input, research_text, domains, is_sovereign_user=self.is_admin
                        )
                        if inscribed:
                            st.toast(f"🌐 Auto-verified & inscribed: {inscribed}")

            found_tracks: List[Dict[str, Any]] = []
            if is_song_request(user_input):
                with st.status("🎵 Searching YouTube, Dailymotion, SoundCloud & open audio archives...", expanded=False):
                    try:
                        found_tracks = search_songs_hybrid(
                            user_input,
                            limit=4,
                            local_media=self.db.get_all_media(),
                        )
                    except Exception as song_err:
                        self.db.record_unresolved_issue("media_search", f"Media search error: {user_input[:60]}", str(song_err), self.profile.mode_key)
                        st.toast(f"Media search notice: {song_err}")

                if found_tracks:
                    track_lines = "\n".join(
                        f"- {t['title']} by {t['artist']} ({t['source_label']})"
                        for t in found_tracks
                    )
                    prompt_parts.append(
                        "[SYSTEM MEDIA ATTACHMENT — Interactive embedded video/audio cards with MP3 & MP4 download buttons "
                        "for ONLY these verified matches render directly below your reply:\n"
                        f"{track_lines}\n"
                        "STRICT RULE: Do NOT output raw URLs or guessed hyperlinks in your text reply. "
                        "Introduce the matched tracks in character and note that embedded video and MP3/MP4 download controls appear below.]"
                    )
                else:
                    self.db.record_unresolved_issue("media_no_match", f"No playable stream matched: {user_input[:80]}", user_input, self.profile.mode_key)
                    prompt_parts.append(
                        "[SYSTEM MEDIA NOTICE: No verified playable stream was found for this query. "
                        "STRICT RULE: Do NOT invent, guess, or output any URLs or links. Apologize briefly in character and ask for the exact title/artist or a direct video link.]"
                    )

            if self.is_admin:
                prompt_parts.append(
                    f"[Speaking directly to your beloved Creator and Sovereign, {self.profile.creator_name}. "
                    f"Keep your counsel sharp, warm, and natural without repeating her name unnecessarily.]"
                )

            prompt_parts.append(user_input)
            prompt_payload = "\n\n".join(prompt_parts)

            ltm_context = self.db.get_ltm_context(web_uid, user_input)
            prior_session_msgs = st.session_state.web_messages[:-1]
            if len(prior_session_msgs) <= 1:
                db_turns = self.db.get_active_user_context(
                    web_uid,
                    user_input,
                    timeout_minutes=120,
                    max_turns=12,
                    char_limit=1800,
                    since_timestamp=st.session_state.chat_cleared_at,
                )
                history_for_model = db_turns
            else:
                history_for_model = prior_session_msgs

            with st.chat_message("assistant"):
                with st.spinner(f"{self.profile.bot_name} is weaving a response..."):
                    web_reply = self.engine.call_gemini_sync(
                        prompt_payload=prompt_payload,
                        history_messages=history_for_model,
                        memory_context=ltm_context,
                    )
                    if found_tracks and "Media & Audio Notice:" not in web_reply:
                        web_reply = f"{web_reply}{AUDIO_OWNERSHIP_NOTICE}"

                    st.markdown(web_reply)
                    msg_idx = len(st.session_state.web_messages)
                    if found_tracks:
                        self._render_track_cards(found_tracks, msg_index=msg_idx)

                    assistant_record: Dict[str, Any] = {"role": "assistant", "content": web_reply}
                    if found_tracks:
                        assistant_record["tracks"] = found_tracks
                    st.session_state.web_messages.append(assistant_record)

                    self.db.save_chat(web_uid, "streamlit_web", "user", user_input)
                    self.db.save_chat(web_uid, "streamlit_web", "model", web_reply)

    def page_strategy_archives(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("archives_header", "Celestial Strategy & Fae Archives"))
        st.caption(self.profile.get_str("archives_caption", "Verified tactical records and Fae Season matrices."))

        docs = self.db.get_all_strategy_docs(allowed_tiers=self.get_allowed_tiers())
        if not docs:
            st.info("No records match your current access tier.")
            return

        col1, col2 = st.columns([2, 1])
        with col1:
            search_q = st.text_input("🔍 Filter by keyword, season, event, or content:", "")
        with col2:
            categories = ["All Categories"] + sorted(list({d["category"] for d in docs}))
            selected_cat = st.selectbox("📂 Category:", categories)

        filtered = [
            d for d in docs
            if (selected_cat == "All Categories" or d["category"] == selected_cat)
            and (
                not search_q
                or search_q.lower() in d["topic"].lower()
                or search_q.lower() in d["content"].lower()
                or search_q.lower() in d["keyword"].lower()
            )
        ]

        st.caption(f"Showing **{len(filtered)}** verified record(s) from `{self.db.db_basename}`.")
        for d in filtered:
            with st.expander(f"📌 **{d['topic']}**  —  `{d['category']}` | Tier: `{d['access_tier']}`"):
                st.markdown(d["content"])
                st.caption(f"Keyword: `{d['keyword']}` • Verified by: **{d['verified_by']}** • Updated: `{d['timestamp']}`")

    def page_domain_calculator(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("calc_header", "March of Synergy & Elemental Calculator"))
        st.caption(self.profile.get_str("calc_caption", "Interactive domain calculator."))
        self.profile.render_calculator()

    def page_lore_vault(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("vault_header", "Laughing Moon, Cosmic Trance & The Creator"))
        st.caption(self.profile.get_str("vault_caption", "Battle-tested wit, atmospheric transmissions, and the story of The Broken Star."))

        t_creator, t_humor, t_media = st.tabs([
            self.profile.get_str("creator_tab_title", "🌟 About the Creator (Aerith)"),
            self.profile.get_str("humor_tab_title", "🌙 Laughing Moon Wit"),
            self.profile.get_str("media_tab_title", "🎵 Cosmic Trance & Media"),
        ])

        with t_creator:
            with st.container(border=True):
                st.markdown(self.profile.about_creator_markdown)

        with t_humor:
            if st.button(self.profile.get_str("joke_button", "🌙 Draw Transmission from the Laughing Moon"), width="stretch"):
                st.success(f"💬 *\"{self.db.get_random_joke()}\"*")
            st.divider()
            jokes = self.db.get_all_jokes()
            for _, j_text, j_author in jokes[:15]:
                st.markdown(f"- {j_text} *(by {j_author})*")

        with t_media:
            media_items = self.db.get_all_media()
            for item in media_items:
                with st.expander(f"🎬 **{item['title']}** (`{item['category']}`)"):
                    st.write(item["description"])
                    if any(v in item["url"].lower() for v in ("youtube.com", "youtu.be", "dailymotion.com", "vimeo.com")):
                        st.video(item["url"])
                    else:
                        st.markdown(f"[🔗 Open Resource]({item['url']})")

    def page_sovereign_forge(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("forge_header", "Sovereign Knowledge Forge & Auto-Scout"))

        if self.profile.enable_auto_scout:
            with st.container(border=True):
                st.subheader("🔭 Autonomous King of Avalon Update Scout")
                st.caption(
                    f"Trusted Instant-Verify Domains: `{', '.join(self.profile.trusted_domains)}` • "
                    "Other web sources require 2+ domain corroboration."
                )
                c1, c2 = st.columns([2, 1])
                custom_scout_q = c1.text_input("Optional specific KoA topic to scout (leave blank for full sweep):", "")
                if c2.button("🚀 Run KoA Update Scout Now", width="stretch"):
                    with st.spinner("Scouting multi-source KoA intelligence and synthesizing guides..."):
                        ok, msg = self.engine.run_koa_auto_scout(custom_scout_q)
                        if ok:
                            st.success(msg)
                        else:
                            st.info(msg)
                st.caption(f"Last Scout Status: `{self.engine.last_koa_scout_status}`")

        tab_grid, tab_add, tab_jokes_media = st.tabs([
            "📊 Double-Click Strategy Grid Editor",
            "➕ Inscribe New Strategy Guide",
            "🌙 Double-Click Laughing Moon & Media Editor",
        ])

        with tab_grid:
            st.caption("💡 **Double-click any cell** below to edit keywords, titles, categories, access tiers, or strategy content.")
            docs = self.db.get_all_strategy_docs()
            df_docs = pd.DataFrame(docs) if docs else pd.DataFrame(columns=["keyword", "topic", "category", "access_tier", "content", "verified_by", "timestamp"])
            edited_df = st.data_editor(
                df_docs,
                num_rows="dynamic",
                width="stretch",
                column_config={
                    "access_tier": st.column_config.SelectboxColumn("Access Tier", options=self.profile.access_tiers, required=True),
                    "content": st.column_config.TextColumn("Strategy Content", width="large"),
                },
                key="strategy_docs_data_editor",
            )
            if st.button("💾 Save Strategy Grid Changes & Sync to Cloud Vaults", width="stretch"):
                self.db.sync_strategy_dataframe(edited_df)
                st.success("All table edits saved to SQLite and queued for GitHub + R2 sync!")
                st.rerun()

        with tab_add:
            with st.form("forge_doc_form"):
                f_kw = st.text_input("Unique Keyword Identifier (e.g. 'fae season 21', 'sigils'):")
                f_topic = st.text_input("Display Title / Topic:")
                c_cat, c_tier = st.columns(2)
                f_cat = c_cat.text_input("Category:", value="Fae Seasons")
                f_tier = c_tier.selectbox("Access Tier:", self.profile.access_tiers)
                f_content = st.text_area("Detailed Guide Content / Formulas:", height=160)
                if st.form_submit_button("💾 Inscribe Guide & Sync to Cloud Vaults", width="stretch"):
                    if f_kw and f_topic and f_content:
                        self.db.add_verified_strategy(
                            f_kw,
                            f_topic,
                            f_content,
                            verified_by=self.profile.creator_name,
                            category=f_cat,
                            access_tier=f_tier,
                        )
                        st.success(f"Saved '{f_topic}' and queued GitHub + R2 sync!")
                    else:
                        st.error("Please fill in Keyword, Title, and Content.")

        with tab_jokes_media:
            st.subheader("🌙 Laughing Moon Jokes / Tips (Double-Click to Edit)")
            jokes_rows = self.db.get_all_jokes()
            df_jokes = pd.DataFrame(jokes_rows, columns=["id", "joke", "added_by"]) if jokes_rows else pd.DataFrame(columns=["id", "joke", "added_by"])
            edited_jokes = st.data_editor(df_jokes, num_rows="dynamic", width="stretch", key="jokes_data_editor")
            if st.button("💾 Save Laughing Moon Table", width="stretch"):
                self.db.sync_jokes_dataframe(edited_jokes)
                st.success("Laughing Moon table updated!")
                st.rerun()

            st.divider()
            with st.form("add_media_form"):
                st.subheader("Add Multimedia Stream / YouTube / Dailymotion / Direct MP3-MP4 Link")
                m_title = st.text_input("Media Title:")
                m_cat = st.text_input("Category:", value="Music")
                m_url = st.text_input("Video or Direct Audio URL:")
                m_desc = st.text_input("Short Description:")
                if st.form_submit_button("Add Media Item", width="stretch"):
                    if m_title and m_url:
                        self.db.add_media(m_title, m_cat, m_url, m_desc, added_by=self.profile.creator_name)
                        st.success("Media added!")
                        st.rerun()

    def page_self_upgrade_studio(self) -> None:
        self.render_banner()
        st.header("🧬 Aurora Self-Upgrade Core & GitHub Actions CI/CD")
        st.caption(
            "Autonomous capability & mode synthesis, unresolved problem investigation, "
            "GitHub REST/GraphQL branch staging, and GitHub Actions CI/CD testing awaiting Sovereign approval."
        )

        gh_ok = self.engine.upgrader.gh.is_configured
        r2_ok = self.db.r2.is_configured
        c_stat1, c_stat2 = st.columns(2)
        c_stat1.info(f"🐙 **GitHub REST/GraphQL/Actions:** {'Connected (`' + self.engine.upgrader.gh.repo + '`)' if gh_ok else 'Set `GITHUB_TOKEN` in Secrets'}")
        c_stat2.info(f"🪣 **Cloudflare R2 Dated Archive:** {'Connected (`' + self.db.r2.bucket + '`)' if r2_ok else 'Set `R2_*` keys in Secrets'}")

        tab_synth, tab_testing, tab_issues = st.tabs([
            "🛠️ Synthesize New Capability or Mode",
            "🧪 In Testing & GitHub Actions CI/CD Review",
            "🩺 Unresolved Problems & Auto-Investigator",
        ])

        with tab_synth:
            with st.form("self_upgrade_synth_form"):
                st.subheader("Direct Aurora to Build & Test a New Module or Entire Mode")
                scope_choice = st.selectbox(
                    "Target Architecture Scope:",
                    [
                        "shared_capability (Available in ALL modes -> capabilities/)",
                        "mode_capability (Specific to one mode -> modes/<mode>/)",
                        "new_mode (Complete self-contained mode package -> modes/<new_mode>/)",
                    ],
                )
                scope_key = scope_choice.split(" ", 1)[0]
                target_mode_input = st.text_input(
                    "Target Mode Key (e.g. 'all', 'gaming', 'realtor', 'counselling'):",
                    value="all" if scope_key == "shared_capability" else "counselling",
                )
                spec_desc = st.text_area(
                    "Describe the capability or new mode Aurora should build, validate, and test:",
                    placeholder="Example: Build a Counselling & Wellness mode with a guided reflection journal and mood check-in calculator...",
                    height=130,
                )
                if st.form_submit_button("🚀 Synthesize, Validate & Push to GitHub CI/CD", width="stretch"):
                    if spec_desc.strip():
                        with st.spinner("Generating module code, verifying AST syntax, pushing branch, and dispatching GitHub Actions CI..."):
                            ok, msg = self.engine.synthesize_and_stage_upgrade(
                                request_description=spec_desc.strip(),
                                scope=scope_key,
                                target_mode=target_mode_input.strip() or "all",
                            )
                            if ok:
                                st.success(msg)
                            else:
                                st.error(msg)
                    else:
                        st.warning("Describe the capability or mode to synthesize.")

        with tab_testing:
            st.subheader("🧪 Modules In Development / In Testing (Awaiting Sovereign Review)")
            if st.button("🔄 Refresh GitHub GraphQL & Actions CI Status", key="refresh_ci_btn"):
                st.rerun()

            proposals = self.db.get_upgrade_proposals()
            if not proposals:
                st.info("No self-upgrade modules are currently staged in testing.")
            else:
                for prop in proposals:
                    with st.container(border=True):
                        st.markdown(f"### `{prop['title']}` — Status: **{prop['status']}**")
                        st.caption(
                            f"Scope: `{prop['scope']}` | Target Mode: `{prop['target_mode']}` | "
                            f"Branch: `{prop['branch_name']}` | Files: `{', '.join(prop['files'])}`"
                        )
                        if prop.get("changelog"):
                            st.write(prop["changelog"])

                        ci_info = self.engine.upgrader.gh.get_branch_ci_status(prop["branch_name"]) if gh_ok and prop["branch_name"] else {}
                        if ci_info:
                            st.caption(
                                f"⚙️ **GitHub Actions CI:** `{ci_info.get('status')}` / Conclusion: `{ci_info.get('conclusion')}` "
                                + (f"([View Run]({ci_info['html_url']}))" if ci_info.get("html_url") else "")
                            )

                        col_a, col_b, col_c = st.columns(3)
                        if prop.get("pr_url"):
                            col_a.link_button("🔗 View GitHub PR", prop["pr_url"], width="stretch")
                        if prop.get("pr_number") and gh_ok and prop["status"] != "Approved & Permanent":
                            if col_b.button("✅ Sovereign Approve & Merge to Main", key=f"merge_{prop['slug']}", width="stretch"):
                                m_ok, m_msg = self.engine.upgrader.gh.merge_pull_request(
                                    int(prop["pr_number"]),
                                    f"[Sovereign Approved] {prop['title']}",
                                )
                                if m_ok:
                                    self.db.update_proposal_status(prop["slug"], "Approved & Permanent", "passed")
                                    st.success(m_msg)
                                    st.rerun()
                                else:
                                    st.error(m_msg)
                        if col_c.button("🗑️ Mark Rejected / Archive", key=f"rej_{prop['slug']}", width="stretch"):
                            self.db.update_proposal_status(prop["slug"], "Archived by Sovereign")
                            st.rerun()

        with tab_issues:
            st.subheader("🩺 Logged Unresolved Problems & Autonomous Investigation")
            if st.button("🔎 Auto-Investigate Logged Problems & Synthesize Fix Now", width="stretch"):
                with st.spinner("Investigating unresolved issues & synthesizing solution module..."):
                    ok, msg = self.engine.investigate_and_propose_improvements()
                    if ok:
                        st.success(msg)
                    else:
                        st.info(msg)

            issues = self.db.get_unresolved_issues("all")
            if issues:
                st.dataframe(pd.DataFrame(issues), width="stretch")
            else:
                st.success("No unresolved issues currently recorded.")

    def page_mode_string_studio(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("studio_header", "Interactive Mode, String & Persona Studio"))
        st.caption(
            f"Editing Mode: **`{self.profile.mode_key}`** (Backed by `modes/{self.profile.mode_key}/profile.py` defaults)."
        )

        current_overrides = self.db.get_mode_override(self.profile.mode_key)

        tab_strings, tab_persona, tab_flags, tab_modes = st.tabs([
            "✏️ Displayed Site Strings & Headers",
            "👑 Persona, Aerith Relationship & Trusted Sources",
            "⚙️ Feature Toggles & Backup Interval",
            "🔄 Revert to Python File / Create Mode",
        ])

        with tab_strings:
            st.subheader(f"Header Banner & Displayed Text Strings (`{self.profile.mode_key}`)")
            updated_strings: Dict[str, str] = {}
            with st.form("ui_strings_editor_form"):
                b1, b2, b3 = st.columns(3)
                new_h_title = b1.text_input("Main Banner Header Title:", value=self.profile.header_title)
                new_h_sub = b2.text_input("Main Banner Subtitle:", value=self.profile.header_subtitle)
                new_gw_txt = b3.text_input("Banner Gateway Status Line:", value=self.profile.gateway_status_text)
                new_welcome = st.text_area("Initial Chat Welcome Greeting:", value=self.profile.welcome_message, height=80)

                st.divider()
                cols = st.columns(2)
                keys_sorted = sorted(self.profile.ui_strings.keys())
                for idx, s_key in enumerate(keys_sorted):
                    with cols[idx % 2]:
                        updated_strings[s_key] = st.text_input(
                            f"`{s_key}`",
                            value=self.profile.ui_strings[s_key],
                            key=f"str_edit_{s_key}",
                        )
                if st.form_submit_button("💾 Save All Displayed Strings to Database & Cloud Vaults", width="stretch"):
                    current_overrides["header_title"] = new_h_title
                    current_overrides["header_subtitle"] = new_h_sub
                    current_overrides["gateway_status_text"] = new_gw_txt
                    current_overrides["welcome_message"] = new_welcome
                    current_overrides["ui_strings"] = updated_strings
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Saved all displayed strings!")
                    st.rerun()

        with tab_persona:
            with st.form("persona_editor_form"):
                new_self_id = st.text_area("Self-Identity & Voice Description:", value=self.profile.self_identity_description, height=130)
                new_creator_rel = st.text_area("Creator (Aerith) Relationship Rules:", value=self.profile.creator_relationship_instruction, height=150)
                new_sys_inst = st.text_area("Core Tactical & Behavioral Directives:", value=self.profile.system_instruction, height=130)
                new_about_md = st.text_area("About the Creator Markdown:", value=self.profile.about_creator_markdown, height=130)
                domains_str = st.text_input("Trusted Verification Domains (comma-separated):", value=", ".join(self.profile.trusted_domains))

                if st.form_submit_button("💾 Save Persona & Trusted Sources", width="stretch"):
                    current_overrides["self_identity_description"] = new_self_id
                    current_overrides["creator_relationship_instruction"] = new_creator_rel
                    current_overrides["system_instruction"] = new_sys_inst
                    current_overrides["about_creator_markdown"] = new_about_md
                    current_overrides["trusted_domains"] = [d.strip() for d in domains_str.split(",") if d.strip()]
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Persona directives updated!")
                    st.rerun()

        with tab_flags:
            with st.form("feature_flags_form"):
                f1, f2, f3 = st.columns(3)
                en_disc = f1.checkbox("Enable Discord Gateway Thread", value=self.profile.enable_discord)
                en_voice = f1.checkbox("Enable Discord Chat Replies", value=self.profile.enable_discord_voice)
                en_learn = f2.checkbox("Enable Auto-Learn", value=self.profile.enable_auto_learn)
                en_scout = f2.checkbox("Enable KoA Auto-Scout", value=self.profile.enable_auto_scout)
                en_calc = f3.checkbox("Show Calculator Page", value=self.profile.enable_calculator)
                en_vault = f3.checkbox("Show Laughing Moon / Lore Page", value=self.profile.enable_lore_vault)

                st.divider()
                w1, w2, w3 = st.columns(3)
                en_ping = w1.checkbox("Enable Keep-Alive Self-Ping", value=self.profile.enable_keep_alive_ping)
                b_hours = w2.number_input("Auto-Backup Interval (Hours):", min_value=0.5, max_value=24.0, value=float(self.profile.backup_interval_hours), step=0.5)
                pub_url = w3.text_input("Public App URL:", value=self.profile.app_public_url)

                if st.form_submit_button("💾 Save Feature Toggles", width="stretch"):
                    current_overrides.update({
                        "enable_discord": en_disc,
                        "enable_discord_voice": en_voice,
                        "enable_auto_learn": en_learn,
                        "enable_auto_scout": en_scout,
                        "enable_calculator": en_calc,
                        "enable_lore_vault": en_vault,
                        "enable_keep_alive_ping": en_ping,
                        "backup_interval_hours": float(b_hours),
                        "app_public_url": pub_url.strip(),
                    })
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Feature flags saved!")
                    st.rerun()

        with tab_modes:
            col_rev, col_new = st.columns(2)
            with col_rev:
                with st.container(border=True):
                    st.subheader("🔄 Fallback to Python Mode File Defaults")
                    if st.button("🔄 Revert Mode to Python File Defaults Now", width="stretch"):
                        self.db.delete_mode_override(self.profile.mode_key)
                        self.profile.revert_to_python_defaults()
                        st.success(f"Reverted `{self.profile.mode_key}` to pristine Python defaults!")
                        st.rerun()

            with col_new:
                with st.container(border=True):
                    st.subheader("➕ Quick-Register Mode Override")
                    with st.form("create_new_mode_form"):
                        new_mode_slug = st.text_input("New Mode Key (e.g. 'counselling', 'fitness'):")
                        new_mode_title = st.text_input("New Mode Header Title:")
                        new_mode_bot = st.text_input("New Mode Bot Name:")
                        if st.form_submit_button("➕ Create Mode Override", width="stretch"):
                            clean_slug = new_mode_slug.lower().strip().replace(" ", "_")
                            if clean_slug:
                                DomainConfig.register_custom_mode(clean_slug, db=self.db)
                                self.db.save_mode_override(clean_slug, {
                                    "page_title": new_mode_title or f"{clean_slug.title()} AI",
                                    "header_title": new_mode_title or f"✨ {clean_slug.title()} AI",
                                    "bot_name": new_mode_bot or "Aurora",
                                    "enable_discord": False,
                                })
                                st.success(f"Created mode `{clean_slug}`! Switch to it in the sidebar.")
                                st.rerun()

    def page_memory_inspector(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("memory_header", "Celestial Memory & User Profile Inspector"))

        conn = self.db._get_conn()
        profiles_df = pd.read_sql_query("SELECT * FROM user_profiles ORDER BY updated_at DESC", conn)
        summaries_df = pd.read_sql_query("SELECT * FROM user_summaries ORDER BY last_updated DESC", conn)
        episodes_df = pd.read_sql_query("SELECT * FROM episodic_memory ORDER BY id DESC LIMIT 50", conn)
        chats_df = pd.read_sql_query("SELECT * FROM user_chat ORDER BY id DESC LIMIT 50", conn)
        conn.close()

        t1, t2, t3, t4 = st.tabs([
            "👤 L1 User Profiles (Double-Click Editable)",
            "📝 Rolling Summaries",
            "📌 L2 Episodic Memory",
            "💬 Recent Chat Logs",
        ])
        with t1:
            edited_profiles = st.data_editor(profiles_df, num_rows="dynamic", width="stretch", key="profiles_editor")
            if st.button("💾 Save User Profile Changes", width="stretch"):
                self.db.sync_profiles_dataframe(edited_profiles)
                st.success("User profiles updated!")
                st.rerun()
        with t2:
            st.dataframe(summaries_df, width="stretch")
        with t3:
            st.dataframe(episodes_df, width="stretch")
        with t4:
            st.dataframe(chats_df, width="stretch")

    def page_silence_controller(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("silence_header", "Channel Silence Veil & Hostility Ledger"))

        conn = self.db._get_conn()
        silence_df = pd.read_sql_query("SELECT * FROM silence_status", conn)
        abuse_df = pd.read_sql_query("SELECT * FROM abuse_ledger ORDER BY offense_count DESC", conn)
        conn.close()

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("🤫 Silenced Channels")
            st.dataframe(silence_df, width="stretch")
            ch_to_wake = st.text_input("Channel ID to Wake / Clear Silence:")
            if st.button("🔔 Lift Channel Silence", width="stretch") and ch_to_wake:
                self.db.clear_silence(ch_to_wake.strip())
                st.success(f"Cleared silence for channel {ch_to_wake}.")
                st.rerun()

        with c2:
            st.subheader("🛡️ Hostility & Abuse Ledger")
            st.dataframe(abuse_df, width="stretch")
            u_to_pardon = st.text_input("User ID to Pardon:")
            if st.button("🕊️ Pardon User Record", width="stretch") and u_to_pardon:
                self.db.pardon_user_abuse(u_to_pardon.strip())
                st.success(f"Pardoned user {u_to_pardon}.")
                st.rerun()

    def page_discord_broadcaster(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("broadcast_header", "Discord Voice & Resonance Broadcaster"))

        available_channels = self.discord_worker.get_available_channels()
        dropdown_options = [ch["label"] for ch in available_channels] + ["Custom Channel ID..."]

        col_sel, col_ref = st.columns([4, 1])
        with col_sel:
            selected_option = st.selectbox(
                "Select Target Discord Channel:",
                options=dropdown_options,
                key="discord_broadcaster_channel_select",
            )
        with col_ref:
            st.write("")
            st.write("")
            if st.button("🔄 Refresh", width="stretch", key="refresh_discord_channels_btn"):
                self.discord_worker._refresh_channels_cache_from_guilds()
                st.rerun()

        if selected_option == "Custom Channel ID...":
            col_name, col_id = st.columns(2)
            with col_id:
                target_ch = st.text_input(
                    "Enter Custom Discord Channel ID:",
                    placeholder="123456789012345678",
                    key="discord_broadcaster_custom_id",
                ).strip()
            resolved = self.discord_worker.resolve_channel_info(target_ch) if target_ch else {"guild": "", "name": ""}
            display_name = (
                f"{resolved['guild']} / {resolved['name']}"
                if resolved.get("name")
                else "Enter a Channel ID to look up name"
            )
            with col_name:
                st.text_input(
                    "Channel Name:",
                    value=display_name,
                    disabled=True,
                    key="discord_broadcaster_custom_name_display",
                )
        else:
            matched = next((ch for ch in available_channels if ch["label"] == selected_option), None)
            target_ch = matched["id"] if matched else ""
            channel_display_name = (
                f"{matched['guild']} / {matched['name']}" if matched else "Unknown Channel"
            )
            col_name, col_id = st.columns(2)
            with col_name:
                st.text_input(
                    "Channel Name:",
                    value=channel_display_name,
                    disabled=True,
                    key="discord_broadcaster_resolved_name",
                )
            with col_id:
                st.text_input(
                    "Channel ID:",
                    value=target_ch,
                    disabled=True,
                    key="discord_broadcaster_resolved_id",
                )

        if "broadcast_draft_text" not in st.session_state:
            st.session_state.broadcast_draft_text = ""

        raw_input = st.text_area(
            "Announcement Message (Write plain notes or final text):",
            value=st.session_state.broadcast_draft_text,
            height=150,
        )

        c_draft, c_send = st.columns(2)
        if c_draft.button(f"✨ Draft in {self.profile.bot_name}'s Voice", width="stretch"):
            if raw_input.strip():
                with st.spinner(f"Infusing {self.profile.bot_name}'s voice..."):
                    st.session_state.broadcast_draft_text = self.engine.draft_in_bot_voice(raw_input)
                    st.rerun()
            else:
                st.warning("Write a draft message first so Aurora can style it.")

        if c_send.button("📡 Send Broadcast to Discord", width="stretch"):
            if target_ch and raw_input.strip():
                ok, status = self.discord_worker.send_broadcast(target_ch.strip(), raw_input.strip())
                if ok:
                    st.success(status)
                else:
                    st.error(status)
            else:
                st.warning("Select a valid channel and enter a message.")

    def _build_changelog_markdown(self) -> str:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        proposals = self.db.get_upgrade_proposals()
        issues = self.db.get_unresolved_issues("all")
        lines = [
            f"# Aurora Sovereign System Snapshot & Changelog ({now_str})",
            f"- **Active Mode:** `{self.profile.mode_key}` | **Database:** `{self.db.db_basename}`",
            "",
            "## Self-Upgrade & Enhancement History",
        ]
        if proposals:
            for p in proposals[:20]:
                lines.append(f"- **{p['title']}** (`{p['status']}`) — Branch: `{p['branch_name']}` | {p['changelog']}")
        else:
            lines.append("- No staged upgrade proposals recorded yet.")

        lines.append("\n## Diagnostic & Resolved Issues Log")
        if issues:
            for iss in issues[:20]:
                lines.append(f"- [{iss['status'].upper()}] `{iss['category']}`: {iss['summary']}")
        else:
            lines.append("- Zero unresolved issues logged.")
        return "\n".join(lines)

    def page_database_vault(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("db_header", "Database Vault, Cloudflare R2 & Cloud Sync"))
        st.caption(
            f"Active Mode: `{self.profile.mode_key}` • Isolated Database File: `{self.db.db_basename}` • "
            f"Scheduled Auto-Backup Every **{self.profile.backup_interval_hours}h**"
        )

        with st.container(border=True):
            st.subheader("🪣 Cloudflare R2 Sovereign Vault (`YYYY/MM/DD` Organized)")
            if self.db.r2.is_configured:
                st.success(
                    f"✅ Connected to Cloudflare R2 Bucket: **`{self.db.r2.bucket}`** — "
                    f"Stores Sovereign MP3/MP4 media (`media/YYYY/MM/DD/`), databases, full source zips, and changelogs (`backups/YYYY/MM/DD/`)."
                )
                r1, r2 = st.columns(2)
                if r1.button("🚀 Run Full Dated R2 Backup Now (DB + Source ZIP + Changelog)", width="stretch"):
                    with st.spinner("Uploading dated snapshot to Cloudflare R2 (`backups/YYYY/MM/DD/`)..."):
                        db_map = {self.db.db_basename: self.db.export_db_bytes()}
                        zip_bytes = self._build_project_zip_bytes()
                        changelog_md = self._build_changelog_markdown()
                        ok_b, msg_b = self.db.r2.run_full_dated_backup(db_map, zip_bytes, changelog_md)
                        if ok_b:
                            st.success(msg_b)
                        else:
                            st.error(msg_b)

                if r2.button("⬇️ Restore Latest DB from Cloudflare R2 (`backups/latest/`)", width="stretch"):
                    with st.spinner("Fetching latest database from Cloudflare R2..."):
                        ok_d, raw_d, msg_d = self.db.r2.download_bytes(f"backups/latest/{self.db.db_basename}")
                        if ok_d and raw_d:
                            r_ok, r_msg = self.db.restore_from_bytes(raw_d)
                            if r_ok:
                                st.success(f"{msg_d} -> {r_msg}")
                            else:
                                st.error(r_msg)
                        else:
                            st.warning(msg_d)

                r2_media = self.db.get_r2_media_archive()
                if r2_media:
                    with st.expander(f"🎵 Sovereign R2 Media Archive ({len(r2_media)} items)", expanded=False):
                        st.dataframe(pd.DataFrame(r2_media), width="stretch")
            else:
                st.info(
                    "💡 Add `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_BUCKET_NAME` to Secrets "
                    "to enable automatic `YYYY/MM/DD` Cloudflare R2 archival for all Sovereign MP3/MP4 downloads, DBs, and changelogs."
                )

        with st.container(border=True):
            st.subheader("☁️ GitHub `db-vault` Branch Auto-Persistence")
            if self.db.vault.is_configured:
                st.success(
                    f"✅ Connected to GitHub Repo: **`{self.db.vault.repo}`** (Branch: **`{self.db.vault.branch}`**) — "
                    f"Last Status: `{self.db.vault.last_sync_status}`"
                )
                g1, g2 = st.columns(2)
                if g1.button("⬆️ Push Database to GitHub Vault Now", width="stretch"):
                    with st.spinner("Snapshotting SQLite & pushing to GitHub `db-vault` branch..."):
                        raw = self.db.export_db_bytes()
                        ok, msg = self.db.vault.push_bytes_to_vault(
                            f"backups/{self.db.db_basename}",
                            raw,
                            f"Manual Admin Backup ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
                        )
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

                if g2.button("⬇️ Pull & Restore Latest Database from GitHub Vault", width="stretch"):
                    with st.spinner("Downloading latest backup from GitHub `db-vault` branch..."):
                        tmp_pull = f"pull_{self.db.db_basename}"
                        ok, msg = self.db.vault.pull_file_from_vault(f"backups/{self.db.db_basename}", tmp_pull)
                        if ok and os.path.exists(tmp_pull):
                            with open(tmp_pull, "rb") as f:
                                pulled_bytes = f.read()
                            os.remove(tmp_pull)
                            r_ok, r_msg = self.db.restore_from_bytes(pulled_bytes)
                            if r_ok:
                                st.success(f"{msg} -> {r_msg}")
                            else:
                                st.error(r_msg)
                        else:
                            st.warning(msg)
            else:
                st.warning(
                    "⚠️ `GITHUB_TOKEN` is not yet set in Secrets. Add `GITHUB_TOKEN` and `GITHUB_REPO` to enable GitHub vault persistence."
                )

        col_dl, col_ul = st.columns(2)
        with col_dl:
            with st.container(border=True):
                st.subheader("📥 Download Live SQLite Backup")
                db_bytes = self.db.export_db_bytes()
                st.download_button(
                    label=f"💾 Download {self.db.db_basename} ({len(db_bytes):,} bytes)",
                    data=db_bytes,
                    file_name=f"backup_{self.db.db_basename}",
                    mime="application/octet-stream",
                    key="stable_sqlite_download_btn",
                    width="stretch",
                )

        with col_ul:
            with st.container(border=True):
                st.subheader("📤 Restore SQLite Backup (.db)")
                uploaded_db = st.file_uploader("Upload a `.db` backup file:", type=["db", "sqlite", "sqlite3"])
                if uploaded_db is not None:
                    if st.button("🔄 Hot-Swap & Restore Uploaded Database", width="stretch"):
                        ok, msg = self.db.restore_from_bytes(uploaded_db.getvalue())
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

        with st.container(border=True):
            st.subheader("📦 Export Complete Modular Project (.zip)")
            zip_bytes = self._build_project_zip_bytes()
            st.download_button(
                label="📦 Download Aurora-KoA-Modular.zip",
                data=zip_bytes,
                file_name="Aurora-KoA-Modular.zip",
                mime="application/zip",
                key="download_full_project_zip_btn",
                width="stretch",
            )

    def _build_project_zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        for root_dir, _, files in os.walk("."):
            if any(skip in root_dir for skip in (".git", "__pycache__", ".venv", "venv", "media_vault")):
                continue
            for fname in files:
                if fname.endswith((".db", ".db-wal", ".db-shm", ".zip")):
                    continue
                full_path = os.path.join(root_dir, fname)
                rel_path = os.path.relpath(full_path, ".").replace("\\", "/")
                buf_arcname = f"Aurora-KoA-main/{rel_path}"
                with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
                    if buf_arcname not in zf.namelist():
                        zf.write(full_path, arcname=buf_arcname)
        buf.seek(0)
        return buf.read()

    def run(self) -> None:
        self.render_sidebar_auth()

        public_pages = [
            st.Page(self.page_resonance_chat, title=self.profile.get_str("chat_nav", "💬 Reasonable Chat"), default=True),
            st.Page(self.page_strategy_archives, title=self.profile.get_str("archives_nav", "📜 Strategies & Lore")),
        ]
        if self.profile.enable_calculator:
            public_pages.append(st.Page(self.page_domain_calculator, title=self.profile.get_str("calc_nav", "⚔️ March of Synergy")))
        if self.profile.enable_lore_vault:
            public_pages.append(st.Page(self.page_lore_vault, title=self.profile.get_str("vault_nav", "🌙 Laughing Moon & Creator")))

        public_group_label = self.profile.get_str("public_menu_title", "Public Sanctum")
        admin_group_label = self.profile.get_str("admin_menu_title", "Sovereign Command")

        nav_sections: Dict[str, List[Any]] = {public_group_label: public_pages}

        if self.is_admin:
            admin_pages = [
                st.Page(self.page_self_upgrade_studio, title="🧬 Self-Upgrade & CI/CD Core"),
                st.Page(self.page_sovereign_forge, title=self.profile.get_str("forge_nav", "🔥 Sovereign Knowledge Forge")),
                st.Page(self.page_mode_string_studio, title=self.profile.get_str("studio_nav", "🎨 Mode, String & Persona Studio")),
                st.Page(self.page_memory_inspector, title=self.profile.get_str("memory_nav", "🧠 Celestial Memory Inspector")),
                st.Page(self.page_database_vault, title=self.profile.get_str("db_nav", "🗄️ Database & R2 Vault")),
            ]
            if self.profile.enable_discord:
                admin_pages.append(st.Page(self.page_silence_controller, title=self.profile.get_str("silence_nav", "🤫 Silence Veil & Abuse Ledger")))
                admin_pages.append(st.Page(self.page_discord_broadcaster, title=self.profile.get_str("broadcast_nav", "📢 Discord Voice Broadcaster")))
            nav_sections[admin_group_label] = admin_pages

        pg = st.navigation(nav_sections)
        pg.run()
