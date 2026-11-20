"""
capabilities/web_research.py
Shared 4-method zero-quota web verification & intelligence suite available to all modes:
1. DuckDuckGo Web Search (DDGS.text)
2. DuckDuckGo Live News (DDGS.news)
3. MediaWiki / Fandom REST JSON APIs
4. Public Reddit JSON feeds
"""

import urllib.parse
from typing import List, Tuple
import requests
from ddgs import DDGS

SEARCH_TRIGGERS = [
    "search", "google", "look up", "lookup", "online", "latest news",
    "patch notes", "recent update", "current event", "new hero", "meta", "verify"
]


class WebResearchSuite:
    """Zero-API-quota multi-source web verification suite."""

    def __init__(self, mode_key: str = "gaming"):
        self.mode_key = mode_key

    def search_ddg_text(self, query: str, max_results: int = 3) -> Tuple[List[str], List[str]]:
        snippets, domains = [], []
        try:
            with DDGS() as ddgs:
                for r in list(ddgs.text(query, max_results=max_results)):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    href = r.get("href", "")
                    if body:
                        snippets.append(f"[Web: {title}] {body} ({href})")
                    if href and "/" in href:
                        parts = href.split("/")
                        if len(parts) > 2:
                            domains.append(parts[2])
        except Exception as e:
            print(f"DDG text search notice: {e}")
        return snippets, domains

    def search_ddg_news(self, query: str, max_results: int = 2) -> Tuple[List[str], List[str]]:
        snippets, domains = [], []
        try:
            with DDGS() as ddgs:
                for r in list(ddgs.news(query, max_results=max_results)):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    url = r.get("url", "")
                    date = r.get("date", "")
                    if body:
                        snippets.append(f"[News ({date}): {title}] {body} ({url})")
                    if url and "/" in url:
                        parts = url.split("/")
                        if len(parts) > 2:
                            domains.append(parts[2])
        except Exception as e:
            print(f"DDG news search notice: {e}")
        return snippets, domains

    def search_mediawiki(self, query: str) -> Tuple[List[str], List[str]]:
        snippets, domains = [], []
        endpoints = []
        if self.mode_key == "gaming":
            endpoints.append(("kingofavalon.fandom.com", "https://kingofavalon.fandom.com/api.php"))
        endpoints.append(("en.wikipedia.org", "https://en.wikipedia.org/w/api.php"))

        headers = {"User-Agent": "AuroraStrategicBot/2.0 (Educational & Tactical Research)"}
        for domain_label, api_url in endpoints:
            try:
                params = {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "utf8": "1",
                    "format": "json",
                    "srlimit": 2,
                }
                r = requests.get(api_url, params=params, headers=headers, timeout=6)
                if r.status_code == 200:
                    search_hits = r.json().get("query", {}).get("search", [])
                    for hit in search_hits:
                        title = hit.get("title", "")
                        raw_snip = hit.get("snippet", "")
                        clean_snip = (
                            raw_snip.replace('<span class="searchmatch">', "")
                            .replace("</span>", "")
                            .replace("&quot;", '"')
                        )
                        if clean_snip:
                            snippets.append(f"[Wiki ({domain_label}) - {title}]: {clean_snip}")
                            domains.append(domain_label)
            except Exception:
                pass
        return snippets, domains

    def search_reddit_public(self, query: str) -> Tuple[List[str], List[str]]:
        snippets, domains = [], []
        headers = {"User-Agent": "AuroraStrategicBot/2.0"}
        try:
            encoded = urllib.parse.quote_plus(query)
            if self.mode_key == "gaming":
                url = f"https://www.reddit.com/r/KingOfAvalon/search.json?q={encoded}&restrict_sr=1&sort=relevance&limit=2"
            else:
                url = f"https://www.reddit.com/search.json?q={encoded}&sort=relevance&limit=2"

            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                children = r.json().get("data", {}).get("children", [])
                for child in children:
                    post = child.get("data", {})
                    title = post.get("title", "")
                    selftext = (post.get("selftext", "") or "")[:280].replace("\n", " ")
                    if title:
                        snippets.append(f"[Community Thread: {title}] {selftext}")
                        domains.append("reddit.com/r/kingofavalon" if self.mode_key == "gaming" else "reddit.com")
        except Exception:
            pass
        return snippets, domains

    def execute_multi_source_research(self, query: str) -> Tuple[str, List[str]]:
        clean_q = query
        for trig in ["search", "lookup", "look up", "google", "verify"]:
            clean_q = clean_q.replace(trig, "")
        clean_q = clean_q.strip() or query

        all_snippets: List[str] = []
        all_domains: List[str] = []

        s1, d1 = self.search_ddg_text(clean_q, max_results=3)
        s2, d2 = self.search_ddg_news(clean_q, max_results=2)
        s3, d3 = self.search_mediawiki(clean_q)
        s4, d4 = self.search_reddit_public(clean_q)

        all_snippets.extend(s1 + s2 + s3 + s4)
        for d in (d1 + d2 + d3 + d4):
            if d and d not in all_domains:
                all_domains.append(d)

        if not all_snippets:
            return "", []

        formatted = "[Live Verified Research Results]:\n" + "\n".join(all_snippets[:7])
        return formatted, all_domains
