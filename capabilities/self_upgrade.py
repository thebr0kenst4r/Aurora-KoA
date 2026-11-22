"""
capabilities/self_upgrade.py
Aurora's Core Self-Upgrade, Autonomous Problem-Solving & CI/CD Orchestration Engine.
Features:
  1. Full GitHub REST & GraphQL API client + GitHub Actions CI/CD workflow runner/poller.
  2. Automatic Unresolved Problem & Error Recorder (`unresolved_issues` table).
  3. Autonomous Investigation & Capability/Mode Synthesizer:
     - Writes new shared capabilities into `capabilities/<name>.py`
     - Writes mode-specific capabilities into `modes/<mode>/<name>.py`
     - Generates complete new self-contained modes under `modes/<new_mode>/`
     - Validates syntax locally via `ast.parse()`, pushes to a staging branch
       (`aurora-upgrade/...`), opens a PR labeled `in-testing` & `awaiting-sovereign-review`,
       triggers GitHub Actions CI/CD tests, and archives changelogs into Cloudflare R2.
"""

import os
import re
import ast
import json
import base64
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any, Optional
import requests

from profiles import get_secret_or_env


class GitHubGraphQLAndRestClient:
    """Unified GitHub REST + GraphQL + Actions CI/CD client."""

    def __init__(self):
        self.token = get_secret_or_env("GITHUB_TOKEN", "")
        self.repo = get_secret_or_env("GITHUB_REPO", "") or self._detect_repo_from_git()
        self.default_branch = "main"

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

    @property
    def owner_and_name(self) -> Tuple[str, str]:
        parts = self.repo.split("/", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else ("thebr0kenst4r", "aurora-koa")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def rest_request(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        if not self.is_configured:
            return 0, {"message": "GITHUB_TOKEN not configured"}
        url = endpoint if endpoint.startswith("http") else f"https://api.github.com/repos/{self.repo}/{endpoint.lstrip('/')}"
        try:
            resp = requests.request(method, url, headers=self._headers(), json=payload, timeout=20)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, data
        except Exception as e:
            return 0, {"message": str(e)}

    def graphql_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """Executes a query or mutation against GitHub's GraphQL v4 API."""
        if not self.is_configured:
            return False, "GITHUB_TOKEN not configured"
        try:
            resp = requests.post(
                "https://api.github.com/graphql",
                headers=self._headers(),
                json={"query": query, "variables": variables or {}},
                timeout=20,
            )
            body = resp.json()
            if resp.status_code == 200 and "errors" not in body:
                return True, body.get("data", {})
            return False, body.get("errors", body)
        except Exception as e:
            return False, str(e)

    def fetch_repo_ci_overview_graphql(self) -> Dict[str, Any]:
        owner, name = self.owner_and_name
        gql = """
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            defaultBranchRef {
              name
              target {
                ... on Commit {
                  oid
                  messageHeadline
                  committedDate
                }
              }
            }
            pullRequests(first: 10, states: OPEN, orderBy: {field: CREATED_AT, direction: DESC}) {
              nodes {
                number
                title
                headRefName
                url
                createdAt
                labels(first: 5) {
                  nodes { name }
                }
                commits(last: 1) {
                  nodes {
                    commit {
                      statusCheckRollup {
                        state
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        ok, data = self.graphql_query(gql, {"owner": owner, "name": name})
        if not ok:
            return {"ok": False, "error": str(data)}
        repo_node = (data or {}).get("repository") or {}
        prs = ((repo_node.get("pullRequests") or {}).get("nodes")) or []
        return {"ok": True, "pull_requests": prs, "default_branch": repo_node.get("defaultBranchRef")}

    def ensure_branch(self, branch_name: str, base_branch: str = "main") -> bool:
        code, _ = self.rest_request("GET", f"git/ref/heads/{branch_name}")
        if code == 200:
            return True
        b_code, b_data = self.rest_request("GET", f"git/ref/heads/{base_branch}")
        if b_code != 200:
            return False
        base_sha = b_data["object"]["sha"]
        c_code, _ = self.rest_request("POST", "git/refs", {"ref": f"refs/heads/{branch_name}", "sha": base_sha})
        return c_code in (200, 201)

    def commit_file_to_branch(
        self,
        branch_name: str,
        file_path: str,
        content_str: str,
        commit_message: str,
    ) -> Tuple[bool, str]:
        self.ensure_branch(branch_name, self.default_branch)
        g_code, g_data = self.rest_request("GET", f"contents/{file_path}?ref={branch_name}")
        existing_sha = g_data.get("sha") if (g_code == 200 and isinstance(g_data, dict)) else None

        payload: Dict[str, Any] = {
            "message": commit_message,
            "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        p_code, p_data = self.rest_request("PUT", f"contents/{file_path}", payload)
        if p_code in (200, 201):
            return True, f"Committed `{file_path}` to branch `{branch_name}`"
        return False, f"GitHub commit failed ({p_code}): {str(p_data)[:200]}"

    def create_or_get_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        labels: Optional[List[str]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        code, existing = self.rest_request("GET", f"pulls?state=open&head={self.owner_and_name[0]}:{head_branch}")
        if code == 200 and isinstance(existing, list) and existing:
            pr = existing[0]
            return True, pr

        c_code, pr_data = self.rest_request(
            "POST",
            "pulls",
            {"title": title, "body": body, "head": head_branch, "base": base_branch},
        )
        if c_code in (200, 201) and isinstance(pr_data, dict):
            pr_num = pr_data.get("number")
            if pr_num and labels:
                self.rest_request("POST", f"issues/{pr_num}/labels", {"labels": labels})
            return True, pr_data
        return False, {"error": str(pr_data)[:250]}

    def trigger_ci_workflow(self, branch_name: str, workflow_file: str = "aurora_ci.yml") -> Tuple[bool, str]:
        code, data = self.rest_request(
            "POST",
            f"actions/workflows/{workflow_file}/dispatches",
            {"ref": branch_name},
        )
        if code in (200, 201, 204):
            return True, f"Dispatched GitHub Actions CI (`{workflow_file}`) on `{branch_name}`"
        return False, f"CI dispatch status ({code}): {str(data)[:180]}"

    def get_branch_ci_status(self, branch_name: str) -> Dict[str, Any]:
        code, data = self.rest_request("GET", f"actions/runs?branch={branch_name}&per_page=3")
        if code == 200 and isinstance(data, dict):
            runs = data.get("workflow_runs", [])
            if runs:
                latest = runs[0]
                return {
                    "status": latest.get("status", "unknown"),
                    "conclusion": latest.get("conclusion") or "pending",
                    "html_url": latest.get("html_url", ""),
                    "run_id": latest.get("id"),
                }
        return {"status": "not_found", "conclusion": "pending", "html_url": ""}

    def merge_pull_request(self, pr_number: int, commit_title: str) -> Tuple[bool, str]:
        code, data = self.rest_request(
            "PUT",
            f"pulls/{pr_number}/merge",
            {"commit_title": commit_title, "merge_method": "squash"},
        )
        if code in (200, 201):
            return True, f"Merged PR #{pr_number} into `{self.default_branch}`!"
        return False, f"Merge failed ({code}): {str(data)[:200]}"


class AuroraSelfUpgradeEngine:
    """
    Core Self-Upgrade & Autonomous Capability Expansion Engine.
    """

    def __init__(self, db: Any, r2_manager: Any = None):
        self.db = db
        self.r2 = r2_manager
        self.gh = GitHubGraphQLAndRestClient()

    @staticmethod
    def validate_python_syntax(code_str: str) -> Tuple[bool, str]:
        try:
            ast.parse(code_str)
            compile(code_str, "<aurora_generated_module>", "exec")
            return True, "AST & bytecode syntax check passed"
        except SyntaxError as e:
            return False, f"SyntaxError at line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation error: {e}"

    def record_problem(
        self,
        category: str,
        summary: str,
        details: str,
        target_mode: str = "all",
    ) -> None:
        if self.db and hasattr(self.db, "record_unresolved_issue"):
            self.db.record_unresolved_issue(category, summary, details, target_mode)

    def stage_module_upgrade(
        self,
        module_title: str,
        target_files: Dict[str, str],
        target_mode: str = "all",
        scope: str = "shared_capability",
        changelog_note: str = "",
        write_locally: bool = True,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        for rel_path, code_text in target_files.items():
            if rel_path.endswith(".py"):
                ok_syn, syn_msg = self.validate_python_syntax(code_text)
                if not ok_syn:
                    self.record_problem("self_upgrade_syntax", f"Failed validation for {rel_path}", syn_msg, target_mode)
                    return False, f"Aborted upgrade — `{rel_path}` failed syntax validation: {syn_msg}", {}

        slug = re.sub(r"[^a-z0-9_]+", "_", module_title.lower()).strip("_")[:32] or "upgrade"
        ts_compact = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        branch_name = f"aurora-upgrade/{slug}-{ts_compact}"

        if write_locally:
            for rel_path, code_text in target_files.items():
                parent = os.path.dirname(rel_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(rel_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(code_text)

        pr_number = 0
        pr_url = ""
        ci_status = "local_validated"
        gh_messages: List[str] = []

        if self.gh.is_configured:
            for rel_path, code_text in target_files.items():
                c_ok, c_msg = self.gh.commit_file_to_branch(
                    branch_name=branch_name,
                    file_path=rel_path,
                    content_str=code_text,
                    commit_message=f"[Aurora Self-Upgrade] Stage {rel_path} ({module_title})",
                )
                gh_messages.append(c_msg)

            pr_body = (
                f"### ✨ Aurora Autonomous Self-Upgrade Proposal\n"
                f"- **Module / Upgrade:** {module_title}\n"
                f"- **Scope:** `{scope}` (Target Mode: `{target_mode}`)\n"
                f"- **Lifecycle Status:** `In Testing — Awaiting Sovereign Review`\n\n"
                f"#### Files Staged\n"
                + "\n".join(f"- `{p}`" for p in target_files.keys())
                + f"\n\n#### Enhancement & Changelog Notes\n{changelog_note or 'Synthesized by Aurora Self-Upgrade Core.'}"
            )
            pr_ok, pr_data = self.gh.create_or_get_pull_request(
                title=f"[In Testing] {module_title}",
                body=pr_body,
                head_branch=branch_name,
                labels=["in-testing", "awaiting-sovereign-review"],
            )
            if pr_ok and isinstance(pr_data, dict):
                pr_number = int(pr_data.get("number", 0))
                pr_url = pr_data.get("html_url", "")
                ci_ok, ci_msg = self.gh.trigger_ci_workflow(branch_name)
                ci_status = "ci_running" if ci_ok else "pr_opened"
                gh_messages.append(ci_msg)

        proposal_record = {
            "slug": f"{slug}_{ts_compact}",
            "title": module_title,
            "scope": scope,
            "target_mode": target_mode,
            "files_json": json.dumps(list(target_files.keys())),
            "branch_name": branch_name,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "status": "In Testing (Awaiting Sovereign Review)",
            "ci_status": ci_status,
            "changelog": changelog_note,
        }
        if self.db and hasattr(self.db, "save_upgrade_proposal"):
            self.db.save_upgrade_proposal(proposal_record)

        if self.r2 and getattr(self.r2, "is_configured", False):
            md_doc = (
                f"# Aurora Self-Upgrade Log: {module_title}\n"
                f"- **Timestamp (UTC):** {datetime.now(timezone.utc).isoformat()}\n"
                f"- **Scope:** {scope} | **Target Mode:** {target_mode}\n"
                f"- **GitHub Branch:** `{branch_name}` | **PR:** {pr_url or 'Local'}\n\n"
                f"## Notes\n{changelog_note}\n"
            )
            log_key = self.r2.build_dated_key("backups", f"UPGRADE_{slug}_{ts_compact}.md", subfolder="changelogs")
            self.r2.upload_bytes(log_key, md_doc.encode("utf-8"), content_type="text/markdown")

        summary = (
            f"Staged `{module_title}` ({len(target_files)} file(s)) on branch `{branch_name}` "
            f"[Status: In Testing — Awaiting Sovereign Review]."
        )
        if gh_messages:
            summary += " " + " | ".join(gh_messages)
        return True, summary, proposal_record

    def generate_new_mode_scaffold(
        self,
        mode_slug: str,
        bot_persona_name: str,
        domain_description: str,
        calculator_title: str,
        generated_profile_code: str,
        generated_capability_code: str,
        changelog_note: str = "",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        clean_mode = re.sub(r"[^a-z0-9_]+", "_", mode_slug.lower()).strip("_")
        if not clean_mode:
            return False, "Invalid mode slug.", {}

        cap_filename = f"{clean_mode}_toolkit.py"
        init_code = (
            f'"""\nmodes/{clean_mode}/__init__.py\n'
            f'Auto-generated mode package for {clean_mode} ({bot_persona_name}).\n"""\n\n'
            f"from modes.{clean_mode}.profile import {clean_mode.title().replace('_', '')}Profile\n\n"
            f'__all__ = ["{clean_mode.title().replace("_", "")}Profile"]\n'
        )

        files_map = {
            f"modes/{clean_mode}/__init__.py": init_code,
            f"modes/{clean_mode}/profile.py": generated_profile_code,
            f"modes/{clean_mode}/{cap_filename}": generated_capability_code,
        }

        return self.stage_module_upgrade(
            module_title=f"New Mode: {clean_mode.title()} ({bot_persona_name})",
            target_files=files_map,
            target_mode=clean_mode,
            scope="new_mode",
            changelog_note=changelog_note or f"Created self-contained mode `{clean_mode}`: {domain_description}",
            write_locally=True,
        )
