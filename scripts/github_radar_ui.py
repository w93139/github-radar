#!/usr/bin/env python3
"""Local, read-only web UI for GitHub Radar."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "ui"
COLLECTOR = ROOT / "scripts" / "github_radar.py"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "github-radar"
COLLECT_LOCK = threading.Lock()
SAFE_TEXT = re.compile(r"^[\w.+# -]{0,64}$", re.UNICODE)


def load_collector_module():
    spec = importlib.util.spec_from_file_location("github_radar_core", COLLECTOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法载入 GitHub Radar 采集器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RADAR_CORE = load_collector_module()

class RequestError(ValueError):
    pass


def parse_int(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise RequestError(f"{name} 必须是整数") from exc
    if not low <= value <= high:
        raise RequestError(f"{name} 必须在 {low}–{high} 之间")
    return value


def parse_filters(query: dict[str, list[str]]) -> dict[str, object]:
    language = query.get("language", ["all"])[0].strip() or "all"
    topic = query.get("topic", [""])[0].strip()
    if not SAFE_TEXT.fullmatch(language):
        raise RequestError("语言筛选包含不支持的字符")
    if not SAFE_TEXT.fullmatch(topic):
        raise RequestError("主题筛选包含不支持的字符")
    return {
        "limit": parse_int(query, "limit", 10, 1, 30),
        "language": language,
        "topic": topic,
        "new_window_days": parse_int(query, "new_window_days", 7, 1, 30),
        "growth_hours": parse_int(query, "growth_hours", 24, 1, 168),
    }


def cache_path(state_dir: Path, filters: dict[str, object]) -> Path:
    key = json.dumps(filters, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return state_dir / "ui-cache" / f"{hashlib.sha256(key).hexdigest()[:20]}.json"


def load_cache(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def cache_is_usable(report: dict[str, object]) -> bool:
    if not isinstance(report.get("_cache"), dict):
        return False
    warnings = [str(warning) for warning in report.get("warnings", [])]
    return not any("未找到 gh 命令" in warning for warning in warnings)


def cache_age_seconds(path: Path) -> float | None:
    try:
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - modified).total_seconds())
    except OSError:
        return None


def save_cache(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="report-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def report_needs_enrichment(report: dict[str, object]) -> bool:
    items = list(report.get("new_projects", [])) + list(report.get("fast_growth", []))
    return any(
        isinstance(item, dict) and item.get("introduction_source") == "repository_description"
        for item in items
    )


def collect(
    filters: dict[str, object], state_dir: Path, readme_mode: str = "cached"
) -> dict[str, object]:
    command = [
        sys.executable,
        str(COLLECTOR),
        "--limit", str(filters["limit"]),
        "--language", str(filters["language"]),
        "--new-window-days", str(filters["new_window_days"]),
        "--growth-hours", str(filters["growth_hours"]),
        "--timezone", "Asia/Shanghai",
        "--format", "json",
        "--state-dir", str(state_dir),
        "--readme-mode", readme_mode,
    ]
    if readme_mode == "cached":
        command.extend(["--candidate-limit", "30"])
    if filters["topic"]:
        command.extend(["--topic", str(filters["topic"])])
    with COLLECT_LOCK:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "采集器执行失败"
        raise RuntimeError(detail[-1000:])
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("采集器返回了无法解析的数据") from exc
    if not isinstance(report, dict):
        raise RuntimeError("采集器返回格式不正确")
    report["_cache"] = {
        "readme_enriched": readme_mode == "full" and not report_needs_enrichment(report),
        "fast_preview": readme_mode == "cached",
    }
    save_cache(cache_path(state_dir, filters), report)
    return report


def enrich_cached_report(filters: dict[str, object], state_dir: Path) -> dict[str, object]:
    path = cache_path(state_dir, filters)
    report = load_cache(path)
    if report is None:
        return collect(filters, state_dir, readme_mode="cached")
    store = RADAR_CORE.RadarStore(state_dir)
    try:
        fallback_count = RADAR_CORE.add_readme_introductions(
            report.get("new_projects", []),
            report.get("fast_growth", []),
            store=store,
            fetch_missing=True,
        )
    finally:
        store.close()
    warnings = [
        warning for warning in report.get("warnings", [])
        if "README 简介" not in str(warning)
    ]
    if fallback_count:
        warnings.append(
            f"{fallback_count} 个榜单条目没有可提取的 README 简介；已回退到 GitHub 仓库简介。"
        )
    report["warnings"] = warnings
    report["_cache"] = {"readme_enriched": True, "fast_preview": False}
    save_cache(path, report)
    return report


def local_preview(filters: dict[str, object], state_dir: Path) -> dict[str, object]:
    now = RADAR_CORE.utc_now()
    store = RADAR_CORE.RadarStore(state_dir)
    try:
        conditions = ["last_seen >= ?"]
        parameters: list[object] = [RADAR_CORE.iso_utc(now - dt.timedelta(days=14))]
        language = str(filters["language"])
        topic = str(filters["topic"])
        if language.casefold() != "all":
            conditions.append("language = ? COLLATE NOCASE")
            parameters.append(language)
        if topic:
            normalized_topic = topic.strip().lower().replace(" ", "-")
            conditions.append("topics_json LIKE ?")
            parameters.append(f'%"{normalized_topic}"%')
        rows = store.connection.execute(
            f"SELECT * FROM repositories WHERE {' AND '.join(conditions)} ORDER BY stars DESC, last_seen DESC LIMIT 300",
            parameters,
        ).fetchall()

        repositories: list[dict[str, object]] = []
        for row in rows:
            repositories.append({
                "full_name": str(row["full_name"]),
                "url": str(row["url"]),
                "description": str(row["description"]),
                "language": str(row["language"]),
                "stars": int(row["stars"]),
                "forks": int(row["forks"]),
                "open_issues": int(row["open_issues"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "pushed_at": str(row["pushed_at"]),
                "license": str(row["license"]),
                "topics": json.loads(str(row["topics_json"])),
                "sources": json.loads(str(row["sources_json"])),
                "stars_today": None,
            })

        cutoff = now - dt.timedelta(days=int(filters["new_window_days"]))
        new_candidates = []
        growth_candidates = []
        for repo in repositories:
            created = RADAR_CORE.parse_timestamp(repo.get("created_at"))
            if created is not None and created >= cutoff:
                item = RADAR_CORE.build_new_item(repo, now, False)
                if item:
                    new_candidates.append(item)
            growth = RADAR_CORE.build_growth_item(
                repo, store, now, int(filters["growth_hours"])
            )
            if growth:
                growth_candidates.append(growth)
        limit = int(filters["limit"])
        new_items = RADAR_CORE.rank_new_items(new_candidates, limit)
        growth_items = RADAR_CORE.rank_growth_items(growth_candidates, limit)
        RADAR_CORE.add_readme_introductions(
            new_items, growth_items, store=store, fetch_missing=False
        )
        timezone = ZoneInfo("Asia/Shanghai")
        return {
            "generated_at": now.astimezone(timezone).isoformat(),
            "scope": {
                "language": language,
                "language_label": "全部公开项目" if language.casefold() == "all" else language,
                "topic": topic or None,
                "new_window_days": int(filters["new_window_days"]),
                "growth_hours": int(filters["growth_hours"]),
                "limit": limit,
                "timezone": "Asia/Shanghai",
            },
            "new_projects": new_items,
            "fast_growth": growth_items,
            "highlights": RADAR_CORE.make_highlights(new_items, growth_items),
            "warnings": [],
            "data_sources": [{"name": "Local SQLite preview", "path": str(store.path)}],
            "_cache": {"readme_enriched": True, "fast_preview": True},
        }
    finally:
        store.close()


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "GitHubRadarMini/1.0"

    @property
    def state_dir(self) -> Path:
        return self.server.state_dir  # type: ignore[attr-defined]

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # WebKit can cancel an obsolete request when filters change. That is
            # a normal client disconnect and must not look like a service crash.
            return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"service": "github-radar", "status": "ok"})
            return
        if parsed.path == "/api/report":
            self.handle_report(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path == "/assets/logo.png":
            self.send_file(ROOT / "assets" / "logo.png")
            return
        if parsed.path == "/assets/icon.png":
            self.send_file(ROOT / "assets" / "icon.png")
            return
        relative = "index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/")
        candidate = (UI_DIR / relative).resolve()
        if UI_DIR.resolve() not in candidate.parents and candidate != UI_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_file(candidate)

    def handle_report(self, query: dict[str, list[str]]) -> None:
        cached: dict[str, object] | None = None
        age: float | None = None
        try:
            filters = parse_filters(query)
            path = cache_path(self.state_dir, filters)
            cached = load_cache(path)
            if cached is not None and not cache_is_usable(cached):
                cached = None
            age = cache_age_seconds(path)
            prefer_cache = query.get("prefer_cache", ["0"])[0] == "1"
            force = query.get("force", ["0"])[0] == "1"
            enrich = query.get("enrich", ["0"])[0] == "1"
            local = query.get("local", ["0"])[0] == "1"
            if local:
                report = local_preview(filters, self.state_dir)
                report["_ui"] = {
                    "cached": True,
                    "local_preview": True,
                    "needs_enrichment": False,
                }
                self.send_json(report)
                return
            if enrich:
                report = enrich_cached_report(filters, self.state_dir)
                report["_ui"] = {"cached": False, "needs_enrichment": False}
                self.send_json(report)
                return
            cache_is_fresh = age is not None and age <= 900
            if cached is not None and (prefer_cache or (cache_is_fresh and not force)):
                cached["_ui"] = {
                    "cached": True,
                    "cache_age_seconds": round(age or 0),
                    "needs_enrichment": bool(cached.get("_cache", {}).get("fast_preview"))
                    or not bool(cached.get("_cache", {}).get("readme_enriched")),
                }
                self.send_json(cached)
                return
            report = collect(filters, self.state_dir, readme_mode="cached")
            report["_ui"] = {
                "cached": False,
                "needs_enrichment": report_needs_enrichment(report),
            }
            self.send_json(report)
        except RequestError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except subprocess.TimeoutExpired:
            if cached is not None:
                warnings = list(cached.get("warnings", []))
                warnings.append("GitHub 扫描超时；当前显示最近一次有效缓存。")
                cached["warnings"] = warnings
                cached["_ui"] = {
                    "cached": True,
                    "cache_age_seconds": round(age or 0),
                    "degraded": True,
                    "needs_enrichment": False,
                }
                self.send_json(cached)
            else:
                self.send_json({"error": "GitHub 扫描超时，请稍后重试"}, HTTPStatus.GATEWAY_TIMEOUT)
        except RuntimeError as exc:
            if cached is not None:
                warnings = list(cached.get("warnings", []))
                warnings.append(f"最新扫描暂不可用；当前显示最近一次有效缓存。原因：{exc}")
                cached["warnings"] = warnings
                cached["_ui"] = {
                    "cached": True,
                    "cache_age_seconds": round(age or 0),
                    "degraded": True,
                    "needs_enrichment": False,
                }
                self.send_json(cached)
            else:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, message: str, *args: object) -> None:
        print(f"[GitHub Radar] {message % args}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 GitHub Radar 本地 Mini")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ready-file", type=Path, help="服务就绪后写入端口和进程信息")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return parser


def write_ready_file(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"service": "github-radar", "port": port, "pid": os.getpid()})
    fd, temp_name = tempfile.mkstemp(prefix="ready-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    args = build_parser().parse_args()
    if args.port != 0 and not 1024 <= args.port <= 65535:
        raise SystemExit("--port 必须为 0，或在 1024–65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), RadarHandler)
    server.daemon_threads = True
    server.state_dir = args.state_dir.resolve()  # type: ignore[attr-defined]
    actual_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{actual_port}"
    if args.ready_file:
        write_ready_file(args.ready_file.resolve(), actual_port)
    print(f"GitHub Radar Mini 已启动：{url}", flush=True)
    print("按 Control-C 停止。服务仅监听本机地址。", flush=True)
    if not args.no_open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    def stop_server(_signum: int, _frame: object) -> None:
        # HTTPServer.shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if args.ready_file:
            try:
                args.ready_file.unlink()
            except FileNotFoundError:
                pass
        print("GitHub Radar Mini 已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
