"""
web_ui.py
Encapsulates the Streamlit multi-page application, separated Alliance (top) & Sovereign (bottom)
sidebar authentication, customized Gaming/Realtor titles & strings, double-click st.data_editor tables,
the Interactive Mode, String & Persona Studio (with 1-click fallback to Python mode files),
"Draft in Aurora's Voice" Discord broadcaster, and 1-click Project .zip Exporter.
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
from ai_engine import GeminiEngine, SEARCH_TRIGGERS
from discord_worker import DiscordWorker


class StreamlitUI:
    """
    Object-Oriented Streamlit UI Controller.
    Adapts branding, navigation, calculators, and admin studios dynamically based on BotProfile.
    """

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

    @property
    def is_admin(self) -> bool:
        return st.session_state.auth_role == "Sovereign"

    @property
    def is_member(self) -> bool:
        return st.session_state.auth_role in ("Member", "Sovereign")

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

    def render_sidebar_auth(self) -> None:
        """
        Renders the Sidebar with Alliance Key Access at the top and
        Sovereign Sanctum Login separated cleanly at the bottom.
        """
        with st.sidebar:
            st.subheader(self.profile.get_str("sidebar_header", "Identity & Resonance"))

            if st.session_state.auth_role == "Guest":
                st.info(self.profile.get_str("guest_badge", "Browsing Public Sanctum as Guest"))

                # TOP SECTION: Alliance Key Access
                with st.container(border=True):
                    st.markdown(f"**{self.profile.get_str('alliance_section_title', '🛡️ Alliance Key Access')}**")
                    alliance_pass = st.text_input(
                        self.profile.get_str("alliance_login_prompt", "Enter Alliance Resonance Key:"),
                        type="password",
                        key="sidebar_alliance_pass"
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

                # BOTTOM SECTION: Sovereign Sanctum Login
                with st.expander(self.profile.get_str("sovereign_section_title", "👑 Sovereign Sanctum Login"), expanded=False):
                    sov_pass = st.text_input(
                        self.profile.get_str("sovereign_login_prompt", "Enter Sovereign Creator Key:"),
                        type="password",
                        key="sidebar_sovereign_pass"
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

                # Allow Member to elevate to Sovereign from bottom expander if needed
                if not self.is_admin:
                    with st.expander(self.profile.get_str("sovereign_section_title", "👑 Sovereign Sanctum Login"), expanded=False):
                        sov_pass = st.text_input(
                            self.profile.get_str("sovereign_login_prompt", "Enter Sovereign Creator Key:"),
                            type="password",
                            key="sidebar_sovereign_elevate"
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

            # Admin Quick Mode Switcher in Sidebar
            if self.is_admin:
                st.divider()
                st.caption("🎛️ **Admin Mode Switcher**")
                custom_keys = self.db.get_all_custom_mode_keys()
                modes = DomainConfig.get_available_modes(custom_keys)
                current_idx = modes.index(self.profile.mode_key) if self.profile.mode_key in modes else 0
                selected_mode = st.selectbox(
                    "Active Deployment Mode:",
                    options=modes,
                    index=current_idx,
                    key="sidebar_mode_select"
                )
                if selected_mode != self.profile.mode_key:
                    st.session_state.session_app_mode_override = selected_mode
                    st.rerun()

                if self.db.vault.is_configured:
                    st.caption(f"☁️ **GitHub Vault:** `{self.db.vault.branch}`")
                    st.caption(f"Status: {self.db.vault.last_sync_status}")

    # =========================================================================
    # PAGE 1: CELESTIAL RESONANCE CHAMBER (Chat)
    # =========================================================================
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
                st.rerun()

        for msg in st.session_state.web_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        user_input = st.chat_input(self.profile.get_str("chat_input_placeholder", "Speak with Aurora..."))
        if user_input:
            st.session_state.web_messages.append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            strat_triggers = ["guide:", "mechanic:", "strategy:", "formula:"]
            if self.is_admin and self.profile.enable_auto_learn and any(k in user_input.lower() for k in strat_triggers):
                topic_key = user_input.split(":", 1)[0].strip()[:32]
                self.db.add_verified_strategy(
                    topic_key,
                    topic_key.title(),
                    user_input,
                    f"{self.profile.creator_name} (Web Direct)",
                    category="Sovereign Strategy"
                )
                st.toast(f"✨ Inscribed into Permanent Knowledge Records: {topic_key}")

            local_strat = self.db.query_local_docs(user_input, allowed_tiers=self.get_allowed_tiers())
            prompt_payload = f"{local_strat}\n{user_input}" if local_strat else user_input

            if any(s in user_input.lower() for s in SEARCH_TRIGGERS):
                with st.status("🌐 Cross-referencing trusted sources & multi-domain intelligence...", expanded=False):
                    research_text, domains = self.engine.execute_multi_source_research(user_input)
                    if research_text:
                        prompt_payload = f"{research_text}\n\n{prompt_payload}"
                        inscribed = self.engine.process_knowledge_ingestion(
                            user_input, research_text, domains, is_sovereign_user=self.is_admin
                        )
                        if inscribed:
                            st.toast(f"🌐 Auto-verified & inscribed: {inscribed}")

            if self.is_admin:
                prompt_payload = (
                    f"[Speaking directly to your beloved Creator and Sovereign, {self.profile.creator_name}! "
                    f"Recognize her with genuine excitement, warmth, and luminous pride while keeping your counsel sharp]:\n"
                    f"{prompt_payload}"
                )

            with st.chat_message("assistant"):
                with st.spinner(f"{self.profile.bot_name} is weaving a response..."):
                    web_reply = self.engine.call_gemini_sync(prompt_payload)
                    st.markdown(web_reply)
                    st.session_state.web_messages.append({"role": "assistant", "content": web_reply})
                    self.db.save_chat("web_user", "streamlit_web", "user", user_input)
                    self.db.save_chat("web_user", "streamlit_web", "model", web_reply)

    # =========================================================================
    # PAGE 2: STRATEGIES & LORE ARCHIVES
    # =========================================================================
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
            search_q = st.text_input("🔍 Filter by keyword, Fae season, event, or content:", "")
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

        st.caption(f"Showing **{len(filtered)}** verified record(s).")
        for d in filtered:
            with st.expander(f"📌 **{d['topic']}**  —  `{d['category']}` | Tier: `{d['access_tier']}`"):
                st.markdown(d["content"])
                st.caption(f"Keyword: `{d['keyword']}` • Verified by: **{d['verified_by']}** • Updated: `{d['timestamp']}`")

    # =========================================================================
    # PAGE 3: MARCH OF SYNERGY / MORTGAGE CALCULATOR
    # =========================================================================
    def page_domain_calculator(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("calc_header", "March of Synergy & Elemental Calculator"))
        st.caption(self.profile.get_str("calc_caption", "Interactive formation and synergy calculator."))

        if self.profile.calculator_type == "realtor_mortgage":
            self._render_realtor_mortgage_calculator()
        else:
            self._render_gaming_march_calculator()

    def _render_gaming_march_calculator(self) -> None:
        col1, col2 = st.columns(2)
        with col1:
            march_cap = st.number_input("Total March Capacity (Troops):", min_value=1000, max_value=10000000, value=350000, step=10000)
            preset = st.selectbox(
                "Tactical Formation Preset:",
                [
                    "Standard Balanced Rally (45% Inf / 27% Cav / 28% Bow)",
                    "Heavy Sanctum / Garrison Anchor (55% Inf / 20% Cav / 25% Bow)",
                    "High-Burst Flank Strike (38% Inf / 35% Cav / 27% Bow)",
                    "Golem / Single-Target Boss (1% Inf / 44% Cav / 55% Bow)",
                    "Custom Ratio"
                ]
            )
        with col2:
            my_element = st.selectbox("Your Primary Planar Element:", ["Fire 🔥", "Ice ❄️", "Wind 🌪️", "Earth 🪨"])
            enemy_element = st.selectbox("Target / Enemy Element:", ["Fire 🔥", "Ice ❄️", "Wind 🌪️", "Earth 🪨"])

        if "Standard" in preset:
            inf_pct, cav_pct, bow_pct = 45, 27, 28
        elif "Heavy" in preset:
            inf_pct, cav_pct, bow_pct = 55, 20, 25
        elif "High-Burst" in preset:
            inf_pct, cav_pct, bow_pct = 38, 35, 27
        elif "Golem" in preset:
            inf_pct, cav_pct, bow_pct = 1, 44, 55
        else:
            c1, c2, c3 = st.columns(3)
            inf_pct = c1.slider("Infantry %", 1, 85, 45)
            cav_pct = c2.slider("Cavalry %", 1, 80, 27)
            bow_pct = max(0, 100 - inf_pct - cav_pct)
            c3.metric("Bowmen % (Auto-balanced)", f"{bow_pct}%")

        inf_count = int(march_cap * (inf_pct / 100.0))
        cav_count = int(march_cap * (cav_pct / 100.0))
        bow_count = march_cap - inf_count - cav_count

        st.subheader("🛡️ Computed Troop Allocation (Primary Tier vs. Cushion Tier)")
        m1, m2, m3 = st.columns(3)
        m1.metric("🛡️ Infantry Frontline", f"{inf_count:,}", f"{inf_pct}% (Main: {int(inf_count*0.75):,} | Cushion: {int(inf_count*0.25):,})")
        m2.metric("🐎 Cavalry Flank", f"{cav_count:,}", f"{cav_pct}% (Main: {int(cav_count*0.75):,} | Cushion: {int(cav_count*0.25):,})")
        m3.metric("🏹 Bowmen Backline", f"{bow_count:,}", f"{bow_pct}% (Main: {int(bow_count*0.80):,} | Cushion: {int(bow_count*0.20):,})")

        counters = {"Ice ❄️": "Fire 🔥", "Fire 🔥": "Wind 🌪️", "Wind 🌪️": "Earth 🪨", "Earth 🪨": "Ice ❄️"}
        if counters.get(my_element) == enemy_element:
            st.success(f"🌟 **Elemental Advantage Active ({my_element} counters {enemy_element}):** +15% Planar Damage & Mitigation resonance!")
        elif counters.get(enemy_element) == my_element:
            st.warning(f"⚠️ **Elemental Disadvantage ({enemy_element} counters {my_element}):** Increase Infantry frontline cushion by +5% to absorb burst.")
        else:
            st.info(f"⚖️ **Neutral Elemental Matchup ({my_element} vs {enemy_element}):** Standard lethality multipliers apply.")

    def _render_realtor_mortgage_calculator(self) -> None:
        col1, col2, col3 = st.columns(3)
        with col1:
            home_price = st.number_input("Home Purchase Price ($):", min_value=50000, max_value=25000000, value=450000, step=10000)
            down_pct = st.slider("Down Payment (%):", min_value=0, max_value=60, value=20, step=1)
            down_amount = home_price * (down_pct / 100.0)
            st.caption(f"Down Payment: **${down_amount:,.0f}** | Loan Principal: **${home_price - down_amount:,.0f}**")
        with col2:
            interest_rate = st.number_input("Annual Interest Rate (%):", min_value=1.0, max_value=15.0, value=6.25, step=0.125)
            loan_term_years = st.selectbox("Loan Term (Years):", [30, 20, 15, 10], index=0)
        with col3:
            prop_tax_rate = st.number_input("Annual Property Tax Rate (%):", min_value=0.0, max_value=5.0, value=1.15, step=0.05)
            annual_ins = st.number_input("Annual Homeowner's Insurance ($):", min_value=0, max_value=25000, value=1500, step=100)
            monthly_hoa = st.number_input("Monthly HOA / Condo Dues ($):", min_value=0, max_value=5000, value=0, step=25)

        loan_amount = home_price - down_amount
        n_months = loan_term_years * 12
        monthly_r = (interest_rate / 100.0) / 12.0

        if monthly_r > 0 and loan_amount > 0:
            monthly_pi = loan_amount * (monthly_r * (1 + monthly_r) ** n_months) / ((1 + monthly_r) ** n_months - 1)
        else:
            monthly_pi = loan_amount / n_months if n_months else 0.0

        monthly_tax = (home_price * (prop_tax_rate / 100.0)) / 12.0
        monthly_ins = annual_ins / 12.0
        monthly_pmi = ((loan_amount * 0.0055) / 12.0) if down_pct < 20 else 0.0
        total_monthly = monthly_pi + monthly_tax + monthly_ins + monthly_pmi + monthly_hoa

        st.subheader(f"💵 Estimated Monthly Payment: **${total_monthly:,.2f} / month**")
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Principal & Interest", f"${monthly_pi:,.2f}")
        k2.metric("Property Taxes", f"${monthly_tax:,.2f}")
        k3.metric("Home Insurance", f"${monthly_ins:,.2f}")
        k4.metric("Estimated PMI", f"${monthly_pmi:,.2f}", "0% at 20%+ down" if down_pct >= 20 else "Required (<20% down)")
        k5.metric("HOA Dues", f"${monthly_hoa:,.2f}")

    # =========================================================================
    # PAGE 4: LAUGHING MOON & THE CREATOR
    # =========================================================================
    def page_lore_vault(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("vault_header", "Laughing Moon, Cosmic Trance & The Creator"))
        st.caption(self.profile.get_str("vault_caption", "Battle-tested wit, atmospheric transmissions, and the story of The Broken Star."))

        t_creator, t_humor, t_media = st.tabs([
            self.profile.get_str("creator_tab_title", "🌟 About the Creator (Aerith)"),
            self.profile.get_str("humor_tab_title", "🌙 Laughing Moon Wit"),
            self.profile.get_str("media_tab_title", "🎵 Cosmic Trance & Media")
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
                    if "youtube.com" in item["url"] or "youtu.be" in item["url"]:
                        st.video(item["url"])
                    else:
                        st.markdown(f"[🔗 Open Resource]({item['url']})")

    # =========================================================================
    # PAGE 5: SOVEREIGN KNOWLEDGE FORGE (WITH DOUBLE-CLICK DATA_EDITOR)
    # =========================================================================
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
            "🌙 Double-Click Laughing Moon & Media Editor"
        ])

        with tab_grid:
            st.caption("💡 **Double-click any cell** below to edit keywords, titles, categories, access tiers, or strategy content. You can also add or delete rows, then click **Save Table Changes**.")
            docs = self.db.get_all_strategy_docs()
            df_docs = pd.DataFrame(docs) if docs else pd.DataFrame(columns=["keyword", "topic", "category", "access_tier", "content", "verified_by", "timestamp"])
            edited_df = st.data_editor(
                df_docs,
                num_rows="dynamic",
                width="stretch",
                column_config={
                    "access_tier": st.column_config.SelectboxColumn("Access Tier", options=self.profile.access_tiers, required=True),
                    "content": st.column_config.TextColumn("Strategy Content", width="large")
                },
                key="strategy_docs_data_editor"
            )
            if st.button("💾 Save Strategy Grid Changes & Sync to GitHub Vault", width="stretch"):
                self.db.sync_strategy_dataframe(edited_df)
                st.success("All table edits saved to SQLite and queued for GitHub Vault sync!")
                st.rerun()

        with tab_add:
            with st.form("forge_doc_form"):
                f_kw = st.text_input("Unique Keyword Identifier (e.g. 'fae season 21', 'sigils'):")
                f_topic = st.text_input("Display Title / Topic:")
                c_cat, c_tier = st.columns(2)
                f_cat = c_cat.text_input("Category:", value="Fae Seasons")
                f_tier = c_tier.selectbox("Access Tier:", self.profile.access_tiers)
                f_content = st.text_area("Detailed Guide Content / Formulas:", height=160)
                if st.form_submit_button("💾 Inscribe Guide & Sync to GitHub Vault", width="stretch"):
                    if f_kw and f_topic and f_content:
                        self.db.add_verified_strategy(
                            f_kw, f_topic, f_content,
                            verified_by=self.profile.creator_name,
                            category=f_cat, access_tier=f_tier
                        )
                        st.success(f"Saved '{f_topic}' and queued GitHub Vault sync!")
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
                st.subheader("Add Multimedia Stream / Link")
                m_title = st.text_input("Media Title:")
                m_cat = st.text_input("Category:", value="Music")
                m_url = st.text_input("YouTube or Resource URL:")
                m_desc = st.text_input("Short Description:")
                if st.form_submit_button("Add Media Item", width="stretch"):
                    if m_title and m_url:
                        self.db.add_media(m_title, m_cat, m_url, m_desc, added_by=self.profile.creator_name)
                        st.success("Media added!")
                        st.rerun()

    # =========================================================================
    # PAGE 6: MODE, STRING & PERSONA STUDIO (WITH PYTHON FILE FALLBACK)
    # =========================================================================
    def page_mode_string_studio(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("studio_header", "Interactive Mode, String & Persona Studio"))
        st.caption(
            f"Editing Mode: **`{self.profile.mode_key}`** (Backed by `mode_{self.profile.mode_key}.py` defaults). "
            "All edits save into SQLite (`mode_overrides` table) and sync to GitHub `db-vault`. "
            "You can revert to the pristine Python file defaults at any time with 1 click."
        )

        current_overrides = self.db.get_mode_override(self.profile.mode_key)

        tab_strings, tab_persona, tab_flags, tab_modes = st.tabs([
            "✏️ Displayed Site Strings & Headers",
            "👑 Persona, Aerith Relationship & Trusted Sources",
            "⚙️ Feature Toggles & Backup Interval",
            "🔄 Revert to Python File / Create Mode"
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
                st.markdown("##### Navigation Titles, Buttons & Section Labels")
                cols = st.columns(2)
                keys_sorted = sorted(self.profile.ui_strings.keys())
                for idx, s_key in enumerate(keys_sorted):
                    with cols[idx % 2]:
                        updated_strings[s_key] = st.text_input(
                            f"`{s_key}`",
                            value=self.profile.ui_strings[s_key],
                            key=f"str_edit_{s_key}"
                        )
                if st.form_submit_button("💾 Save All Displayed Strings to Database & GitHub Vault", width="stretch"):
                    current_overrides["header_title"] = new_h_title
                    current_overrides["header_subtitle"] = new_h_sub
                    current_overrides["gateway_status_text"] = new_gw_txt
                    current_overrides["welcome_message"] = new_welcome
                    current_overrides["ui_strings"] = updated_strings
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Saved all displayed strings to SQLite and synced to GitHub Vault!")
                    st.rerun()

        with tab_persona:
            st.subheader("How Aurora Sees Herself, Relates to Aerith & Verifies Sources")
            with st.form("persona_editor_form"):
                new_self_id = st.text_area(
                    "Self-Identity & Voice Description (How she sees herself & speaks):",
                    value=self.profile.self_identity_description,
                    height=130
                )
                new_creator_rel = st.text_area(
                    "Creator (Aerith) Relationship Rules (Unprompted restraint vs. excitement when speaking to Aerith):",
                    value=self.profile.creator_relationship_instruction,
                    height=150
                )
                new_sys_inst = st.text_area(
                    "Core Tactical & Behavioral Directives:",
                    value=self.profile.system_instruction,
                    height=130
                )
                new_about_md = st.text_area(
                    "About the Creator (Aerith) Markdown (Displayed on Laughing Moon & Creator page):",
                    value=self.profile.about_creator_markdown,
                    height=130
                )
                domains_str = st.text_input(
                    "Trusted Verification Domains (comma-separated, requires only 1 hit to auto-verify):",
                    value=", ".join(self.profile.trusted_domains)
                )

                if st.form_submit_button("💾 Save Persona, Creator Rules & Trusted Sources", width="stretch"):
                    current_overrides["self_identity_description"] = new_self_id
                    current_overrides["creator_relationship_instruction"] = new_creator_rel
                    current_overrides["system_instruction"] = new_sys_inst
                    current_overrides["about_creator_markdown"] = new_about_md
                    current_overrides["trusted_domains"] = [d.strip() for d in domains_str.split(",") if d.strip()]
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Persona & Creator relationship directives updated!")
                    st.rerun()

        with tab_flags:
            st.subheader("Feature Toggles, Automation & Watchdog Settings")
            with st.form("feature_flags_form"):
                f1, f2, f3 = st.columns(3)
                en_disc = f1.checkbox("Enable Discord Gateway Thread", value=self.profile.enable_discord)
                en_voice = f1.checkbox("Enable Discord Chat Replies", value=self.profile.enable_discord_voice)
                en_learn = f2.checkbox("Enable Auto-Learn (Reactions & Web)", value=self.profile.enable_auto_learn)
                en_scout = f2.checkbox("Enable KoA Auto-Scout", value=self.profile.enable_auto_scout)
                en_calc = f3.checkbox("Show Calculator Page", value=self.profile.enable_calculator)
                en_vault = f3.checkbox("Show Laughing Moon / Lore Page", value=self.profile.enable_lore_vault)

                st.divider()
                w1, w2, w3 = st.columns(3)
                en_ping = w1.checkbox("Enable Keep-Alive Self-Ping", value=self.profile.enable_keep_alive_ping)
                b_hours = w2.number_input(
                    "Auto-Backup Interval (Hours):",
                    min_value=0.5, max_value=24.0,
                    value=float(self.profile.backup_interval_hours), step=0.5
                )
                pub_url = w3.text_input("Public App URL (for Keep-Alive Ping):", value=self.profile.app_public_url)

                if st.form_submit_button("💾 Save Feature Toggles & Automation Settings", width="stretch"):
                    current_overrides.update({
                        "enable_discord": en_disc,
                        "enable_discord_voice": en_voice,
                        "enable_auto_learn": en_learn,
                        "enable_auto_scout": en_scout,
                        "enable_calculator": en_calc,
                        "enable_lore_vault": en_vault,
                        "enable_keep_alive_ping": en_ping,
                        "backup_interval_hours": float(b_hours),
                        "app_public_url": pub_url.strip()
                    })
                    self.db.save_mode_override(self.profile.mode_key, current_overrides)
                    self.profile.apply_overrides(current_overrides)
                    st.success("Feature flags saved to database!")
                    st.rerun()

        with tab_modes:
            col_rev, col_new = st.columns(2)
            with col_rev:
                with st.container(border=True):
                    st.subheader("🔄 Fallback to Python Mode File Defaults")
                    st.write(
                        f"Clears all custom database overrides for **`{self.profile.mode_key}`** and restores "
                        f"100% of the original strings, prompts, and settings defined in `mode_{self.profile.mode_key}.py`."
                    )
                    if st.button("🔄 Revert Mode to Python File Defaults Now", width="stretch"):
                        self.db.delete_mode_override(self.profile.mode_key)
                        self.profile.revert_to_python_defaults()
                        st.success(f"Reverted `{self.profile.mode_key}` to pristine Python file defaults!")
                        st.rerun()

            with col_new:
                with st.container(border=True):
                    st.subheader("➕ Create New Deployment Mode")
                    with st.form("create_new_mode_form"):
                        new_mode_slug = st.text_input("New Mode Key (lowercase, e.g. 'consulting', 'fitness'):")
                        new_mode_title = st.text_input("New Mode Header Title:")
                        new_mode_bot = st.text_input("New Mode Bot Name:")
                        if st.form_submit_button("➕ Create New Mode Profile", width="stretch"):
                            clean_slug = new_mode_slug.lower().strip().replace(" ", "_")
                            if clean_slug:
                                self.db.save_mode_override(clean_slug, {
                                    "page_title": new_mode_title or f"{clean_slug.title()} AI",
                                    "header_title": new_mode_title or f"✨ {clean_slug.title()} AI",
                                    "bot_name": new_mode_bot or "Aurora",
                                    "enable_discord": False
                                })
                                st.success(f"Created mode `{clean_slug}`! Switch to it in the sidebar.")
                                st.rerun()

    # =========================================================================
    # PAGE 7: CELESTIAL MEMORY INSPECTOR (WITH DOUBLE-CLICK PROFILE EDITOR)
    # =========================================================================
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
            "💬 Recent Chat Logs"
        ])
        with t1:
            st.caption("Double-click any cell to edit permanent user profile facts.")
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

    # =========================================================================
    # PAGE 8: SILENCE VEIL & ABUSE LEDGER
    # =========================================================================
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

    # =========================================================================
    # PAGE 9: DISCORD VOICE & RESONANCE BROADCASTER
    # =========================================================================
    def page_discord_broadcaster(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("broadcast_header", "Discord Voice & Resonance Broadcaster"))

        default_ch = self.discord_worker.allowed_channel_ids[0] if self.discord_worker.allowed_channel_ids else ""
        target_ch = st.text_input("Target Discord Channel ID:", value=default_ch)

        if "broadcast_draft_text" not in st.session_state:
            st.session_state.broadcast_draft_text = ""

        raw_input = st.text_area(
            "Announcement Message (Write plain notes or final text):",
            value=st.session_state.broadcast_draft_text,
            height=150
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
                st.warning("Provide both a Channel ID and a message.")

    # =========================================================================
    # PAGE 10: DATABASE VAULT, GITHUB SYNC & PROJECT ZIP EXPORTER
    # =========================================================================
    def page_database_vault(self) -> None:
        self.render_banner()
        st.header(self.profile.get_str("db_header", "Database Vault, Auto-Backup & Watchdog Controls"))
        st.caption(
            f"Active Database File: `{self.db.db_file}` • "
            f"Scheduled Auto-Backup Every **{self.profile.backup_interval_hours}h** • "
            f"Keep-Alive Self-Ping: **{'Active' if self.profile.enable_keep_alive_ping else 'Paused'}**"
        )

        # 1. GitHub Branch Vault Cloud Persistence Panel
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
                            f"backups/{self.db.db_file}",
                            raw,
                            f"Manual Admin Backup ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"
                        )
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

                if g2.button("⬇️ Pull & Restore Latest Database from GitHub Vault", width="stretch"):
                    with st.spinner("Downloading latest backup from GitHub `db-vault` branch..."):
                        tmp_pull = f"pull_{self.db.db_file}"
                        ok, msg = self.db.vault.pull_file_from_vault(f"backups/{self.db.db_file}", tmp_pull)
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
                    "⚠️ `GITHUB_TOKEN` is not yet set in your Streamlit Secrets. "
                    "Add `GITHUB_TOKEN = \"ghp_...\"` and `GITHUB_REPO = \"thebr0kenst4r/aurora-koa\"` to your "
                    "Streamlit Secrets to enable automatic zero-reboot cloud persistence!"
                )

        # 2. Manual Local SQLite Download & Live Restore
        col_dl, col_ul = st.columns(2)
        with col_dl:
            with st.container(border=True):
                st.subheader("📥 Download Live SQLite Backup")
                st.write("Creates a lock-free binary snapshot of your active SQLite database.")
                db_bytes = self.db.export_db_bytes()
                st.download_button(
                    label=f"💾 Download {self.db.db_file} ({len(db_bytes):,} bytes)",
                    data=db_bytes,
                    file_name=f"backup_{self.db.db_file}",
                    mime="application/octet-stream",
                    key="stable_sqlite_download_btn",
                    width="stretch"
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

        # 3. 1-Click Full Modular Project .zip Exporter
        with st.container(border=True):
            st.subheader("📦 Export Complete Modular Project (.zip)")
            st.caption("Download all refactored `.py` modules (including `mode_gaming.py` and `mode_realtor.py`), `requirements.txt`, and config files in a single `.zip` bundle.")
            zip_bytes = self._build_project_zip_bytes()
            st.download_button(
                label="📦 Download Aurora-KoA-Modular.zip",
                data=zip_bytes,
                file_name="Aurora-KoA-Modular.zip",
                mime="application/zip",
                key="download_full_project_zip_btn",
                width="stretch"
            )

    def _build_project_zip_bytes(self) -> bytes:
        """Packages all project files into an in-memory .zip archive."""
        buf = io.BytesIO()
        project_files = [
            "bot.py", "profiles.py", "mode_gaming.py", "mode_realtor.py",
            "database.py", "ai_engine.py", "discord_worker.py", "web_ui.py",
            "requirements.txt", "secrets_template.toml", ".streamlit/config.toml", "README.md"
        ]
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path in project_files:
                if os.path.exists(rel_path):
                    zf.write(rel_path, arcname=f"Aurora-KoA-main/{rel_path}")
        buf.seek(0)
        return buf.read()

    # =========================================================================
    # MAIN NAVIGATION ROUTER
    # =========================================================================
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
                st.Page(self.page_sovereign_forge, title=self.profile.get_str("forge_nav", "🔥 Sovereign Knowledge Forge")),
                st.Page(self.page_mode_string_studio, title=self.profile.get_str("studio_nav", "🎨 Mode, String & Persona Studio")),
                st.Page(self.page_memory_inspector, title=self.profile.get_str("memory_nav", "🧠 Celestial Memory Inspector")),
                st.Page(self.page_database_vault, title=self.profile.get_str("db_nav", "🗄️ Database Vault & Self-Heal")),
            ]
            if self.profile.enable_discord:
                admin_pages.append(st.Page(self.page_silence_controller, title=self.profile.get_str("silence_nav", "🤫 Silence Veil & Abuse Ledger")))
                admin_pages.append(st.Page(self.page_discord_broadcaster, title=self.profile.get_str("broadcast_nav", "📢 Discord Voice Broadcaster")))
            nav_sections[admin_group_label] = admin_pages

        pg = st.navigation(nav_sections)
        pg.run()