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


if __name__ == "__main__":
    unittest.main()
