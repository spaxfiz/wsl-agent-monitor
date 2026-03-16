from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from wsl_agent_monitor.probes import run_probe_once


class ProbeTests(unittest.TestCase):
    def test_missing_wsl_returns_error_payload(self) -> None:
        with patch("wsl_agent_monitor.probes.subprocess.run", side_effect=FileNotFoundError("wsl.exe not found")):
            result = run_probe_once("Claude", "", {})

        self.assertEqual("error", result["status"])
        self.assertEqual("Probe failed", result["summary"])
        self.assertEqual([], result["events"])

    def test_invalid_json_returns_error_payload(self) -> None:
        completed = subprocess.CompletedProcess(args=["wsl.exe"], returncode=0, stdout="not-json", stderr="")
        with patch("wsl_agent_monitor.probes.subprocess.run", return_value=completed):
            result = run_probe_once("Codex", "", {})

        self.assertEqual("error", result["status"])
        self.assertEqual("Probe returned invalid JSON", result["summary"])
        self.assertEqual([], result["sessions"])

    def test_host_probe_uses_local_dispatch(self) -> None:
        local_payload = {
            "status": "watching",
            "summary": "local",
            "detail": "ok",
            "sources": ["C:/Users/test/.codex/sessions/rollout.jsonl"],
            "offsets": {},
            "sessions": [{"id": "abc123", "name": "monitor"}],
            "events": [],
            "usage_text": "Usage tot 51k | 1 sess",
        }

        with patch("wsl_agent_monitor.probes._run_local_probe_once", return_value=local_payload) as local_probe:
            with patch("wsl_agent_monitor.probes.subprocess.run") as subprocess_run:
                result = run_probe_once("Codex", "", {}, probe_target="host")

        local_probe.assert_called_once_with("Codex", {})
        subprocess_run.assert_not_called()
        self.assertEqual("watching", result["status"])
        self.assertEqual("Usage tot 51k | 1 sess", result["usage_text"])

    def test_wsl_probe_defaults_missing_usage_text(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["wsl.exe"],
            returncode=0,
            stdout='{"status":"idle","summary":"ok","detail":"-","sources":[],"offsets":{},"sessions":[],"events":[]}',
            stderr="",
        )
        with patch("wsl_agent_monitor.probes.subprocess.run", return_value=completed):
            result = run_probe_once("Codex", "", {})

        self.assertEqual("idle", result["status"])
        self.assertEqual("Usage unavailable", result["usage_text"])


if __name__ == "__main__":
    unittest.main()
