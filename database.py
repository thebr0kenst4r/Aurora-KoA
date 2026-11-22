"""
database.py
Encapsulates isolated per-mode SQLite databases (aurora_core.db, realtor_core.db,
<mode>_core.db), L1/L2 memory retrieval, Mode & UI String Overrides,
Unresolved Problems & Self-Upgrade Proposals ledgers (`unresolved_issues`,
`upgrade_proposals`, `r2_media_archive`), live SQLite .backup() export/restore,
and dual cloud persistence to BOTH GitHub ('db-vault') AND Cloudflare R2 (YYYY/MM/DD).
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

from profiles import (
    BotProfile,
    SITE_STATE_FILE,
    CUSTOM_MODES_FILE,
    get_secret_or_env,
)
from capabilities.r2_storage import CloudflareR2Manager


class GitHubVaultSync:
    def __init__(self, db_filename: str):
        self.db_filename = os.path.basename(db_filename)
        self.token = get_secret_or_env("GITHUB_TOKEN", "")
        self.repo = get_secret_or_env("GITHUB_REPO", "") or self._detect_repo_from_git()
        self.branch = get_secret_or_env("GITHUB_VAULT_BRANCH", "db-vault")
        self.last_sync_time: float = 0.0
        self.last_sync_status: str = "Not synced yet"
        self._lock = threading.Lock()

    def _detect_repo_from_git(self) -> str:
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
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def ensure_vault_branch_exists(self) -> bool:
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
                timeout=10,
            )
            if ref_resp.status_code != 200:
                return False

            base_sha = ref_resp.json()["object"]["sha"]
            create_resp = requests.post(
                f"https://api.github.com/repos/{self.repo}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
                timeout=10,
            )
            return create_resp.status_code in (200, 201)
        except Exception as e:
            self.last_sync_status = f"Branch check error: {e}"
            return False

    def pull_file_from_vault(self, remote_path: str, local_path: str) -> Tuple[bool, str]:
        if not self.is_configured:
            return False, "GITHUB_TOKEN not set in Secrets."

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

            parent = os.path.dirname(local_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(raw_bytes)

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.last_sync_status = f"Pulled from GitHub ({ts})"
            return True, f"Restored {local_path} ({len(raw_bytes):,} bytes) from {self.repo}@{self.branch}"
        except Exception as e:
            return False, f"GitHub pull failed: {e}"

    def push_bytes_to_vault(self, remote_path: str, raw_bytes: bytes, commit_msg: str) -> Tuple[bool, str]:
        if not self.is_configured:
            return False, "GITHUB_TOKEN not set in Secrets."

        with self._lock:
            try:
                self.ensure_vault_branch_exists()
                url = f"https://api.github.com/repos/{self.repo}/contents/{remote_path}"
                get_r = requests.get(f"{url}?ref={self.branch}", headers=self._headers(), timeout=10)
                existing_sha = get_r.json().get("sha") if get_r.status_code == 200 else None

                payload: Dict[str, Any] = {
                    "message": commit_msg,
                    "content": base64.b64encode(raw_bytes).decode("ascii"),
                    "branch": self.branch,
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
    def __init__(self, profile: BotProfile):
        self.profile = profile
        self.db_file = profile.resolved_db_path
        self.db_basename = os.path.basename(self.db_file)
        self.write_lock = threading.Lock()
        self.vault = GitHubVaultSync(self.db_basename)
        self.r2 = CloudflareR2Manager()
        self._startup_restore_check()
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_file, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _startup_restore_check(self) -> None:
        if not os.path.exists(self.db_file) or os.path.getsize(self.db_file) == 0:
            restored = False
            if self.r2.is_configured:
                ok_r2, raw_r2, msg_r2 = self.r2.download_bytes(f"backups/latest/{self.db_basename}")
                if ok_r2 and raw_r2.startswith(b"SQLite format 3"):
                    with open(self.db_file, "wb") as f:
                        f.write(raw_r2)
                    print(f"[R2 Startup Restore - {self.db_basename}] {msg_r2}")
                    restored = True
            if not restored and self.vault.is_configured:
                ok, msg = self.vault.pull_file_from_vault(f"backups/{self.db_basename}", self.db_file)
                print(f"[GitHub Vault Startup Check - {self.db_basename}] {msg}")

        if not os.path.exists(CUSTOM_MODES_FILE) and self.vault.is_configured:
            self.vault.pull_file_from_vault("backups/custom_modes.json", CUSTOM_MODES_FILE)

    def sync_json_file_to_vault(self, local_path: str, remote_path: str, commit_msg: str) -> None:
        if not os.path.exists(local_path):
            return

        def _push():
            try:
                with open(local_path, "rb") as f:
                    raw = f.read()
                if self.vault.is_configured:
                    self.vault.push_bytes_to_vault(remote_path, raw, commit_msg)
                if self.r2.is_configured:
                    self.r2.upload_bytes(remote_path, raw, content_type="application/json")
            except Exception as e:
                print(f"[JSON Dual Vault Sync Notice]: {e}")

        threading.Thread(target=_push, daemon=True).start()

    def trigger_background_sync(self, force: bool = False, reason: str = "Auto-sync") -> None:
        if not self.vault.is_configured and not self.r2.is_configured:
            return
        if not force and (time.time() - self.vault.last_sync_time) < 60:
            return

        def _worker():
            raw_bytes = self.export_db_bytes()
            if raw_bytes:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if self.vault.is_configured:
                    self.vault.push_bytes_to_vault(
                        f"backups/{self.db_basename}",
                        raw_bytes,
                        f"[{self.profile.mode_key}] {reason} ({ts})",
                    )
                if self.r2.is_configured:
                    dated_key = self.r2.build_dated_key("backups", self.db_basename, subfolder="databases")
                    self.r2.upload_bytes(dated_key, raw_bytes, content_type="application/octet-stream")
                    self.r2.upload_bytes(f"backups/latest/{self.db_basename}", raw_bytes, content_type="application/octet-stream")

        threading.Thread(target=_worker, daemon=True).start()

    def init_db(self) -> None:
        with self.write_lock:
            conn = self._get_conn()
            c = conn.cursor()

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

            c.execute("""
                CREATE TABLE IF NOT EXISTS user_summaries (
                    user_id TEXT PRIMARY KEY,
                    summary TEXT,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    facts TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS episodic_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    topic TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

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

            c.execute("""
                CREATE TABLE IF NOT EXISTS jokes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    joke TEXT UNIQUE,
                    added_by TEXT DEFAULT 'Aurora'
                )
            """)

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

            c.execute("""
                CREATE TABLE IF NOT EXISTS silence_status (
                    channel_id TEXT PRIMARY KEY,
                    silence_until DATETIME,
                    max_silence_until DATETIME
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS abuse_ledger (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    offense_count INTEGER DEFAULT 0,
                    muted_until DATETIME,
                    last_offense DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS mode_overrides (
                    mode_key TEXT PRIMARY KEY,
                    overrides_json TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS unresolved_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    summary TEXT,
                    details TEXT,
                    target_mode TEXT DEFAULT 'all',
                    status TEXT DEFAULT 'open',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS upgrade_proposals (
                    slug TEXT PRIMARY KEY,
                    title TEXT,
                    scope TEXT,
                    target_mode TEXT,
                    files_json TEXT,
                    branch_name TEXT,
                    pr_number INTEGER DEFAULT 0,
                    pr_url TEXT,
                    status TEXT DEFAULT 'In Testing',
                    ci_status TEXT DEFAULT 'pending',
                    changelog TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS r2_media_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    media_type TEXT,
                    r2_key TEXT UNIQUE,
                    public_url TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            for kw, topic, cat, tier, content, ver in self.profile.seed_docs:
                c.execute(
                    """
                    INSERT OR IGNORE INTO strategy_docs
                    (keyword, topic, category, access_tier, content, verified_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kw.lower().strip(), topic, cat, tier, content, ver),
                )

            for jk in self.profile.seed_jokes:
                c.execute("INSERT OR IGNORE INTO jokes (joke, added_by) VALUES (?, ?)", (jk, self.profile.bot_name))

            for title, cat, url, desc in self.profile.seed_media:
                c.execute(
                    "INSERT OR IGNORE INTO media_vault (title, category, url, description, added_by) VALUES (?, ?, ?, ?, ?)",
                    (title, cat, url, desc, self.profile.creator_name),
                )

            conn.commit()
            conn.close()

    def record_unresolved_issue(self, category: str, summary: str, details: str, target_mode: str = "all") -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO unresolved_issues (category, summary, details, target_mode, status) VALUES (?, ?, ?, ?, 'open')",
                (category.strip(), summary.strip(), details.strip()[:2000], target_mode.strip()),
            )
            conn.commit()
            conn.close()

    def get_unresolved_issues(self, status_filter: str = "open") -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()
        if status_filter == "all":
            c.execute("SELECT id, category, summary, details, target_mode, status, timestamp FROM unresolved_issues ORDER BY id DESC LIMIT 100")
        else:
            c.execute(
                "SELECT id, category, summary, details, target_mode, status, timestamp FROM unresolved_issues WHERE status = ? ORDER BY id DESC LIMIT 100",
                (status_filter,),
            )
        rows = [
            {
                "id": r[0],
                "category": r[1],
                "summary": r[2],
                "details": r[3],
                "target_mode": r[4],
                "status": r[5],
                "timestamp": r[6],
            }
            for r in c.fetchall()
        ]
        conn.close()
        return rows

    def mark_issue_resolved(self, issue_id: int) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute("UPDATE unresolved_issues SET status = 'resolved' WHERE id = ?", (int(issue_id),))
            conn.commit()
            conn.close()

    def save_upgrade_proposal(self, record: Dict[str, Any]) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO upgrade_proposals
                (slug, title, scope, target_mode, files_json, branch_name, pr_number, pr_url, status, ci_status, changelog, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    record["slug"],
                    record["title"],
                    record.get("scope", "shared_capability"),
                    record.get("target_mode", "all"),
                    record.get("files_json", "[]"),
                    record.get("branch_name", ""),
                    int(record.get("pr_number", 0)),
                    record.get("pr_url", ""),
                    record.get("status", "In Testing"),
                    record.get("ci_status", "pending"),
                    record.get("changelog", ""),
                ),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Upgrade staged: {record['title']}")

    def get_upgrade_proposals(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT slug, title, scope, target_mode, files_json, branch_name, pr_number, pr_url, status, ci_status, changelog, created_at FROM upgrade_proposals ORDER BY created_at DESC"
        )
        rows = [
            {
                "slug": r[0],
                "title": r[1],
                "scope": r[2],
                "target_mode": r[3],
                "files": json.loads(r[4] or "[]"),
                "branch_name": r[5],
                "pr_number": r[6],
                "pr_url": r[7],
                "status": r[8],
                "ci_status": r[9],
                "changelog": r[10],
                "created_at": r[11],
            }
            for r in c.fetchall()
        ]
        conn.close()
        return rows

    def update_proposal_status(self, slug: str, status: str, ci_status: Optional[str] = None) -> None:
        with self.write_lock:
            conn = self._get_conn()
            if ci_status:
                conn.execute("UPDATE upgrade_proposals SET status = ?, ci_status = ? WHERE slug = ?", (status, ci_status, slug))
            else:
                conn.execute("UPDATE upgrade_proposals SET status = ? WHERE slug = ?", (status, slug))
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Upgrade {slug} -> {status}")

    def log_r2_media_download(self, title: str, media_type: str, r2_key: str, public_url: str, size_bytes: int) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO r2_media_archive (title, media_type, r2_key, public_url, size_bytes, timestamp)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (title, media_type, r2_key, public_url, int(size_bytes)),
            )
            conn.commit()
            conn.close()

    def get_r2_media_archive(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT id, title, media_type, r2_key, public_url, size_bytes, timestamp FROM r2_media_archive ORDER BY id DESC LIMIT 100")
        rows = [
            {
                "id": r[0],
                "title": r[1],
                "media_type": r[2],
                "r2_key": r[3],
                "public_url": r[4],
                "size_bytes": r[5],
                "timestamp": r[6],
            }
            for r in c.fetchall()
        ]
        conn.close()
        return rows

    def get_mode_override(self, mode_key: str) -> Dict[str, Any]:
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
        payload_json = json.dumps(overrides_dict, ensure_ascii=False, indent=2)
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO mode_overrides (mode_key, overrides_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (mode_key.lower().strip(), payload_json),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason=f"Updated mode_overrides for {mode_key}")

    def delete_mode_override(self, mode_key: str) -> None:
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

    def export_db_bytes(self) -> bytes:
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
            return True, f"Successfully restored {self.db_basename} ({len(raw_bytes):,} bytes) and queued cloud sync!"
        except Exception as e:
            return False, f"Restore failed: {e}"
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def save_chat(self, user_id: str, channel_id: str, role: str, content: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO user_chat (user_id, channel_id, role, content) VALUES (?, ?, ?, ?)",
                (str(user_id), str(channel_id), role, content),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=False, reason="Chat update")

    def update_l1_profile(self, user_id: str, facts: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO user_profiles (user_id, facts, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (str(user_id), facts.strip()),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="L1 Profile update")

    def add_l2_episode(self, user_id: str, topic: str, content: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO episodic_memory (user_id, topic, content) VALUES (?, ?, ?)",
                (str(user_id), topic.strip(), content.strip()),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="L2 Episode added")

    def update_rolling_summary(self, user_id: str, summary: str) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO user_summaries (user_id, summary, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (str(user_id), summary.strip()),
            )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=False, reason="Summary update")

    def get_ltm_context(self, user_id: str, prompt: str) -> str:
        conn = self._get_conn()
        c = conn.cursor()

        c.execute("SELECT facts FROM user_profiles WHERE user_id = ?", (str(user_id),))
        p_row = c.fetchone()
        l1_profile = p_row[0] if p_row else ""

        c.execute("SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),))
        s_row = c.fetchone()
        summary = s_row[0] if s_row else ""

        words = [w.lower() for w in re.findall(r"\w+", prompt) if len(w) > 3]
        episodes = []
        if words:
            placeholders = " OR ".join(["content LIKE ?" for _ in words[:4]])
            params = [str(user_id)] + [f"%{w}%" for w in words[:4]]
            query = f"SELECT topic, content FROM episodic_memory WHERE user_id = ? AND ({placeholders}) ORDER BY id DESC LIMIT 3"
            c.execute(query, params)
            episodes = c.fetchall()

        conn.close()

        injections = []
        if l1_profile:
            injections.append(f"[User Permanent Profile: {l1_profile}]")
        if episodes:
            ep_text = "; ".join([f"{top}: {con}" for top, con in episodes])
            injections.append(f"[Recalled Past Episodes: {ep_text}]")
        if summary:
            injections.append(f"[Persistent Knowledge of User: {summary}]")

        return "\n".join(injections)

    def get_active_user_context(
        self,
        user_id: str,
        prompt: str,
        timeout_minutes: int = 120,
        max_turns: int = 14,
        char_limit: int = 1800,
        since_timestamp: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        c = conn.cursor()

        if since_timestamp:
            c.execute(
                """
                SELECT role, content FROM user_chat
                WHERE user_id = ?
                  AND timestamp >= ?
                ORDER BY id DESC LIMIT ?
                """,
                (str(user_id), since_timestamp, max_turns),
            )
        else:
            c.execute(
                """
                SELECT role, content FROM user_chat
                WHERE user_id = ?
                  AND timestamp >= datetime('now', '-' || ? || ' minutes')
                ORDER BY id DESC LIMIT ?
                """,
                (str(user_id), timeout_minutes, max_turns),
            )
        rows = c.fetchall()
        conn.close()

        formatted: List[Dict[str, Any]] = []
        ltm_snippet = self.get_ltm_context(user_id, prompt)
        if ltm_snippet:
            formatted.append({"role": "user", "parts": [{"text": f"[Context & Memory Notes]\n{ltm_snippet}"}]})
            formatted.append({"role": "model", "parts": [{"text": "Understood. I have these records in mind."}]})

        for role, content in reversed(rows):
            sdk_role = "model" if role in ("model", "assistant") else "user"
            clipped = content[:char_limit] + ("..." if len(content) > char_limit else "")
            if formatted and formatted[-1]["role"] == sdk_role:
                formatted[-1]["parts"][0]["text"] += f"\n\n{clipped}"
            else:
                formatted.append({"role": sdk_role, "parts": [{"text": clipped}]})

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

        c.execute("SELECT role, content, timestamp FROM user_chat WHERE user_id = ? ORDER BY id DESC LIMIT 12", (str(user_id),))
        history = c.fetchall()
        conn.close()

        lines = [
            f"- Profile Facts: {profile}",
            f"- Rolling Summary: {summary}",
            "- Recorded Episodes:",
        ]
        for top, con, ts in episodes:
            lines.append(f"  [{ts}] {top}: {con}")
        lines.append("- Recent Raw Dialogues:")
        for role, content, ts in reversed(history):
            lines.append(f"  [{ts}] {role}: {content[:200]}")
        return "\n".join(lines)

    def query_local_docs(self, prompt: str, allowed_tiers: Optional[List[str]] = None) -> Optional[str]:
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

    def add_verified_strategy(
        self,
        keyword: str,
        topic: str,
        content: str,
        verified_by: str,
        category: str = "General",
        access_tier: str = "Everyone",
    ) -> None:
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_docs
                (keyword, topic, category, access_tier, content, verified_by, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (keyword.lower().strip(), topic.strip(), category.strip(), access_tier.strip(), content.strip(), verified_by.strip()),
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
                "timestamp": ts,
            })
        conn.close()
        return docs

    def sync_strategy_dataframe(self, edited_df: pd.DataFrame) -> None:
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
                    (kw, topic, cat, tier, content, ver),
                )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="Bulk edited strategy_docs table")

    def sync_profiles_dataframe(self, edited_df: pd.DataFrame) -> None:
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
                        (uid, facts),
                    )
            conn.commit()
            conn.close()
        self.trigger_background_sync(force=True, reason="Bulk edited user_profiles table")

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
                (title.strip(), category.strip(), url.strip(), description.strip(), added_by),
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

    def set_silence(self, channel_id: str, hours_min: int = 1, hours_max: int = 4) -> None:
        now = datetime.now(timezone.utc)
        min_s = (now + timedelta(hours=hours_min)).strftime("%Y-%m-%d %H:%M:%S")
        max_s = (now + timedelta(hours=hours_max)).strftime("%Y-%m-%d %H:%M:%S")
        with self.write_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO silence_status (channel_id, silence_until, max_silence_until) VALUES (?, ?, ?)",
                (str(channel_id), min_s, max_s),
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
                (str(user_id), username, count, muted_until_str),
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
            (str(channel_id), minutes),
        )
        engaged = c.fetchone() is not None
        conn.close()
        return engaged
