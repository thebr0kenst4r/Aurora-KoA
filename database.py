"""
database.py
Encapsulates all SQLite database operations, L1/L2 memory retrieval,
database-backed Mode & UI String Overrides (with instant fallback to Python file defaults),
bulk DataFrame editors for double-click st.data_editor tables,
live thread-safe SQLite .backup() export/restore, and automatic persistence
to a dedicated 'db-vault' branch on GitHub so Streamlit Cloud reboots never wipe data.
"""

import os
import re
import json
import time
import base64
import tempfile
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any
import pandas as pd
import requests

from profiles import BotProfile, get_secret_or_env


class GitHubVaultSync:
    """
    Synchronizes the active SQLite .db file to a dedicated branch (default: 'db-vault')
    in the user's GitHub repository via the GitHub REST API.
    Because it commits to 'db-vault' instead of 'main', Streamlit Cloud NEVER triggers
    an unwanted reboot when saving the database.
    """

    def __init__(self, db_filename: str):
        self.db_filename = db_filename
        self.token = get_secret_or_env("GITHUB_TOKEN", "")
        self.repo = get_secret_or_env("GITHUB_REPO", "") or self._detect_repo_from_git()
        self.branch = get_secret_or_env("GITHUB_VAULT_BRANCH", "db-vault")
        self.last_sync_time: float = 0.0
        self.last_sync_status: str = "Not synced yet"
        self._lock = threading.Lock()

    def _detect_repo_from_git(self) -> str:
        """Auto-detects 'owner/repo' from .git/config if GITHUB_REPO isn't explicitly set in Secrets."""
        git_config = os.path.join(".git", "config")
        if os.path.exists(git_config):
            try:
                with open(git_config, "r", encoding="utf-8") as f:
                    text = f.read()
                m = re.search(r"github\.com[:/]([^/\s]+/[^/\s\.]+?)(?:\.git)?\s", text)
                if m:
                    return m.group(1).strip()
            except Exception:
                pass
        return "thebr0kenst4r/aurora-koa"

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.repo)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

    def ensure_vault_branch_exists(self) -> bool:
        """Verifies the 'db-vault' branch exists on GitHub, creating it off the default branch if needed."""
        if not self.is_configured:
            return False
        try:
            branch_url = f"https://api.github.com/repos/{self.repo}/git/ref/heads/{self.branch}"
            r = requests.get(branch_url, headers=self._headers(), timeout=10)
            if r.status_code == 200:
                return True

            repo_info = requests.get(f"https://api.github.com/repos/{self.repo}", headers=self._headers(), timeout=10)
            default_branch = repo_info.json().get("default_branch", "main") if repo_info.status_code == 200 else "main"

            ref_resp = requests.get(
                f"https://api.github.com/repos/{self.repo}/git/ref/heads/{default_branch}",
                headers=self._headers(),
                timeout=10
            )
            if ref_resp.status_code != 200:
                return False

            base_sha = ref_resp.json()["object"]["sha"]
            create_resp = requests.post(
                f"https://api.github.com/repos/{self.repo}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
                timeout=10
            )
            return create_resp.status_code in (200, 201)
        except Exception as e:
            self.last_sync_status = f"Branch check error: {e}"
            return False

    def pull_file_from_vault(self, remote_path: str, local_path: str) -> Tuple[bool, str]:
        """Downloads a file from the GitHub 'db-vault' branch and writes it safely to disk."""
        if not self.is_configured:
            return False, "GITHUB_TOKEN not set in Streamlit Secrets."

        try:
            url = f"https://api.github.com/repos/{self.repo}/contents/{remote_path}?ref={self.branch}"
            r = requests.get(url, headers=self._headers(), timeout=15)
            if r.status_code == 404:
                return False, f"No remote backup found at {remote_path} on branch '{self.branch}'."
            if r.status_code != 200:
                return False, f"GitHub API returned {r.status_code}."

            data = r.json()
            content_b64 = data.get("content", "")
            if content_b64:
                raw_bytes = base64.b64decode(content_b64)
            elif data.get("download_url"):
                dl = requests.get(data["download_url"], headers=self._headers(), timeout=20)
                raw_bytes = dl.content
            else:
                return False, "Empty file payload from GitHub."

            with open(local_path, "wb") as f:
                f.write(raw_bytes)

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.last_sync_status = f"Pulled from GitHub ({ts})"
            return True, f"Restored {local_path} ({len(raw_bytes):,} bytes) from {self.repo}@{self.branch}"
        except Exception as e:
            return False, f"GitHub pull failed: {e}"

    def push_bytes_to_vault(self, remote_path: str, raw_bytes: bytes, commit_msg: str) -> Tuple[bool, str]:
        """Uploads raw file bytes to the GitHub 'db-vault' branch."""
        if not self.is_configured:
            return False, "GITHUB_TOKEN not set in Streamlit Secrets."

        with self._lock:
            try:
                self.ensure_vault_branch_exists()
                url = f"https://api.github.com/repos/{self.repo}/contents/{remote_path}"
                get_r = requests.get(f"{url}?ref={self.branch}", headers=self._headers(), timeout=10)
                existing_sha = get_r.json().get("sha") if get_r.status_code == 200 else None

                payload: Dict[str, Any] = {
                    "message": commit_msg,
                    "content": base64.b64encode(raw_bytes).decode("ascii"),
                    "branch": self.branch
                }
                if existing_sha:
                    payload["sha"] = existing_sha

                put_r = requests.put(url, headers=self._headers(), json=payload, timeout=25)
                if put_r.status_code in (200, 201):
                    self.last_sync_time = time.time()
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    self.last_sync_status = f"Synced to GitHub ({ts})"
                    return True, f"Saved to {self.repo} ({self.branch}/{remote_path}) at {ts}"
                return False, f"GitHub push error ({put_r.status_code}): {put_r.text[:200]}"
            except Exception as e:
                self.last_sync_status = f"Push failed: {e}"
                return False, f"GitHub push exception: {e}"


class DatabaseManager:
    """
    Thread-safe SQLite Manager handling all schema migrations, L1/L2 memory,
    rolling summaries, tiered knowledge base lookups, mode/UI string overrides,
    silence/abuse tracking, live .backup() snapshots, and automatic GitHub 'db-vault' persistence.
    """

    def __init__(self, profile: BotProfile):
        self.profile = profile
        self.db_file = profile.db_filename
        self.write_lock = threading.Lock()
        self.vault = GitHubVaultSync(self.db_file)
        self._startup_restore_check()
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _startup_restore_check(self) -> None:
        """
        On container boot, if the local SQLite database does not exist yet (ephemeral wipe),
        automatically restores it from the GitHub 'db-vault' branch.
        """
        if not self.vault.is_configured:
            return

        if not os.path.exists(self.db_file) or os.path.getsize(self.db_file) == 0:
            ok, msg = self.vault.pull_file_from_vault(f"backups/{self.db_file}", self.db_file)
            print(f"[GitHub Vault Startup Check] {msg}")

    def trigger_background_sync(self, force: bool = False, reason: str = "Auto-sync") -> None:
        """
        Spawns a non-blocking daemon thread to push the database snapshot to GitHub 'db-vault'.
        Enforces a 60-second cooldown on automatic chat syncs unless force=True (admin edits).
        """
        if not self.vault.is_configured:
            return
        if not force and (time.time() - self.vault.last_sync_time) < 60:
            return

        def _worker():
            raw_bytes = self.export_db_bytes()
            if raw_bytes:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                self.vault.push_bytes_to_vault(
                    f"backups/{self.db_file}",
                    raw_bytes,
                    f"[{self.profile.mode_key}] {reason} ({ts})"
                )

        threading.Thread(target=_worker, daemon=True).start()

    def init_db(self) -> None:
        """Initializes all tables, runs seamless column migrations, and seeds initial profile data."""
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()

            # 1. Cross-channel chat logs
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_chat (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    channel_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. Rolling summaries
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_summaries (
                    user_id TEXT PRIMARY KEY,
                    summary TEXT,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 3. L1 Long-Term Profile Memory
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    facts TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 4. L2 Episodic Memory
            c.execute("""
                CREATE TABLE IF NOT EXISTS episodic_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    topic TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 5. Tiered Strategy & Knowledge Base
            c.execute("""
                CREATE TABLE IF NOT EXISTS strategy_docs (
                    keyword TEXT PRIMARY KEY,
                    topic TEXT,
                    category TEXT DEFAULT 'General',
                    access_tier TEXT DEFAULT 'Everyone',
                    content TEXT,
                    verified_by TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("PRAGMA table_info(strategy_docs)")
            existing_cols = {row[1] for row in c.fetchall()}
            if "category" not in existing_cols:
                c.execute("ALTER TABLE strategy_docs ADD COLUMN category TEXT DEFAULT 'General'")
            if "access_tier" not in existing_cols:
                c.execute("ALTER TABLE strategy_docs ADD COLUMN access_tier TEXT DEFAULT 'Everyone'")

            # 6. Joke / Laughing Moon Vault
            c.execute("""
                CREATE TABLE IF NOT EXISTS jokes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    joke TEXT UNIQUE,
                    added_by TEXT DEFAULT 'Aurora'
                )
            """)

            # 7. Multimedia & Lore Vault
            c.execute("""
                CREATE TABLE IF NOT EXISTS media_vault (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT UNIQUE,
                    category TEXT,
                    url TEXT,
                    description TEXT,
                    added_by TEXT DEFAULT 'Aerith',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 8. Silence Protocol Tracker
            c.execute("""
                CREATE TABLE IF NOT EXISTS silence_status (
                    channel_id TEXT PRIMARY KEY,
                    silence_until DATETIME,
                    max_silence_until DATETIME
                )
            """)

            # 9. Abuse & Hostility Tally Ledger
            c.execute("""
                CREATE TABLE IF NOT EXISTS abuse_ledger (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    offense_count INTEGER DEFAULT 0,
                    muted_until DATETIME,
                    last_offense DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 10. Database-Backed Mode & UI String Overrides (Fallback-Safe Layering)
            c.execute("""
                CREATE TABLE IF NOT EXISTS mode_overrides (
                    mode_key TEXT PRIMARY KEY,
                    overrides_json TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Seed strategy_docs using INSERT OR IGNORE so existing edits are preserved while missing seeds are added
            for kw, topic, cat, tier, content, ver in self.profile.seed_docs:
                c.execute(
                    """
                    INSERT OR IGNORE INTO strategy_docs
                    (keyword, topic, category, access_tier, content, verified_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kw.lower().strip(), topic, cat, tier, content, ver)
                )

            # Seed jokes/tips
            for jk in self.profile.seed_jokes:
                c.execute("INSERT OR IGNORE INTO jokes (joke, added_by) VALUES (?, ?)", (jk, self.profile.bot_name))

            # Seed media vault
            for title, cat, url, desc in self.profile.seed_media:
                c.execute(
                    "INSERT OR IGNORE INTO media_vault (title, category, url, description, added_by) VALUES (?, ?, ?, ?, ?)",
                    (title, cat, url, desc, self.profile.creator_name)
                )

            conn.commit()
            conn.close()

    # --- MODE & UI STRING OVERRIDES (STORED IN SQLITE WITH PYTHON FILE FALLBACK) ---

    def get_mode_override(self, mode_key: str) -> Dict[str, Any]:
        """Fetches stored JSON overrides for a mode_key from SQLite."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT overrides_json FROM mode_overrides WHERE mode_key = ?", (mode_key.lower().strip(),))
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                return json.loads(row[0])
        except Exception as e:
            print(f"Notice reading mode_overrides for {mode_key}: {e}")
        return {}

    def save_mode_override(self, mode_key: str, overrides_dict: Dict[str, Any]) -> None:
        """Saves customized mode strings, persona rules, and feature flags into SQLite and syncs to GitHub."""
        payload_json = json.dumps(overrides_dict, ensure_ascii=False, indent=2)
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO mode_overrides (mode_key, overrides_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (mode_key.lower().strip(), payload_json)
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Updated mode_overrides for {mode_key}")

    def delete_mode_override(self, mode_key: str) -> None:
        """Clears database overrides for mode_key, reverting it 100% to its Python mode file defaults."""
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM mode_overrides WHERE mode_key = ?", (mode_key.lower().strip(),))
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Reverted {mode_key} to Python file defaults")

    def get_all_custom_mode_keys(self) -> List[str]:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT mode_key FROM mode_overrides ORDER BY mode_key")
            keys = [r[0] for r in c.fetchall() if r[0]]
            conn.close()
            return keys
        except Exception:
            return []

    # --- LIVE SAFE BACKUP & RESTORE (.backup() API) ---

    def export_db_bytes(self) -> bytes:
        """
        Uses SQLite's online .backup() API to create a consistent binary snapshot of the database
        even while the Discord background worker has active connections open.
        """
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        try:
            src_conn = self._get_conn()
            dst_conn = sqlite3.connect(tmp_path)
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"Database export error: {e}")
            return b""
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def restore_from_bytes(self, raw_bytes: bytes) -> Tuple[bool, str]:
        """
        Validates an uploaded SQLite .db file and hot-swaps it into the active database
        using SQLite's online .backup() API so active threads never hit corrupt file locks.
        """
        if not raw_bytes or len(raw_bytes) < 100 or not raw_bytes.startswith(b"SQLite format 3"):
            return False, "Invalid file: Uploaded file is not a valid SQLite 3 database."

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw_bytes)

            with self.write_lock:
                src_conn = sqlite3.connect(tmp_path)
                dst_conn = self._get_conn()
                with dst_conn:
                    src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()

            self.init_db()
            self.trigger_background_sync(force=True, reason="Manual DB Restore")
            return True, f"Successfully restored database ({len(raw_bytes):,} bytes) and queued cloud sync!"
        except Exception as e:
            return False, f"Restore failed: {e}"
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # --- CHAT & LONG-TERM MEMORY (L1 + L2) ---

    def save_chat(self, user_id: str, channel_id: str, role: str, content: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO user_chat (user_id, channel_id, role, content) VALUES (?, ?, ?, ?)",
                (str(user_id), str(channel_id), role, content)
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=False, reason="Chat update")

    def update_l1_profile(self, user_id: str, facts: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (str(user_id), facts.strip())
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="L1 Profile update")

    def add_l2_episode(self, user_id: str, topic: str, content: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO episodic_memory (user_id, topic, content) VALUES (?, ?, ?)",
                (str(user_id), topic.strip(), content.strip())
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="L2 Episode added")

    def update_rolling_summary(self, user_id: str, summary: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO user_summaries (user_id, summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (str(user_id), summary.strip())
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=False, reason="Summary update")

    def get_ltm_context(self, user_id: str, prompt: str) -> str:
        """Retrieves L1 Profile facts and selective L2 Episodic notes matching prompt keywords ($0 tokens)."""
        conn = self._get_conn()
        c = conn.cursor()

        c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
        p_row = c.fetchone()
        l1_profile = p_row[0] if p_row else ""

        words = [w.lower() for w in re.findall(r"\w+", prompt) if len(w) > 3]
        episodes = []
        if words:
            placeholders = " OR ".join(["content LIKE ?" for _ in words[:4]])
            params = [str(user_id)] + [f"%{w}%" for w in words[:4]]
            query = f"SELECT topic, content FROM episodic_memory WHERE user_id = ? AND ({placeholders}) ORDER BY id DESC LIMIT 2"
            c.execute(query, params)
            episodes = c.fetchall()

        conn.close()

        injections = []
        if l1_profile:
            injections.append(f"[User Permanent Profile: {l1_profile}]")
        if episodes:
            ep_text = "; ".join([f"{top}: {con}" for top, con in episodes])
            injections.append(f"[Recalled Past Episodes: {ep_text}]")

        return "\n".join(injections)

    def get_active_user_context(self, user_id: str, prompt: str, timeout_minutes: int = 25, max_turns: int = 2, char_limit: int = 350) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()

        c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
        s_row = c.fetchone()
        summary = s_row[0] if s_row else ""

        c.execute(
            """
            SELECT role, content FROM user_chat
            WHERE user_id = ?
              AND timestamp >= datetime('now', '-' || ? || ' minutes')
            ORDER BY id DESC LIMIT ?
            """,
            (str(user_id), timeout_minutes, max_turns)
        )
        rows = c.fetchall()
        conn.close()

        formatted: List[Dict[str, Any]] = []
        ltm_snippet = self.get_ltm_context(user_id, prompt)
        if ltm_snippet:
            formatted.append({"role": "model", "parts": [{"text": ltm_snippet}]})

        if summary:
            formatted.append({"role": "model", "parts": [{"text": f"[Persistent Knowledge of User: {summary}]"}]})

        for role, content in reversed(rows):
            clipped = content[:char_limit] + ("..." if len(content) > char_limit else "")
            formatted.append({"role": role, "parts": [{"text": clipped}]})

        return formatted

    def get_deep_memory(self, user_id: str) -> str:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
        p_row = c.fetchone()
        profile = p_row[0] if p_row else "No profile recorded."

        c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
        s_row = c.fetchone()
        summary = s_row[0] if s_row else "No rolling summary recorded."

        c.execute("SELECT topic, content, timestamp FROM episodic_memory WHERE user_id = ? ORDER BY id DESC LIMIT 5", (str(user_id),))
        episodes = c.fetchall()

        c.execute("SELECT role, content, timestamp FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 8", (str(user_id),))
        history = c.fetchall()
        conn.close()

        lines = [
            f"- Profile Facts: {profile}",
            f"- Rolling Summary: {summary}",
            "- Recorded Episodes:"
        ]
        for top, con, ts in episodes:
            lines.append(f"  [{ts}] {top}: {con}")
        lines.append("- Recent Raw Dialogues:")
        for role, content, ts in reversed(history):
            lines.append(f"  [{ts}] {role}: {content[:140]}")
        return "\n".join(lines)

    # --- KNOWLEDGE BASE, STRATEGY DOCS & DATA_EDITOR BULK SYNC ---

    def query_local_docs(self, prompt: str, allowed_tiers: Optional[List[str]] = None) -> Optional[str]:
        """Searches local strategy_docs first ($0 tokens, 0 API quota) filtered by access tier."""
        clean = prompt.lower()
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT keyword, topic, category, access_tier, content FROM strategy_docs")
        matches = []
        for kw, topic, cat, tier, content in c.fetchall():
            if allowed_tiers and tier not in allowed_tiers:
                continue
            if kw.lower() in clean or topic.lower() in clean or any(w in clean for w in topic.lower().split() if len(w) > 3):
                matches.append(f"[{topic} ({cat}) Knowledge: {content}]")
        conn.close()
        return "\n".join(matches[:4]) if matches else None

    def add_verified_strategy(self, keyword: str, topic: str, content: str, verified_by: str, category: str = "General", access_tier: str = "Everyone") -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_docs
                (keyword, topic, category, access_tier, content, verified_by, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (keyword.lower().strip(), topic.strip(), category.strip(), access_tier.strip(), content.strip(), verified_by.strip())
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Knowledge added: {topic}")

    def delete_strategy_doc(self, keyword: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM strategy_docs WHERE keyword = ?", (keyword.lower().strip(),))
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Knowledge deleted: {keyword}")

    def get_all_strategy_docs(self, allowed_tiers: Optional[List[str]] = None) -> List[Dict[str, str]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT keyword, topic, category, access_tier, content, verified_by, timestamp FROM strategy_docs ORDER BY category, topic")
        docs = []
        for kw, topic, cat, tier, content, ver, ts in c.fetchall():
            if allowed_tiers and tier not in allowed_tiers:
                continue
            docs.append({
                "keyword": kw,
                "topic": topic,
                "category": cat or "General",
                "access_tier": tier or "Everyone",
                "content": content,
                "verified_by": ver,
                "timestamp": ts
            })
        conn.close()
        return docs

    def sync_strategy_dataframe(self, edited_df: pd.DataFrame) -> None:
        """Saves double-click st.data_editor changes (edits, additions, deletions) back to strategy_docs."""
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM strategy_docs")
            for _, row in edited_df.iterrows():
                kw = str(row.get("keyword", "")).lower().strip()
                topic = str(row.get("topic", "")).strip()
                content = str(row.get("content", "")).strip()
                if not kw or not topic or not content:
                    continue
                cat = str(row.get("category", "General")).strip() or "General"
                tier = str(row.get("access_tier", "Everyone")).strip() or "Everyone"
                ver = str(row.get("verified_by", self.profile.creator_name)).strip()
                c.execute(
                    """
                    INSERT OR REPLACE INTO strategy_docs
                    (keyword, topic, category, access_tier, content, verified_by, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (kw, topic, cat, tier, content, ver)
                )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="Bulk edited strategy_docs table")

    def sync_profiles_dataframe(self, edited_df: pd.DataFrame) -> None:
        """Saves double-click st.data_editor changes to L1 user_profiles."""
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM user_profiles")
            for _, row in edited_df.iterrows():
                uid = str(row.get("user_id", "")).strip()
                facts = str(row.get("facts", "")).strip()
                if uid and facts:
                    c.execute(
                        "INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (uid, facts)
                    )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="Bulk edited user_profiles table")

    # --- JOKES & MEDIA VAULT ---

    def get_random_joke(self) -> str:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT joke FROM jokes ORDER BY RANDOM() LIMIT 1")
        row = c.fetchone()
        conn.close()
        return row[0] if row else "Even stars occasionally forget their punchlines."

    def add_joke(self, joke_text: str, added_by: str = "Aurora") -> None:
        if not joke_text or len(joke_text.strip()) < 12:
            return
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("INSERT OR IGNORE INTO jokes (joke, added_by) VALUES (?, ?)", (joke_text.strip(), added_by))
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=False, reason="Joke added")

    def get_all_jokes(self) -> List[Tuple[int, str, str]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT id, joke, added_by FROM jokes ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        return rows

    def sync_jokes_dataframe(self, edited_df: pd.DataFrame) -> None:
        """Saves double-click st.data_editor edits to the jokes table."""
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM jokes")
            for _, row in edited_df.iterrows():
                jk = str(row.get("joke", "")).strip()
                added_by = str(row.get("added_by", self.profile.bot_name)).strip() or self.profile.bot_name
                if jk:
                    c.execute("INSERT OR IGNORE INTO jokes (joke, added_by) VALUES (?, ?)", (jk, added_by))
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="Bulk edited jokes table")

    def get_all_media(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT id, title, category, url, description, added_by, timestamp FROM media_vault ORDER BY id DESC")
        items = [
            {"id": r[0], "title": r[1], "category": r[2], "url": r[3], "description": r[4], "added_by": r[5], "timestamp": r[6]}
            for r in c.fetchall()
        ]
        conn.close()
        return items

    def add_media(self, title: str, category: str, url: str, description: str, added_by: str = "Aerith") -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO media_vault (title, category, url, description, added_by) VALUES (?, ?, ?, ?, ?)",
                (title.strip(), category.strip(), url.strip(), description.strip(), added_by)
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Media added: {title}")

    def delete_media(self, media_id: int) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM media_vault WHERE id = ?", (int(media_id),))
            conn.commit()
            conn.close()

    # --- SILENCE PROTOCOL & ABUSE TALLY (TIMEZONE-AWARE UTC) ---

    def set_silence(self, channel_id: str, hours_min: int = 1, hours_max: int = 4) -> None:
        now = datetime.now(timezone.utc)
        min_s = (now + timedelta(hours=hours_min)).strftime("%Y-%m-%d %H:%M:%S")
        max_s = (now + timedelta(hours=hours_max)).strftime("%Y-%m-%d %H:%M:%S")
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO silence_status (channel_id, silence_until, max_silence_until) VALUES (?, ?, ?)",
                (str(channel_id), min_s, max_s)
            )
            conn.commit()
            conn.close()

    def clear_silence(self, channel_id: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM silence_status WHERE channel_id = ?", (str(channel_id),))
            conn.commit()
            conn.close()

    def get_silence_state(self, channel_id: str) -> Tuple[str, Optional[datetime]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT silence_until, max_silence_until FROM silence_status WHERE channel_id = ?", (str(channel_id),))
        row = c.fetchone()
        conn.close()
        if not row:
            return "active", None

        now = datetime.now(timezone.utc)
        silence_until = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        max_silence = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        if now > max_silence:
            self.clear_silence(channel_id)
            return "active", None
        elif now > silence_until:
            return "grace_period", silence_until
        else:
            return "silenced", silence_until

    def record_abuse_offense(self, user_id: str, username: str) -> Tuple[int, Optional[str]]:
        """Increments user hostility count and applies progressive cooldown mute if threshold exceeded."""
        now = datetime.now(timezone.utc)
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT offense_count FROM abuse_ledger WHERE user_id = ?", (str(user_id),))
            row = c.fetchone()
            count = (row[0] + 1) if row else 1

            muted_until_str = None
            if count >= 3:
                mute_hours = min(24, (count - 2) * 2)
                muted_until_str = (now + timedelta(hours=mute_hours)).strftime("%Y-%m-%d %H:%M:%S")

            c.execute(
                """
                INSERT OR REPLACE INTO abuse_ledger (user_id, username, offense_count, muted_until, last_offense)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (str(user_id), username, count, muted_until_str)
            )
            conn.commit()
            conn.close()
        return count, muted_until_str

    def pardon_user_abuse(self, user_id: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM abuse_ledger WHERE user_id = ?", (str(user_id),))
            conn.commit()
            conn.close()

    def was_channel_recently_engaged(self, channel_id: str, minutes: int = 3) -> bool:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT id FROM user_chat WHERE channel_id = ? AND timestamp >= datetime('now', '-' || ? || ' minutes') LIMIT 1",
            (str(channel_id), minutes)
        )
        engaged = c.fetchone() is not None
        conn.close()
        return engaged