"""
bot.py
Smart Assistant for Thebr0kenst4r (Aurora — Gaming Mode, Aria — Realtor Mode,
plus any dynamically synthesized modes).
Server-ready Streamlit + Discord + Watchdog entrypoint supporting:
  - Global site-wide persistent mode switching (DomainConfig)
  - Isolated per-mode SQLite databases (DatabaseManager)
  - Dual GitHub ('db-vault') + Cloudflare R2 (YYYY/MM/DD) scheduled backups
  - Shared cross-mode capabilities (Self-Upgrade Core, R2, 403-Resilient Media Conduit, Web Research)
"""

import os
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone
import streamlit as st

from profiles import DomainConfig, SITE_STATE_FILE, get_deployment_namespace
from database import DatabaseManager, GitHubVaultSync
from ai_engine import GeminiEngine
from discord_worker import DiscordWorker
from web_ui import StreamlitUI


@st.cache_resource
def _bootstrap_global_site_state() -> bool:
    if not os.path.exists(SITE_STATE_FILE):
        vault = GitHubVaultSync("site_state")
        if vault.is_configured:
            ns = get_deployment_namespace()
            ok, msg = vault.pull_file_from_vault(f"backups/site_state_{ns}.json", SITE_STATE_FILE)
            print(f"[Global Site State Boot Check - {ns}] {msg}")
            if ok and os.path.exists(SITE_STATE_FILE):
                try:
                    with open(SITE_STATE_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict) and data.get("active_mode"):
                            DomainConfig.set_active_mode_key(data["active_mode"], db=None)
                except Exception as e:
                    print(f"[Global Site State Parse Notice]: {e}")
    return True


_bootstrap_global_site_state()

active_mode_key = DomainConfig.get_active_mode_key()
base_profile = DomainConfig.load_base_profile_from_python_file(active_mode_key)

st.set_page_config(
    page_title=base_profile.page_title,
    page_icon=base_profile.page_icon,
    layout="wide",
)


@st.cache_resource
def get_core_services(mode_key: str):
    profile = DomainConfig.load_base_profile_from_python_file(mode_key)
    db_manager = DatabaseManager(profile)

    overrides = db_manager.get_mode_override(mode_key)
    if overrides:
        profile.apply_overrides(overrides)

    ai_engine = GeminiEngine(profile, db_manager)
    discord_worker = DiscordWorker(profile, db_manager, ai_engine)

    discord_worker.start_background_thread()

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

    def _watchdog_keepalive_and_backup():
        last_backup_ts = time.time()
        while True:
            time.sleep(600)
            if profile.enable_keep_alive_ping and profile.app_public_url:
                try:
                    req = urllib.request.Request(
                        profile.app_public_url,
                        headers={"User-Agent": "AuroraKeepAliveWatchdog/2.0"},
                    )
                    urllib.request.urlopen(req, timeout=10)
                except Exception:
                    pass

            interval_sec = max(1800.0, float(profile.backup_interval_hours) * 3600.0)
            if (time.time() - last_backup_ts) >= interval_sec:
                last_backup_ts = time.time()
                db_manager.trigger_background_sync(
                    force=True,
                    reason=f"Scheduled {profile.backup_interval_hours}h Auto-Backup",
                )
                if db_manager.r2.is_configured:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    changelog_md = f"# Scheduled Backup ({ts})\n- Mode: `{mode_key}`\n- DB: `{db_manager.db_basename}`\n"
                    db_manager.r2.run_full_dated_backup(
                        {db_manager.db_basename: db_manager.export_db_bytes()},
                        b"",
                        changelog_md,
                    )

    if not any(t.name == f"Watchdog_{mode_key}" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_watchdog_keepalive_and_backup, name=f"Watchdog_{mode_key}", daemon=True).start()

    return db_manager, ai_engine, discord_worker


db_service, engine_service, discord_service = get_core_services(active_mode_key)
active_profile = DomainConfig.load_profile(active_mode_key, db=db_service)

db_service.profile = active_profile
engine_service.profile = active_profile
discord_service.profile = active_profile

app_ui = StreamlitUI(
    profile=active_profile,
    db=db_service,
    engine=engine_service,
    discord_worker=discord_service,
)
app_ui.run()
