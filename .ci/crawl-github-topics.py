#!/usr/bin/env python3
"""Crawl GitHub topic-tagged WordPress plugins/themes into discovery catalogs.

Publishes discovery/github-plugins.json and discovery/github-themes.json.
Every entry is verified at the resolved tag ref (never the default branch):
release/tag resolution, root plugin/theme headers, committed vendor/ for
composer repos, syntax lint and dangerous-sink static checks.

State (.ci/github-crawl-state.json) maps full_name -> last verified entry and
pushed_at, so unchanged repos skip re-verification on the next run. The state
file is committed together with the catalogs.

Failures never publish: if the crawl raises, existing catalogs stay untouched.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_DIR = ROOT / "discovery"
STATE_FILE = ROOT / ".ci" / "github-crawl-state.json"
STATE_VERSION = 1

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DEFAULT_ICON = f"{RAW}/estebanforge/unrepress-index/main/assets/images/icon-256.webp"

TOPICS: dict[str, list[str]] = {
    "plugins": ["wp-plugin", "wordpress-plugin"],
    "themes": ["wp-theme", "wordpress-theme"],
}
MIN_STARS = 20
PER_PAGE = 100
MAX_PER_QUERY = 1000
PACE_SECONDS = 0.25
SEARCH_PACE_SECONDS = 2.1  # search quota is 30/min
CORE_FLOOR = 50
SEARCH_FLOOR = 2
MAX_RUNTIME_SECONDS = 5 * 3600

SEMVER_RE = re.compile(r"(?:^|[^\d.])(\d+)\.(\d+)(?:\.(\d+))?(?:$|[^\d.])")
DANGEROUS_SINKS = (
    re.compile(r"\beval\s*\(\s*base64_decode\b"),
    re.compile(r"\b(shell_exec|passthru)\s*\("),
    re.compile(r"\bsystem\s*\(\s*\$"),
    re.compile(r"\bexec\s*\(\s*\$\w+\s*\."),
    re.compile(r"[A-Za-z0-9+/]{4096,}={0,2}"),
)


class CrawlError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[crawl] {message}", flush=True)


class GitHub:
    """GitHub API client with per-bucket rate accounting and backoff."""

    LIMITS = {"core": 5000, "search": 30}
    FLOORS = {"core": CORE_FLOOR, "search": SEARCH_FLOOR}
    PACES = {"core": PACE_SECONDS, "search": SEARCH_PACE_SECONDS}

    def __init__(self, token: str) -> None:
        self.token = token
        self.remaining: dict[str, int] = dict(self.LIMITS)
        self.reset_at: dict[str, float] = {"core": 0.0, "search": 0.0}

    def _headers(self, api_type: str) -> dict[str, str]:
        headers = {
            "User-Agent": "UnrePress-index-crawler",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json" if api_type == "core" else "application/vnd.github.text-match+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _observe(self, headers: Any, api_type: str) -> None:
        try:
            self.remaining[api_type] = int(headers.get("x-ratelimit-remaining", self.LIMITS[api_type]))
            self.reset_at[api_type] = float(headers.get("x-ratelimit-reset", 0))
        except (TypeError, ValueError):
            pass

    def _wait_for_budget(self, api_type: str) -> None:
        if self.remaining[api_type] >= self.FLOORS[api_type]:
            return
        wait = max(self.reset_at[api_type] - time.time(), 0.0) + 2.0
        if wait > MAX_RUNTIME_SECONDS:
            raise CrawlError(f"rate-limit reset exceeds max runtime ({int(wait)}s)")
        log(f"rate limit low ({self.remaining[api_type]} {api_type}); sleeping {int(wait)}s")
        time.sleep(min(wait, 3600))
        # No response arrived while sleeping; assume the window reset and let
        # the next HTTP response install real numbers.
        self.remaining[api_type] = self.LIMITS[api_type]

    def get(self, url: str, api_type: str = "core") -> tuple[int, dict[str, Any] | list[Any] | None]:
        """GET a JSON URL. Returns (status, body). 404/410 -> (status, None)."""
        self._wait_for_budget(api_type)
        for attempt in range(4):
            request = urllib.request.Request(url, headers=self._headers(api_type))
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    self._observe(response.headers, api_type)
                    body = json.loads(response.read().decode("utf-8"))
                    return response.status, body
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    return error.code, None
                if error.code in (403, 429):
                    retry_after = error.headers.get("retry-after")
                    reset = error.headers.get("x-ratelimit-reset")
                    wait = float(retry_after) if retry_after else max(float(reset or 0) - time.time(), 0) + 2.0
                    wait = min(max(wait, 2.0), 3600)
                    log(f"HTTP {error.code} on {url}; backing off {int(wait)}s")
                    time.sleep(wait)
                    continue
                if 500 <= error.code < 600 and attempt < 3:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise CrawlError(f"HTTP {error.code} on {url}") from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                if attempt < 3:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise CrawlError(f"network failure on {url}: {error}") from error
        raise CrawlError(f"retries exhausted on {url}")

    def get_raw(self, url: str) -> str | None:
        """Fetch a raw text file. Returns None on 404."""
        request = urllib.request.Request(url, headers={"User-Agent": "UnrePress-index-crawler"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return None
            log(f"raw fetch HTTP {error.code} on {url}")
            return None
        except (urllib.error.URLError, TimeoutError) as error:
            log(f"raw fetch failed {url}: {error}")
            return None

    def pace(self, api_type: str = "core") -> None:
        time.sleep(self.PACES[api_type])


def search_topic(gh: GitHub, topic: str) -> tuple[list[dict[str, Any]], bool]:
    """All starred-enough repos for one topic, up to the 1000-result cap."""
    results: list[dict[str, Any]] = []
    truncated = False
    query = urllib.parse.quote(f"topic:{topic} fork:false archived:false stars:>={MIN_STARS}")
    for page in range(1, MAX_PER_QUERY // PER_PAGE + 1):
        url = f"{API}/search/repositories?q={query}&sort=stars&order=desc&per_page={PER_PAGE}&page={page}"
        status, body = gh.get(url, api_type="search")
        if status != 200 or not isinstance(body, dict):
            break
        items = body.get("items", [])
        results.extend(items)
        if len(items) < PER_PAGE:
            break
        if len(results) >= MAX_PER_QUERY:
            truncated = True
            break
        gh.pace("search")
    return results[:MAX_PER_QUERY], truncated


def merge_topics(hits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge the two queries per kind by full_name, keeping the topic union."""
    merged: dict[str, dict[str, Any]] = {}
    for hit in hits:
        key = hit["full_name"].lower()
        slot = merged.setdefault(key, {**hit, "_topics": []})
        if hit.get("stargazers_count", 0) > slot.get("stargazers_count", 0):
            slot.update({k: v for k, v in hit.items() if k != "_topics"})
        if hit["_topic"] not in slot["_topics"]:
            slot["_topics"].append(hit["_topic"])
    return merged


def parse_semver(value: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.search(value)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0))


def resolve_release(gh: GitHub, full_name: str) -> dict[str, Any] | None:
    """Latest release, else the highest-semver tag. Excludes tagless repos."""
    gh.pace()
    status, release = gh.get(f"{API}/repos/{full_name}/releases/latest")
    if status == 200 and isinstance(release, dict):
        tag = release.get("tag_name", "")
        version = parse_semver(tag)
        if not version:
            return None
        asset = next(
            (
                a
                for a in release.get("assets", [])
                if a.get("browser_download_url", "").endswith(".zip")
            ),
            None,
        )
        if asset:
            return {
                "tag": tag,
                "version": version,
                "build": "release-asset",
                "download_url": asset["browser_download_url"],
            }
        return {
            "tag": tag,
            "version": version,
            "build": "tag-archive",
            "download_url": f"https://codeload.github.com/{full_name}/zip/refs/tags/{urllib.parse.quote(tag)}",
        }
    gh.pace()
    status, tags = gh.get(f"{API}/repos/{full_name}/tags?per_page=100")
    if status != 200 or not isinstance(tags, list):
        return None
    tagged = [(tag["name"], parse_semver(tag["name"])) for tag in tags if parse_semver(tag.get("name", ""))]
    if not tagged:
        return None
    best_tag, best_version = max(tagged, key=lambda pair: pair[1])
    return {
        "tag": best_tag,
        "version": best_version,
        "build": "tag-archive",
        "download_url": f"https://codeload.github.com/{full_name}/zip/refs/tags/{urllib.parse.quote(best_tag)}",
    }


def get_tag_tree(gh: GitHub, full_name: str, tag: str) -> dict[str, Any] | None:
    """Tree of the tag ref (never the default branch). None when truncated."""
    gh.pace()
    status, tree = gh.get(f"{API}/repos/{full_name}/git/trees/{urllib.parse.quote(tag)}?recursive=1")
    if status != 200 or not isinstance(tree, dict) or tree.get("truncated"):
        return None
    return tree


def root_paths(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in tree.get("tree", []) if "/" not in entry.get("path", "")]


def detect_main_file(gh: GitHub, full_name: str, tag: str, repo_name: str, roots: list[dict[str, Any]]) -> str | None:
    """First root PHP file carrying a Plugin Name: header (up to 3 fetches)."""
    php_files = sorted(entry["path"] for entry in roots if entry["path"].endswith(".php") and entry.get("type") == "blob")
    candidates: list[str] = []
    preferred = f"{repo_name}.php"
    if preferred in php_files:
        candidates.append(preferred)
    for generic in ("plugin.php", "index.php"):
        if generic in php_files:
            candidates.append(generic)
    candidates.extend(path for path in php_files if path not in candidates)
    for path in candidates[:3]:
        gh.pace()
        content = gh.get_raw(f"{RAW}/{full_name}/{urllib.parse.quote(tag)}/{path}")
        if content and re.search(r"^\s*\*\s*Plugin Name:\s*\S", content, re.MULTILINE):
            return path
    return None


def extract_header(content: str, key: str) -> str:
    match = re.search(rf"^\s*\*\s*{key}:\s*(.+?)\s*$", content, re.MULTILINE)
    if not match and key in ("Theme Name", "Template"):
        match = re.search(rf"^{key}:\s*(.+?)\s*$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def static_checks(content: str, php_available: bool) -> str | None:
    """Cheap quality gates on the main file. Returns a reason when excluded."""
    for pattern in DANGEROUS_SINKS:
        if pattern.search(content):
            return "dangerous-sink"
    if php_available:
        with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as handle:
            handle.write(content)
            temp_path = handle.name
        try:
            lint = subprocess.run(
                ["php", "-l", temp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if lint.returncode != 0:
                return "php-lint"
        except (subprocess.SubprocessError, OSError):
            return None
        finally:
            os.unlink(temp_path)
    return None


def verify_repo(gh: GitHub, repo: dict[str, Any], php_available: bool) -> tuple[dict[str, Any] | None, str | None]:
    """Full verification of one repo at its resolved tag. Returns (entry, reason)."""
    full_name = repo["full_name"]
    name = full_name.split("/")[1]

    resolved = resolve_release(gh, full_name)
    if resolved is None:
        return None, "no-tags-or-releases"

    tree = get_tag_tree(gh, full_name, resolved["tag"])
    if tree is None:
        return None, "missing-or-truncated-tree"

    roots = root_paths(tree)
    root_names = {entry["path"] for entry in roots}

    if "composer.json" in root_names and not any(
        path == "vendor" or path.startswith("vendor/") for path in root_names
    ):
        return None, "composer-without-vendor"

    version_str = ".".join(str(part) for part in resolved["version"])
    main_content = None

    if repo["_kind"] == "themes":
        if "style.css" not in root_names:
            return None, "no-root-style-css"
        gh.pace()
        style = gh.get_raw(f"{RAW}/{full_name}/{urllib.parse.quote(resolved['tag'])}/style.css")
        if not style or not extract_header(style, "Theme Name"):
            return None, "no-theme-header"
        if extract_header(style, "Template"):
            return None, "child-theme"
        name = extract_header(style, "Theme Name") or name
        requires_php = extract_header(style, "Requires PHP") or "7.4"
        requires = extract_header(style, "Requires at least") or "6.0"
        description = repo.get("description") or ""
    else:
        main_file = detect_main_file(gh, full_name, resolved["tag"], name, roots)
        if main_file is None:
            return None, "no-plugin-header"
        gh.pace()
        main_content = gh.get_raw(f"{RAW}/{full_name}/{urllib.parse.quote(resolved['tag'])}/{main_file}")
        if not main_content:
            return None, "main-file-unreadable"
        name = extract_header(main_content, "Plugin Name") or name
        requires_php = extract_header(main_content, "Requires PHP") or "7.4"
        requires = extract_header(main_content, "Requires at least") or "6.0"
        description = repo.get("description") or extract_header(main_content, "Description")

    if main_content is not None:
        reason = static_checks(main_content, php_available)
        if reason:
            return None, reason

    owner, repo_part = full_name.split("/", 1)
    pushed = repo.get("pushed_at", "")
    updated = pushed.replace("T", " ").split(".")[0].removesuffix("Z") if pushed else ""
    public_topics = [topic for topic in repo.get("topics", []) if topic not in TOPICS[repo["_kind"]]]
    safe_description = " ".join((description or "").split())[:2000]

    entry = {
        "slug": f"{owner}--{repo_part}",
        "name": name,
        "version": version_str,
        "author": f'<a href="https://github.com/{html.escape(owner, quote=True)}">{html.escape(owner)}</a>',
        "author_profile": f"https://github.com/{owner}",
        "requires": requires,
        "tested": "6.7",
        "requires_php": requires_php,
        "short_description": safe_description[:150],
        "description": safe_description,
        "download_url": resolved["download_url"],
        "homepage": repo.get("html_url", ""),
        "icons": {"default": DEFAULT_ICON},
        "last_updated": updated,
        "tags": public_topics,
        "topics": repo["_topics"],
        "stars": int(repo.get("stargazers_count", 0)),
        "pushed_at": pushed,
        "provider": "github",
        "build": resolved["build"],
        "default_branch": repo.get("default_branch", "main"),
    }
    return entry, None


def load_curated_repo_urls() -> set[str]:
    """Repository URLs already covered by curated per-item JSONs."""
    urls: set[str] = set()

    def normalize(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"^https?://(www\.)?", "", value)
        return value.removesuffix(".git").rstrip("/")

    for item_dir in ("plugins", "themes"):
        for path in (ROOT / item_dir).rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            meta = data.get("unrepress_meta", {}) if isinstance(data, dict) else {}
            for field in ("repository", "tags"):
                value = meta.get(field)
                if isinstance(value, str) and "github.com" in value:
                    urls.add(normalize(value))
            if isinstance(data, dict) and isinstance(data.get("homepage"), str) and "github.com" in data["homepage"]:
                urls.add(normalize(data["homepage"]))
    return urls


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": STATE_VERSION, "repos": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def write_catalog(path: Path, source: str, kind: str, entries: list[dict[str, Any]], truncated: bool) -> None:
    payload = {
        "schema_version": 1,
        "source": source,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "truncated": truncated,
        kind: entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def crawl_kind(
    gh: GitHub,
    kind: str,
    state: dict[str, Any],
    curated_urls: set[str],
    limit: int,
    php_available: bool,
) -> tuple[list[dict[str, Any]], bool]:
    hits: list[dict[str, Any]] = []
    truncated = False
    for topic in TOPICS[kind]:
        topic_hits, capped = search_topic(gh, topic)
        truncated = truncated or capped
        for hit in topic_hits:
            hit["_topic"] = topic
            hit["_kind"] = kind
            hits.append(hit)

    repos = merge_topics(hits)
    entries: list[dict[str, Any]] = []
    verified = 0
    started = time.time()
    for key in sorted(repos, key=lambda name: -repos[name].get("stargazers_count", 0)):
        repo = repos[key]
        if limit and verified >= limit:
            log(f"{kind}: --limit {limit} reached, stopping early")
            break
        if time.time() - started > MAX_RUNTIME_SECONDS:
            raise CrawlError(f"{kind}: max runtime reached with {verified} verified")
        full_name = repo["full_name"]
        if not (repo.get("description") or "").strip():
            log(f"{kind}: skip {full_name} (no description)")
            state["repos"][full_name.lower()] = {"pushed_at": repo.get("pushed_at", ""), "excluded": "no-description"}
            continue
        normalized = re.sub(r"^https?://(www\.)?", "", repo["html_url"].lower()).removesuffix(".git")
        if normalized.rstrip("/") in curated_urls:
            log(f"{kind}: skip curated {full_name}")
            continue
        cached = state["repos"].get(full_name.lower())
        if cached and cached.get("pushed_at") == repo.get("pushed_at"):
            # Unchanged repo: reuse the verified entry, or keep the exclusion.
            if cached.get("entry"):
                cached["entry"]["topics"] = repo["_topics"]
                cached["entry"]["stars"] = int(repo.get("stargazers_count", 0))
                entries.append(cached["entry"])
            else:
                log(f"{kind}: keep excluded {full_name} ({cached.get('excluded')})")
            continue
        entry, reason = verify_repo(gh, repo, php_available)
        if entry is None:
            log(f"{kind}: exclude {full_name} ({reason})")
            state["repos"][full_name.lower()] = {"pushed_at": repo.get("pushed_at", ""), "excluded": reason}
        else:
            entries.append(entry)
            state["repos"][full_name.lower()] = {"pushed_at": repo.get("pushed_at", ""), "entry": entry}
            log(f"{kind}: ok {full_name} v{entry['version']} ({entry['build']}, {entry['stars']} stars)")
        verified += 1
        save_state(state)
    entries.sort(key=lambda entry: -entry.get("stars", 0))
    return entries, truncated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kinds", default="plugins,themes", help="comma list: plugins,themes")
    parser.add_argument("--limit", type=int, default=0, help="verify at most N repos per kind (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="do not write catalog files")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log("GITHUB_TOKEN not set; unauthenticated quota (60 req/hr) will not suffice")
    gh = GitHub(token)
    php_available = shutil.which("php") is not None
    if not php_available:
        log("php not on PATH; skipping lint checks")

    state = load_state()
    curated_urls = load_curated_repo_urls()
    log(f"curated repo URLs for dedup: {len(curated_urls)}")

    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip() in TOPICS]
    outputs: dict[str, tuple[Path, str, list[dict[str, Any]], bool]] = {}
    try:
        for kind in kinds:
            entries, truncated = crawl_kind(gh, kind, state, curated_urls, args.limit, php_available)
            outputs[kind] = (DISCOVERY_DIR / f"github-{kind}.json", kind, entries, truncated)
            log(f"{kind}: {len(entries)} entries (truncated={truncated})")
    except CrawlError as error:
        log(f"FAILED, keeping previous catalogs: {error}")
        return 1

    if not args.dry_run:
        for path, kind, entries, truncated in outputs.values():
            write_catalog(path, "github-topic", kind, entries, truncated)
        save_state(state)
        log("state and catalogs written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
