"""
bot.py
Lightweight Main Entrypoint for Streamlit Community Cloud.
Wires together DomainConfig (loading mode_gaming.py or mode_realtor.py + SQLite overrides),
DatabaseManager (with GitHub 'db-vault' auto-persistence), GeminiEngine,
DiscordWorker, Keep-Alive Self-Ping & Scheduled Auto-Backup Watchdog, and StreamlitUI.
"""

import time
import threading
import urllib.request
import streamlit as st

from profiles import DomainConfig
from database import DatabaseManager
from ai_engine import GeminiEngine
from discord_worker import DiscordWorker
from web_ui import StreamlitUI

# 1. Load the base Python profile for initial page config
initial_mode_key = DomainConfig.get_active_mode_key()
base_profile = DomainConfig.load_base_profile_from_python_file(initial_mode_key)

st.set_page_config(
    page_title=base_profile.page_title,
    page_icon=base_profile.page_icon,
    layout="wide"
)


@st.cache_resource
def get_core_services(mode_key: str):
    """
    Initializes singleton backend services per mode_key so the DatabaseManager,
    Gemini key rotation pool, DiscordWorker, and Watchdog threads persist across reruns.
    """
    profile = DomainConfig.load_base_profile_from_python_file(mode_key)
    db_manager = DatabaseManager(profile)

    # Apply any admin overrides saved in SQLite on top of the Python file defaults
    overrides = db_manager.get_mode_override(mode_key)
    if overrides:
        profile.apply_overrides(overrides)

    ai_engine = GeminiEngine(profile, db_manager)
    discord_worker = DiscordWorker(profile, db_manager, ai_engine)

    # 1. Start Discord gateway thread if enabled
    discord_worker.start_background_thread()

    # 2. Start Autonomous King of Avalon Update Scout (every 12 hours if enabled)
    def _periodic_koa_scout():
        time.sleep(60)
        while True:
            try:
                if profile.enable_auto_scout:
                    ai_engine.run_koa_auto_scout()
            except Exception as e:
                print(f"Periodic KoA Scout notice: {e}")
            time.sleep(12 * 3600)

    if not any(t.name == f"KoAScout_{mode_key}" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_periodic_koa_scout, name=f"KoAScout_{mode_key}", daemon=True).start()

    # 3. Start Zero-Token Keep-Alive Self-Ping & Scheduled GitHub Backup Watchdog
    def _watchdog_keepalive_and_backup():
        last_backup_ts = time.time()
        while True:
            time.sleep(600)  # Wake every 10 minutes
            # (a) Zero-Token HTTP Keep-Alive Ping so Streamlit Cloud doesn't sleep
            if profile.enable_keep_alive_ping and profile.app_public_url:
                try:
                    req = urllib.request.Request(
                        profile.app_public_url,
                        headers={"User-Agent": "AuroraKeepAliveWatchdog/2.0"}
                    )
                    urllib.request.urlopen(req, timeout=10)
                except Exception:
                    pass

            # (b) Scheduled Periodic GitHub 'db-vault' Backup
            interval_sec = max(1800.0, float(profile.backup_interval_hours) * 3600.0)
            if (time.time() - last_backup_ts) >= interval_sec:
                last_backup_ts = time.time()
                db_manager.trigger_background_sync(
                    force=True,
                    reason=f"Scheduled {profile.backup_interval_hours}h Auto-Backup"
                )

    if not any(t.name == f"Watchdog_{mode_key}" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_watchdog_keepalive_and_backup, name=f"Watchdog_{mode_key}", daemon=True).start()

    return db_manager, ai_engine, discord_worker


# 2. Acquire Backend Services & Apply Latest SQLite Mode Overrides
db_service, engine_service, discord_service = get_core_services(initial_mode_key)
active_profile = DomainConfig.load_profile(initial_mode_key, db=db_service)

db_service.profile = active_profile
engine_service.profile = active_profile
discord_service.profile = active_profile

# 3. Render Streamlit Multi-Page UI
app_ui = StreamlitUI(
    profile=active_profile,
    db=db_service,
    engine=engine_service,
    discord_worker=discord_service
)
app_ui.run()