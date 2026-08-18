from __future__ import annotations

import argparse
import base64
import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "github_radar.py"
SPEC = importlib.util.spec_from_file_location("github_radar", SCRIPT)
radar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(radar)
FIXTURES = Path(__file__).parent / "fixtures"


class GitHubRadarTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 8, 19, 1, 0, tzinfo=dt.timezone.utc)
        self.search_payload = json.loads((FIXTURES / "search.json").read_text())

    def test_parse_trending_fixture(self):
        items = radar.parse_trending_html((FIXTURES / "trending.html").read_text(), "daily")
        self.assertEqual([item["full_name"] for item in items], ["example/alpha", "example/beta"])
        self.assertEqual(items[0]["stars_today"], 1234)
        self.assertEqual(items[0]["stars"], 9999)
        self.assertEqual(items[0]["description"], "Alpha & tools")
        self.assertIsNone(items[1]["stars_today"])

    def test_trending_placeholder_does_not_erase_api_counts(self):
        api_repo = radar.normalize_repo(self.search_payload["items"][0], "github_search_new")
        assert api_repo
        trending_repo = radar.parse_trending_html(
            (FIXTURES / "trending.html").read_text(), "daily"
        )[0]
        trending_repo["stars"] = 0
        merged = radar.merge_repo(api_repo, trending_repo)
        self.assertEqual(merged["stars"], 120)
        self.assertEqual(merged["stars_today"], 1234)

    def test_read_only_endpoint_guard(self):
        self.assertEqual(radar.validate_api_endpoint("/search/repositories"), "/search/repositories")
        self.assertEqual(radar.validate_api_endpoint("/repos/example/alpha"), "/repos/example/alpha")
        self.assertEqual(
            radar.validate_api_endpoint("/repos/example/alpha/readme"),
            "/repos/example/alpha/readme",
        )
        for endpoint in (
            "/user/starred/example/alpha",
            "/repos/example/alpha/issues",
            "/repos/example/alpha/forks",
            "/orgs/example/repos",
        ):
            with self.assertRaises(radar.RadarError):
                radar.validate_api_endpoint(endpoint)
        self.assertEqual(
            radar.gh_graphql_query.__doc__,
            "Run an internally constructed read-only GraphQL query.",
        )
        with self.assertRaises(radar.RadarError):
            radar.gh_graphql_query("mutation { addStar(input: {}) { clientMutationId } }")

    def test_readme_one_liner_skips_title_badges_and_code(self):
        readme = """
# Alpha

[![Build](https://img.shields.io/badge/build-passing-green)](https://example.com)

Alpha turns natural-language tasks into reproducible command-line workflows. It keeps every run auditable.

```python
print("not an introduction")
```
"""
        self.assertEqual(
            radar.readme_one_liner(readme),
            "Alpha turns natural-language tasks into reproducible command-line workflows.",
        )

    def test_readme_one_liner_skips_language_switch_and_repeated_title(self):
        readme = """
# DeepSeek Harness

English | 中文

DeepSeek Harness Desktop（DSH Desktop）

这是一个帮助开发者在桌面上管理 AI 编程任务的开源工具。
"""
        self.assertEqual(
            radar.readme_one_liner(readme),
            "这是一个帮助开发者在桌面上管理 AI 编程任务的开源工具。",
        )

    def test_fetch_readme_introduction_decodes_public_contents(self):
        encoded = base64.b64encode("# Alpha\n\n面向开发者的轻量级任务编排工具。\n".encode()).decode()
        with mock.patch.object(
            radar,
            "gh_api_get",
            return_value={"encoding": "base64", "content": encoded},
        ) as api_get:
            summary = radar.fetch_readme_introduction("example/alpha")
        self.assertEqual(summary, "面向开发者的轻量级任务编排工具。")
        self.assertEqual(api_get.call_args.args[0], "/repos/example/alpha/readme")

    def test_readme_enrichment_falls_back_to_repository_description(self):
        items = [{"full_name": "example/alpha", "description": "Fallback description"}]
        with mock.patch.object(
            radar,
            "fetch_readme_introduction",
            side_effect=radar.RadarError("README unavailable"),
        ):
            fallback_count = radar.add_readme_introductions(items, [], workers=1)
        self.assertEqual(fallback_count, 1)
        self.assertEqual(items[0]["introduction"], "Fallback description")
        self.assertEqual(items[0]["introduction_source"], "repository_description")

    def test_graphql_batch_normalizes_public_metadata(self):
        payload = {
            "data": {
                "r0": {
                    "nameWithOwner": "example/alpha",
                    "url": "https://github.com/example/alpha",
                    "description": "Alpha",
                    "primaryLanguage": {"name": "Python"},
                    "stargazerCount": 150,
                    "forkCount": 4,
                    "issues": {"totalCount": 2},
                    "createdAt": "2026-08-17T00:00:00Z",
                    "updatedAt": "2026-08-19T00:00:00Z",
                    "pushedAt": "2026-08-19T00:00:00Z",
                    "licenseInfo": {"spdxId": "MIT"},
                    "repositoryTopics": {"nodes": [{"topic": {"name": "ai-agent"}}]},
                    "isPrivate": False,
                    "isFork": False,
                    "isArchived": False,
                    "visibility": "PUBLIC",
                }
            }
        }
        with mock.patch.object(radar, "gh_graphql_query", return_value=payload) as query:
            result = radar.fetch_repo_metadata_batch(["example/alpha"])
        self.assertIn("example/alpha", result)
        self.assertEqual(result["example/alpha"]["stars"], 150)
        self.assertNotIn("mutation", query.call_args.args[0].lower())

    def test_normalize_excludes_private_forks_and_archived(self):
        base = dict(self.search_payload["items"][0])
        for field in ("private", "fork", "archived"):
            candidate = dict(base)
            candidate[field] = True
            self.assertIsNone(radar.normalize_repo(candidate, "test"))

    def test_rank_new_uses_stars_then_velocity(self):
        items = [
            {"full_name": "x/a", "stars": 100, "stars_per_day": 10, "updated_at": "b"},
            {"full_name": "x/b", "stars": 100, "stars_per_day": 20, "updated_at": "a"},
            {"full_name": "x/c", "stars": 99, "stars_per_day": 50, "updated_at": "c"},
        ]
        ranked = radar.rank_new_items(items, 3)
        self.assertEqual([item["full_name"] for item in ranked], ["x/b", "x/a", "x/c"])

    def test_snapshot_growth_precedes_trending_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = radar.RadarStore(Path(directory))
            old_repo = radar.normalize_repo(self.search_payload["items"][0], "test")
            assert old_repo
            old_repo["stars"] = 100
            store.save_success([old_repo], self.now - dt.timedelta(hours=24), "old", [])
            current = dict(old_repo)
            current["stars"] = 125
            current["stars_today"] = 999
            item = radar.build_growth_item(current, store, self.now, 24)
            self.assertEqual(item["star_delta"], 25)
            self.assertEqual(item["growth_source"], "snapshot_24h")
            store.close()

    def test_first_run_uses_daily_trending_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = radar.RadarStore(Path(directory))
            repo = radar.normalize_repo(self.search_payload["items"][0], "test")
            assert repo
            repo["stars_today"] = 42
            item = radar.build_growth_item(repo, store, self.now, 24)
            self.assertEqual(item["star_delta"], 42)
            self.assertEqual(item["growth_source"], "github_trending_daily")
            self.assertIsNone(radar.build_growth_item(repo, store, self.now, 168))
            store.close()

    def test_topic_and_language_filters(self):
        repo = radar.normalize_repo(self.search_payload["items"][0], "test")
        assert repo
        self.assertTrue(radar.repo_matches_scope(repo, "Python", "ai-agent"))
        self.assertFalse(radar.repo_matches_scope(repo, "Rust", None))
        self.assertFalse(radar.repo_matches_scope(repo, "all", "database"))

    def test_corrupt_database_stops_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.sqlite3"
            path.write_bytes(b"not a sqlite database")
            with self.assertRaises(radar.RadarError):
                radar.RadarStore(Path(directory))
            self.assertEqual(path.read_bytes(), b"not a sqlite database")

    def test_partial_sources_still_produce_stable_json_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            normalized = [radar.normalize_repo(item, "github_search_new") for item in self.search_payload["items"]]
            search_items = [item for item in normalized if item]
            trending = radar.parse_trending_html((FIXTURES / "trending.html").read_text(), "daily")
            args = argparse.Namespace(
                limit=10,
                language="all",
                topic=None,
                new_window_days=7,
                growth_hours=24,
                timezone="Asia/Shanghai",
                format="json",
                state_dir=directory,
            )
            metadata = {item["full_name"]: item for item in search_items}

            def fake_metadata(name):
                return metadata.get(name)

            with mock.patch.object(radar, "search_new_repositories", return_value=search_items), mock.patch.object(
                radar, "fetch_trending", side_effect=[trending, radar.RadarError("weekly unavailable")]
            ), mock.patch.object(radar, "fetch_repo_metadata", side_effect=fake_metadata), mock.patch.object(
                radar, "add_readme_introductions", return_value=0
            ):
                report = radar.collect_report(args, now=self.now)
            self.assertEqual(
                set(report),
                {
                    "generated_at",
                    "scope",
                    "new_projects",
                    "fast_growth",
                    "highlights",
                    "warnings",
                    "data_sources",
                },
            )
            self.assertTrue(report["new_projects"])
            self.assertTrue(report["fast_growth"])
            self.assertTrue(any("weekly unavailable" in warning for warning in report["warnings"]))

    def test_failed_collection_does_not_write_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                limit=10,
                language="all",
                topic=None,
                new_window_days=7,
                growth_hours=24,
                timezone="Asia/Shanghai",
                format="json",
                state_dir=directory,
            )
            with mock.patch.object(radar, "search_new_repositories", side_effect=radar.RadarError("search down")), mock.patch.object(
                radar, "fetch_trending", side_effect=radar.RadarError("trending down")
            ):
                with self.assertRaises(radar.RadarError):
                    radar.collect_report(args, now=self.now)
            connection = sqlite3.connect(Path(directory) / "radar.sqlite3")
            count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            connection.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
