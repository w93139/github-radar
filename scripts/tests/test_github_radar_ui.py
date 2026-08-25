from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "github_radar_ui.py"
SPEC = importlib.util.spec_from_file_location("github_radar_ui", SCRIPT)
ui = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ui)


class GitHubRadarUITests(unittest.TestCase):
    def test_default_filters(self):
        self.assertEqual(
            ui.parse_filters({}),
            {"limit": 10, "language": "all", "topic": "", "new_window_days": 7, "growth_hours": 24},
        )

    def test_filter_validation(self):
        with self.assertRaises(ui.RequestError):
            ui.parse_filters({"limit": ["100"]})
        with self.assertRaises(ui.RequestError):
            ui.parse_filters({"language": ["Rust; touch /tmp/no"]})
        with self.assertRaises(ui.RequestError):
            ui.parse_filters({"topic": ["a/b"]})

    def test_cache_round_trip_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache" / "report.json"
            ui.save_cache(path, {"generated_at": "2026-08-25T09:00:00+08:00"})
            self.assertEqual(ui.load_cache(path)["generated_at"], "2026-08-25T09:00:00+08:00")
            path.write_text("broken", encoding="utf-8")
            self.assertIsNone(ui.load_cache(path))

    def test_cache_age_is_available_for_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("{}", encoding="utf-8")
            age = ui.cache_age_seconds(path)
            self.assertIsNotNone(age)
            self.assertGreaterEqual(age, 0)
        self.assertLess(age, 5)

    def test_ready_file_records_dynamic_port_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "ready.json"
            ui.write_ready_file(path, 54321)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["service"], "github-radar")
        self.assertEqual(payload["port"], 54321)
        self.assertEqual(payload["pid"], os.getpid())

    def test_parser_accepts_ephemeral_port(self):
        parser = ui.build_parser()
        args = parser.parse_args(["--port", "0", "--ready-file", "/tmp/radar-ready.json"])
        self.assertEqual(args.port, 0)
        self.assertEqual(args.ready_file, Path("/tmp/radar-ready.json"))

    def test_local_preview_is_immediate_and_network_free(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ui.subprocess, "run"
        ) as run:
            report = ui.local_preview(
                {"limit": 10, "language": "all", "topic": "", "new_window_days": 7, "growth_hours": 24},
                Path(directory),
            )
        run.assert_not_called()
        self.assertEqual(report["new_projects"], [])
        self.assertEqual(report["data_sources"][0]["name"], "Local SQLite preview")

    @mock.patch.object(ui.subprocess, "run")
    def test_collect_uses_fixed_read_only_collector_arguments(self, run):
        report = {"generated_at": "now", "new_projects": [], "fast_growth": []}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        with tempfile.TemporaryDirectory() as directory:
            result = ui.collect(
                {"limit": 10, "language": "Rust", "topic": "ai-agent", "new_window_days": 7, "growth_hours": 24},
                Path(directory),
            )
        self.assertEqual(result["generated_at"], report["generated_at"])
        self.assertFalse(result["_cache"]["readme_enriched"])
        self.assertTrue(result["_cache"]["fast_preview"])
        command = run.call_args.args[0]
        self.assertIn("--format", command)
        self.assertIn("json", command)
        self.assertIn("--topic", command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
