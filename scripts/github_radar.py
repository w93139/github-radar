#!/usr/bin/env python3
"""Read-only GitHub repository radar with local Star snapshots."""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VERSION = "0.2.1"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "github-radar"
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_REPO_ENDPOINT_RE = re.compile(r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RadarError(RuntimeError):
    """A safe, user-facing collector error."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def clean_text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = CONTROL_RE.sub("", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def safe_error(exc: BaseException, limit: int = 240) -> str:
    return clean_text(str(exc), limit=limit) or exc.__class__.__name__


def validate_repo_name(full_name: str) -> str:
    if not REPO_NAME_RE.fullmatch(full_name):
        raise RadarError(f"无效仓库名称：{clean_text(full_name, 100)}")
    return full_name


def validate_api_endpoint(endpoint: str) -> str:
    if endpoint == "/search/repositories" or ALLOWED_REPO_ENDPOINT_RE.fullmatch(endpoint):
        return endpoint
    raise RadarError(f"拒绝非只读或未授权的 GitHub API 端点：{clean_text(endpoint, 120)}")


def gh_api_get(endpoint: str, params: dict[str, str] | None = None, timeout: int = 25) -> Any:
    """Call only explicitly allowlisted GitHub GET endpoints through gh."""
    endpoint = validate_api_endpoint(endpoint)
    command = [
        "gh",
        "api",
        "--method",
        "GET",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        endpoint,
    ]
    for key, value in (params or {}).items():
        command.extend(["-f", f"{key}={value}"])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RadarError("未找到 gh 命令；GitHub Radar 需要已安装并登录的 GitHub CLI。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RadarError(f"GitHub API 查询超时：{endpoint}") from exc
    if result.returncode != 0:
        message = clean_text(result.stderr, 300)
        raise RadarError(f"GitHub API 查询失败（{endpoint}）：{message}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(f"GitHub API 返回了无效 JSON（{endpoint}）。") from exc


def gh_graphql_query(query: str, timeout: int = 30) -> Any:
    """Run an internally constructed read-only GraphQL query."""
    compact = re.sub(r"\s+", " ", query).strip()
    if not compact.startswith("query ") or re.search(r"\bmutation\b", compact, re.IGNORECASE):
        raise RadarError("拒绝非只读 GitHub GraphQL 操作。")
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RadarError("未找到 gh 命令；GitHub Radar 需要已安装并登录的 GitHub CLI。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RadarError("GitHub GraphQL 批量查询超时。") from exc
    if result.returncode != 0:
        raise RadarError(f"GitHub GraphQL 查询失败：{clean_text(result.stderr, 300)}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError("GitHub GraphQL 返回了无效 JSON。") from exc
    if isinstance(payload, dict) and payload.get("errors"):
        raise RadarError("GitHub GraphQL 返回查询错误，未采用该批次数据。")
    return payload


def qualifier_value(value: str) -> str:
    value = clean_text(value, 80)
    if not value:
        raise RadarError("筛选值不能为空。")
    if re.fullmatch(r"[A-Za-z0-9_.+#-]+", value):
        return value
    return '"' + value.replace('"', "") + '"'


def build_search_query(since_date: dt.date, language: str, topic: str | None) -> str:
    parts = ["is:public", "fork:false", "archived:false", f"created:>={since_date.isoformat()}"]
    if language.casefold() != "all":
        parts.append(f"language:{qualifier_value(language)}")
    if topic:
        normalized_topic = clean_text(topic, 80).strip().lower().replace(" ", "-")
        if not re.fullmatch(r"[a-z0-9_.-]+", normalized_topic):
            raise RadarError("Topic 只能包含字母、数字、点、下划线或连字符。")
        parts.append(f"topic:{normalized_topic}")
    return " ".join(parts)


def normalize_license(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "未知"
    value = clean_text(raw.get("spdx_id") or raw.get("name"), 80)
    return "未知" if not value or value == "NOASSERTION" else value


def normalize_repo(raw: dict[str, Any], source: str) -> dict[str, Any] | None:
    full_name = clean_text(raw.get("full_name"), 180)
    if not REPO_NAME_RE.fullmatch(full_name):
        return None
    if raw.get("private") is True or raw.get("fork") is True or raw.get("archived") is True:
        return None
    visibility = clean_text(raw.get("visibility"), 20).lower()
    if visibility and visibility != "public":
        return None
    topics = raw.get("topics") if isinstance(raw.get("topics"), list) else []
    return {
        "full_name": full_name,
        "url": clean_text(raw.get("html_url"), 500) or f"https://github.com/{full_name}",
        "description": clean_text(raw.get("description"), 500),
        "language": clean_text(raw.get("language"), 80) or "未知",
        "stars": int(raw.get("stargazers_count") or 0),
        "forks": int(raw.get("forks_count") or 0),
        "open_issues": int(raw.get("open_issues_count") or 0),
        "created_at": clean_text(raw.get("created_at"), 40),
        "updated_at": clean_text(raw.get("updated_at"), 40),
        "pushed_at": clean_text(raw.get("pushed_at"), 40),
        "license": normalize_license(raw.get("license")),
        "topics": sorted({clean_text(item, 80).lower() for item in topics if clean_text(item, 80)}),
        "sources": [source],
        "stars_today": None,
    }


def merge_repo(base: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if base is None:
        result = dict(incoming)
        result["sources"] = sorted(set(incoming.get("sources", [])))
        return result
    result = dict(base)
    incoming_is_trending = any(
        str(source).startswith("github_trending_") for source in incoming.get("sources", [])
    )
    for key, value in incoming.items():
        if key == "sources":
            continue
        if key == "stars_today":
            if value is not None:
                result[key] = max(int(result.get(key) or 0), int(value))
            continue
        if (
            incoming_is_trending
            and key in {"stars", "forks", "open_issues"}
            and int(value or 0) == 0
            and int(result.get(key) or 0) > 0
        ):
            continue
        if value not in (None, "", [], "未知") or result.get(key) in (None, "", [], "未知"):
            result[key] = value
    result["sources"] = sorted(set(base.get("sources", [])) | set(incoming.get("sources", [])))
    return result


def search_new_repositories(
    since_date: dt.date, language: str, topic: str | None, per_page: int = 100
) -> list[dict[str, Any]]:
    payload = gh_api_get(
        "/search/repositories",
        {
            "q": build_search_query(since_date, language, topic),
            "sort": "stars",
            "order": "desc",
            "per_page": str(min(max(per_page, 1), 100)),
        },
        timeout=25,
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RadarError("GitHub Search 响应缺少 items 列表。")
    repos: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized = normalize_repo(item, "github_search_new")
            if normalized:
                repos.append(normalized)
    return repos


def strip_html(fragment: str) -> str:
    return clean_text(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)), 500)


def parse_trending_html(document: str, period: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    articles = re.findall(
        r'<article\b[^>]*class="[^"]*\bBox-row\b[^"]*"[^>]*>(.*?)</article>',
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for article in articles:
        heading_match = re.search(r"<h2\b.*?</h2>", article, flags=re.IGNORECASE | re.DOTALL)
        if not heading_match:
            continue
        link_match = re.search(
            r'href="/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"', heading_match.group(0)
        )
        if not link_match:
            continue
        full_name = f"{link_match.group(1)}/{link_match.group(2)}"
        delta_match = re.search(
            r"([\d,]+)\s+stars\s+(today|this week)", article, flags=re.IGNORECASE
        )
        description_match = re.search(
            r'<p\b[^>]*class="[^"]*color-fg-muted[^"]*"[^>]*>(.*?)</p>',
            article,
            flags=re.IGNORECASE | re.DOTALL,
        )
        language_match = re.search(
            r'<span\b[^>]*itemprop="programmingLanguage"[^>]*>(.*?)</span>',
            article,
            flags=re.IGNORECASE | re.DOTALL,
        )
        total_stars_match = re.search(
            rf'href="/{re.escape(full_name)}/stargazers"[^>]*>.*?</svg>\s*([\d,]+)\s*</a>',
            article,
            flags=re.IGNORECASE | re.DOTALL,
        )
        stars_today = None
        if delta_match and delta_match.group(2).lower() == "today":
            stars_today = int(delta_match.group(1).replace(",", ""))
        repos.append(
            {
                "full_name": full_name,
                "url": f"https://github.com/{full_name}",
                "description": strip_html(description_match.group(1)) if description_match else "",
                "language": strip_html(language_match.group(1)) if language_match else "未知",
                "stars": int(total_stars_match.group(1).replace(",", "")) if total_stars_match else 0,
                "forks": 0,
                "open_issues": 0,
                "created_at": "",
                "updated_at": "",
                "pushed_at": "",
                "license": "未知",
                "topics": [],
                "sources": [f"github_trending_{period}"],
                "stars_today": stars_today,
            }
        )
    return repos


def trending_url(period: str) -> str:
    if period not in {"daily", "weekly"}:
        raise RadarError(f"不支持的 Trending 周期：{period}")
    return "https://github.com/trending?" + urllib.parse.urlencode({"since": period})


def fetch_trending(period: str, timeout: int = 25) -> list[dict[str, Any]]:
    url = trending_url(period)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"github-radar/{VERSION} (+local-read-only)",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = response.read(3_000_000).decode("utf-8", errors="replace")
    except Exception as exc:
        raise RadarError(f"GitHub Trending {period} 读取失败：{safe_error(exc)}") from exc
    repos = parse_trending_html(document, period)
    if not repos:
        raise RadarError(f"GitHub Trending {period} 页面未解析到仓库。")
    return repos


def fetch_repo_metadata(full_name: str) -> dict[str, Any] | None:
    validate_repo_name(full_name)
    raw = gh_api_get(f"/repos/{full_name}")
    if not isinstance(raw, dict):
        return None
    return normalize_repo(raw, "github_rest_repo")


def fetch_repo_metadata_batch(names: Iterable[str], chunk_size: int = 50) -> dict[str, dict[str, Any]]:
    """Fetch public repository metadata in read-only GraphQL batches."""
    validated = [validate_repo_name(name) for name in dict.fromkeys(names)]
    results: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(validated), chunk_size):
        chunk = validated[offset : offset + chunk_size]
        selections: list[str] = []
        for index, full_name in enumerate(chunk):
            owner, name = full_name.split("/", 1)
            selections.append(
                f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ "
                "nameWithOwner url description primaryLanguage { name } stargazerCount forkCount "
                "issues(states: OPEN) { totalCount } createdAt updatedAt pushedAt "
                "licenseInfo { spdxId } repositoryTopics(first: 20) { nodes { topic { name } } } "
                "isPrivate isFork isArchived visibility }"
            )
        query = "query GitHubRadarPublicMetadata { " + " ".join(selections) + " }"
        payload = gh_graphql_query(query)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RadarError("GitHub GraphQL 响应缺少 data。")
        for index, requested_name in enumerate(chunk):
            node = data.get(f"r{index}")
            if not isinstance(node, dict):
                continue
            topics_node = node.get("repositoryTopics")
            topic_nodes = topics_node.get("nodes", []) if isinstance(topics_node, dict) else []
            topics = []
            for topic_node in topic_nodes if isinstance(topic_nodes, list) else []:
                topic = topic_node.get("topic") if isinstance(topic_node, dict) else None
                if isinstance(topic, dict) and topic.get("name"):
                    topics.append(topic["name"])
            primary_language = node.get("primaryLanguage")
            issues = node.get("issues")
            license_info = node.get("licenseInfo")
            raw = {
                "full_name": node.get("nameWithOwner") or requested_name,
                "html_url": node.get("url"),
                "description": node.get("description"),
                "language": primary_language.get("name") if isinstance(primary_language, dict) else None,
                "stargazers_count": node.get("stargazerCount"),
                "forks_count": node.get("forkCount"),
                "open_issues_count": issues.get("totalCount") if isinstance(issues, dict) else 0,
                "created_at": node.get("createdAt"),
                "updated_at": node.get("updatedAt"),
                "pushed_at": node.get("pushedAt"),
                "license": {"spdx_id": license_info.get("spdxId")} if isinstance(license_info, dict) else None,
                "topics": topics,
                "private": node.get("isPrivate"),
                "fork": node.get("isFork"),
                "archived": node.get("isArchived"),
                "visibility": str(node.get("visibility") or "").lower(),
            }
            normalized = normalize_repo(raw, "github_graphql_repo")
            if normalized:
                results[requested_name] = normalized
    return results


class RadarStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "radar.sqlite3"
        try:
            self.connection = sqlite3.connect(self.path, timeout=15)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise RadarError(
                f"历史数据库无法读取：{self.path}。已停止以避免覆盖；修复或重建前需要人工确认。"
            ) from exc

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS repositories (
                full_name TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                description TEXT NOT NULL,
                language TEXT NOT NULL,
                stars INTEGER NOT NULL,
                forks INTEGER NOT NULL,
                open_issues INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                pushed_at TEXT NOT NULL,
                license TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                full_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                stars INTEGER NOT NULL,
                source TEXT NOT NULL,
                run_id TEXT NOT NULL,
                PRIMARY KEY (full_name, observed_at),
                FOREIGN KEY (full_name) REFERENCES repositories(full_name)
            );
            CREATE INDEX IF NOT EXISTS observations_lookup
                ON observations(full_name, observed_at);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                warning_count INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def active_candidate_names(self, now: dt.datetime, limit: int = 60) -> list[str]:
        cutoff = iso_utc(now - dt.timedelta(days=2))
        rows = self.connection.execute(
            """
            SELECT full_name FROM repositories
            WHERE last_seen >= ?
            ORDER BY stars DESC, last_seen DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [str(row["full_name"]) for row in rows]

    def known_names(self, names: Iterable[str]) -> set[str]:
        unique = sorted(set(names))
        if not unique:
            return set()
        placeholders = ",".join("?" for _ in unique)
        rows = self.connection.execute(
            f"SELECT full_name FROM repositories WHERE full_name IN ({placeholders})", unique
        ).fetchall()
        return {str(row["full_name"]) for row in rows}

    def prior_snapshot(
        self, full_name: str, now: dt.datetime, growth_hours: int
    ) -> tuple[str, int] | None:
        tolerance_hours = max(4.0, min(24.0, growth_hours * 0.10))
        target = now - dt.timedelta(hours=growth_hours)
        lower = target - dt.timedelta(hours=tolerance_hours)
        upper = target + dt.timedelta(hours=tolerance_hours)
        row = self.connection.execute(
            """
            SELECT observed_at, stars FROM observations
            WHERE full_name = ? AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(strftime('%s', observed_at) - ?) ASC
            LIMIT 1
            """,
            (full_name, iso_utc(lower), iso_utc(upper), int(target.timestamp())),
        ).fetchone()
        if row is None:
            return None
        return str(row["observed_at"]), int(row["stars"])

    def save_success(
        self,
        repos: Iterable[dict[str, Any]],
        now: dt.datetime,
        run_id: str,
        warnings: list[str],
    ) -> None:
        observed_at = iso_utc(now)
        status = "partial" if warnings else "success"
        try:
            with self.connection:
                for repo in repos:
                    existing = self.connection.execute(
                        "SELECT first_seen FROM repositories WHERE full_name = ?",
                        (repo["full_name"],),
                    ).fetchone()
                    first_seen = str(existing["first_seen"]) if existing else observed_at
                    self.connection.execute(
                        """
                        INSERT INTO repositories(
                            full_name, url, description, language, stars, forks, open_issues,
                            created_at, updated_at, pushed_at, license, topics_json,
                            sources_json, first_seen, last_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(full_name) DO UPDATE SET
                            url=excluded.url,
                            description=excluded.description,
                            language=excluded.language,
                            stars=excluded.stars,
                            forks=excluded.forks,
                            open_issues=excluded.open_issues,
                            created_at=excluded.created_at,
                            updated_at=excluded.updated_at,
                            pushed_at=excluded.pushed_at,
                            license=excluded.license,
                            topics_json=excluded.topics_json,
                            sources_json=excluded.sources_json,
                            last_seen=excluded.last_seen
                        """,
                        (
                            repo["full_name"],
                            repo["url"],
                            repo["description"],
                            repo["language"],
                            repo["stars"],
                            repo["forks"],
                            repo["open_issues"],
                            repo["created_at"],
                            repo["updated_at"],
                            repo["pushed_at"],
                            repo["license"],
                            json.dumps(repo["topics"], ensure_ascii=False),
                            json.dumps(repo["sources"], ensure_ascii=False),
                            first_seen,
                            observed_at,
                        ),
                    )
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO observations(
                            full_name, observed_at, stars, source, run_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            repo["full_name"],
                            observed_at,
                            int(repo["stars"]),
                            ",".join(repo["sources"]),
                            run_id,
                        ),
                    )
                retention_cutoff = iso_utc(now - dt.timedelta(days=90))
                self.connection.execute(
                    "DELETE FROM observations WHERE observed_at < ?", (retention_cutoff,)
                )
                self.connection.execute(
                    """
                    INSERT INTO runs(run_id, started_at, completed_at, status, warning_count)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, observed_at, iso_utc(utc_now()), status, len(warnings)),
                )
        except sqlite3.DatabaseError as exc:
            raise RadarError("保存历史快照失败；本次数据未作为有效基线写入。") from exc


def repo_matches_scope(repo: dict[str, Any], language: str, topic: str | None) -> bool:
    if language.casefold() != "all" and repo.get("language", "").casefold() != language.casefold():
        return False
    if topic:
        normalized = topic.strip().lower().replace(" ", "-")
        if normalized not in {str(item).lower() for item in repo.get("topics", [])}:
            return False
    return True


def build_new_item(
    repo: dict[str, Any], now: dt.datetime, first_seen_today: bool
) -> dict[str, Any] | None:
    created = parse_timestamp(repo.get("created_at"))
    if created is None:
        return None
    age_days = max((now - created).total_seconds() / 86400.0, 1.0 / 24.0)
    return {
        "full_name": repo["full_name"],
        "url": repo["url"],
        "description": repo["description"],
        "language": repo["language"],
        "stars": int(repo["stars"]),
        "stars_per_day": round(int(repo["stars"]) / age_days, 1),
        "created_at": repo["created_at"],
        "age_days": round(age_days, 1),
        "license": repo["license"],
        "topics": repo["topics"],
        "first_seen_today": first_seen_today,
        "updated_at": repo["updated_at"],
    }


def build_growth_item(
    repo: dict[str, Any], store: RadarStore, now: dt.datetime, growth_hours: int
) -> dict[str, Any] | None:
    baseline = store.prior_snapshot(repo["full_name"], now, growth_hours)
    source = ""
    baseline_at: str | None = None
    delta: int | None = None
    growth_rate: float | None = None
    if baseline is not None:
        baseline_at, baseline_stars = baseline
        delta = int(repo["stars"]) - baseline_stars
        growth_rate = round((delta / baseline_stars) * 100, 2) if baseline_stars > 0 else None
        source = f"snapshot_{growth_hours}h"
    elif growth_hours == 24 and repo.get("stars_today") is not None:
        delta = int(repo["stars_today"])
        estimated_baseline = max(int(repo["stars"]) - delta, 0)
        growth_rate = round((delta / estimated_baseline) * 100, 2) if estimated_baseline > 0 else None
        source = "github_trending_daily"
    if delta is None or delta <= 0:
        return None
    return {
        "full_name": repo["full_name"],
        "url": repo["url"],
        "description": repo["description"],
        "language": repo["language"],
        "stars": int(repo["stars"]),
        "star_delta": delta,
        "growth_rate": growth_rate,
        "growth_source": source,
        "baseline_at": baseline_at,
        "pushed_at": repo["pushed_at"],
        "license": repo["license"],
        "topics": repo["topics"],
    }


def rank_new_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        items,
        key=lambda item: (
            int(item["stars"]),
            float(item["stars_per_day"]),
            str(item.get("updated_at", "")),
        ),
        reverse=True,
    )[:limit]
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def rank_growth_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        items,
        key=lambda item: (
            int(item["star_delta"]),
            float(item["growth_rate"] if item["growth_rate"] is not None else -1.0),
            str(item.get("pushed_at", "")),
        ),
        reverse=True,
    )[:limit]
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def make_highlights(
    new_items: list[dict[str, Any]], growth_items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    highlights: list[dict[str, str]] = []
    used: set[str] = set()
    if new_items:
        item = new_items[0]
        highlights.append(
            {
                "full_name": item["full_name"],
                "url": item["url"],
                "reason": f"近期开源项目总 Star 最高（{item['stars']:,} Star）",
            }
        )
        used.add(item["full_name"])
    if growth_items:
        item = growth_items[0]
        label = "本地快照实测" if item["growth_source"].startswith("snapshot_") else "GitHub Trending 当日口径"
        highlights.append(
            {
                "full_name": item["full_name"],
                "url": item["url"],
                "reason": f"增长榜领先（+{item['star_delta']:,}，{label}）",
            }
        )
        used.add(item["full_name"])
    velocity = sorted(new_items, key=lambda item: float(item["stars_per_day"]), reverse=True)
    for item in velocity:
        if item["full_name"] not in used:
            highlights.append(
                {
                    "full_name": item["full_name"],
                    "url": item["url"],
                    "reason": f"新项目日均 Star 突出（约 {item['stars_per_day']:,.1f}/日）",
                }
            )
            break
    return highlights[:3]


def md_escape(value: Any, limit: int = 90) -> str:
    text = clean_text(value, limit).replace("|", "\\|")
    return text or "—"


def render_markdown(report: dict[str, Any]) -> str:
    scope = report["scope"]
    lines = [
        f"# GitHub Radar · {report['generated_at'][:10]}",
        "",
        f"_生成时间：{report['generated_at']} · 范围：{scope['language_label']}"
        + (f" · Topic: `{scope['topic']}`" if scope["topic"] else "")
        + "_",
        "",
        "> 增速榜仅覆盖 GitHub Trending、近期新项目和本地追踪候选，不代表全 GitHub 的穷尽排名。",
        "",
        f"## 近 {scope['new_window_days']} 日新项目 Top {scope['limit']}",
        "",
    ]
    new_items = report["new_projects"]
    if new_items:
        lines.extend(
            [
                "| # | 项目 | 简介 | 语言 | Star | 日均 Star | 创建时间 | 许可证 | 状态 |",
                "|---:|---|---|---|---:|---:|---|---|---|",
            ]
        )
        for item in new_items:
            status = "🆕 首次收录" if item["first_seen_today"] else "持续入榜"
            lines.append(
                f"| {item['rank']} | [{md_escape(item['full_name'], 100)}]({item['url']}) | "
                f"{md_escape(item['description'])} | {md_escape(item['language'], 40)} | "
                f"{item['stars']:,} | {item['stars_per_day']:,.1f} | "
                f"{md_escape(item['created_at'][:10], 20)} | {md_escape(item['license'], 30)} | {status} |"
            )
    else:
        lines.append("本次没有取得符合条件的新项目。")

    lines.extend(
        [
            "",
            f"## 过去 {scope['growth_hours']} 小时增长 Top {scope['limit']}",
            "",
        ]
    )
    growth_items = report["fast_growth"]
    if growth_items:
        lines.extend(
            [
                "| # | 项目 | 简介 | 语言 | 当前 Star | 增量 | 增长率 | 口径 |",
                "|---:|---|---|---|---:|---:|---:|---|",
            ]
        )
        for item in growth_items:
            rate = f"{item['growth_rate']:.2f}%" if item["growth_rate"] is not None else "—"
            source = "本地快照实测" if item["growth_source"].startswith("snapshot_") else "Trending 当日回退"
            lines.append(
                f"| {item['rank']} | [{md_escape(item['full_name'], 100)}]({item['url']}) | "
                f"{md_escape(item['description'])} | {md_escape(item['language'], 40)} | "
                f"{item['stars']:,} | +{item['star_delta']:,} | {rate} | {source} |"
            )
    else:
        lines.append("暂无可验证的增长数据；本次快照已建立，后续运行将用于计算增量。")

    lines.extend(["", "## 今日值得关注", ""])
    if report["highlights"]:
        for highlight in report["highlights"]:
            lines.append(
                f"- [{md_escape(highlight['full_name'], 100)}]({highlight['url']})：{md_escape(highlight['reason'], 160)}"
            )
    else:
        lines.append("- 本次数据不足，未生成重点摘要。")

    if report["warnings"]:
        lines.extend(["", "## 数据提示", ""])
        lines.extend(f"- {md_escape(warning, 260)}" for warning in report["warnings"])

    lines.extend(
        [
            "",
            "## 数据来源",
            "",
            "- GitHub REST API：公开仓库搜索与元数据（只读 GET）。",
            "- GitHub Trending：Daily/Weekly 候选发现；页面不可用时降级为 API 与本地快照。",
            f"- 本地历史：`{md_escape(report['data_sources'][-1]['path'], 240)}`（保留 90 天观测）。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def collect_report(args: argparse.Namespace, now: dt.datetime | None = None) -> dict[str, Any]:
    now = (now or utc_now()).astimezone(dt.timezone.utc)
    try:
        timezone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as exc:
        raise RadarError(f"未知时区：{args.timezone}") from exc
    local_now = now.astimezone(timezone)
    since_date = local_now.date() - dt.timedelta(days=args.new_window_days)
    store = RadarStore(Path(args.state_dir))
    warnings: list[str] = []
    run_id = str(uuid.uuid4())
    repos_by_name: dict[str, dict[str, Any]] = {}
    daily_available = False
    weekly_available = False
    search_available = False
    try:
        try:
            for repo in search_new_repositories(since_date, args.language, args.topic):
                repos_by_name[repo["full_name"]] = merge_repo(
                    repos_by_name.get(repo["full_name"]), repo
                )
            search_available = True
        except RadarError as exc:
            warnings.append(safe_error(exc))

        trending_names: set[str] = set()
        for period in ("daily", "weekly"):
            try:
                trending_repos = fetch_trending(period)
                if period == "daily":
                    daily_available = True
                else:
                    weekly_available = True
                for repo in trending_repos:
                    trending_names.add(repo["full_name"])
                    repos_by_name[repo["full_name"]] = merge_repo(
                        repos_by_name.get(repo["full_name"]), repo
                    )
            except RadarError as exc:
                warnings.append(safe_error(exc))

        active_names = set(store.active_candidate_names(now))
        metadata_names = sorted((trending_names | active_names) - set(repos_by_name))
        metadata_names.extend(
            sorted(
                name
                for name in trending_names
                if repos_by_name.get(name, {}).get("stars", 0) == 0
                and name not in metadata_names
            )
        )
        metadata_names = list(dict.fromkeys(metadata_names))[:120]
        failures: list[str] = []
        if metadata_names:
            try:
                batch_metadata = fetch_repo_metadata_batch(metadata_names)
                for name, metadata in batch_metadata.items():
                    repos_by_name[name] = merge_repo(repos_by_name.get(name), metadata)
                failures.extend(name for name in metadata_names if name not in batch_metadata)
            except RadarError:
                # Trending HTML still provides repository names, total Stars,
                # and same-day deltas. Keep that limited data rather than
                # falling into many slow per-repository requests.
                failures.extend(metadata_names)
        if failures:
            warnings.append(
                f"{len(failures)} 个候选仓库的元数据读取失败；已跳过或使用页面中的有限字段。"
            )

        complete_repos = [
            repo
            for repo in repos_by_name.values()
            if int(repo.get("stars", 0)) >= 0
            and REPO_NAME_RE.fullmatch(str(repo.get("full_name", "")))
            and repo_matches_scope(repo, args.language, args.topic)
        ]
        if not complete_repos:
            raise RadarError("没有取得可用的公开仓库数据；未写入本次快照。")

        known = store.known_names(repo["full_name"] for repo in complete_repos)
        new_candidates: list[dict[str, Any]] = []
        growth_candidates: list[dict[str, Any]] = []
        cutoff = now - dt.timedelta(days=args.new_window_days)
        for repo in complete_repos:
            created = parse_timestamp(repo.get("created_at"))
            if created is not None and created >= cutoff:
                item = build_new_item(repo, now, repo["full_name"] not in known)
                if item:
                    new_candidates.append(item)
            growth = build_growth_item(repo, store, now, args.growth_hours)
            if growth:
                growth_candidates.append(growth)

        new_items = rank_new_items(new_candidates, args.limit)
        growth_items = rank_growth_items(growth_candidates, args.limit)
        if not growth_items:
            warnings.append("尚无可用增长基线；已保存本次快照供后续比较。")
        elif all(item["growth_source"] == "github_trending_daily" for item in growth_items):
            warnings.append("本次增长榜使用 GitHub Trending 的 stars today 回退口径；后续将优先采用本地快照实测。")

        report = {
            "generated_at": local_now.replace(microsecond=0).isoformat(),
            "scope": {
                "language": args.language,
                "language_label": "全部公开项目" if args.language.casefold() == "all" else args.language,
                "topic": args.topic,
                "new_window_days": args.new_window_days,
                "growth_hours": args.growth_hours,
                "limit": args.limit,
                "timezone": args.timezone,
                "growth_candidate_universe": [
                    "github_trending_daily",
                    "github_trending_weekly",
                    "recent_new_repositories",
                    "local_recent_candidates",
                ],
            },
            "new_projects": new_items,
            "fast_growth": growth_items,
            "highlights": make_highlights(new_items, growth_items),
            "warnings": warnings,
            "data_sources": [
                {
                    "name": "GitHub REST API",
                    "url": "https://api.github.com",
                    "available": search_available,
                    "access": "public-read-only",
                },
                {
                    "name": "GitHub Trending Daily",
                    "url": trending_url("daily"),
                    "available": daily_available,
                },
                {
                    "name": "GitHub Trending Weekly",
                    "url": trending_url("weekly"),
                    "available": weekly_available,
                },
                {"name": "Local SQLite history", "path": str(store.path)},
            ],
        }
        store.save_success(complete_repos, now, run_id, warnings)
        return report
    finally:
        store.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成公开 GitHub 新项目与候选范围内 Star 增长榜（只读）。"
    )
    parser.add_argument("--version", action="version", version=f"GitHub Radar {VERSION}")
    parser.add_argument("--limit", type=int, default=10, help="每个榜单的项目数（1-50）")
    parser.add_argument("--language", default="all", help="语言筛选，默认 all")
    parser.add_argument("--topic", default=None, help="GitHub topic 筛选")
    parser.add_argument("--new-window-days", type=int, default=7, help="新项目窗口天数")
    parser.add_argument("--growth-hours", type=int, default=24, help="增长比较窗口小时数")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="报告时区")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="快照数据库目录")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 50:
        parser.error("--limit 必须在 1 到 50 之间")
    if not 1 <= args.new_window_days <= 365:
        parser.error("--new-window-days 必须在 1 到 365 之间")
    if not 1 <= args.growth_hours <= 24 * 365:
        parser.error("--growth-hours 必须在 1 到 8760 之间")
    args.language = clean_text(args.language, 80) or "all"
    args.topic = clean_text(args.topic, 80) or None
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = collect_report(args)
    except RadarError as exc:
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "generated_at": iso_utc(utc_now()),
                        "scope": {},
                        "new_projects": [],
                        "fast_growth": [],
                        "highlights": [],
                        "warnings": [safe_error(exc)],
                        "data_sources": [],
                        "error": safe_error(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"# GitHub Radar 运行失败\n\n- {safe_error(exc)}")
        return 1
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
