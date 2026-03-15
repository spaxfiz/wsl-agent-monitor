from __future__ import annotations

import importlib
import tkinter as tk
import unittest
from unittest.mock import patch

from wsl_agent_monitor.ui import FloatingMonitor


class FloatingMonitorUiTests(unittest.TestCase):
    def setUp(self) -> None:
        setup_tray = patch.object(FloatingMonitor, "_setup_tray", autospec=True, return_value=None)
        self.addCleanup(setup_tray.stop)
        setup_tray.start()

        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        self.monitor = FloatingMonitor(self.root)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        self.monitor.is_closing = True

    def test_detail_toggle_expands_in_place(self) -> None:
        session = self.monitor.sessions["Claude"]
        session.detail_full_var.set("abc123  example")

        self.assertEqual("", session.detail_full_label.winfo_manager())

        session.detail_toggle_button.invoke()
        self.root.update_idletasks()

        self.assertTrue(session.detail_expanded)
        self.assertEqual("Hide", session.detail_toggle_button.cget("text"))
        self.assertEqual("pack", session.detail_full_label.winfo_manager())
        children = [str(widget) for widget in session.detail_full_label.master.winfo_children()]
        self.assertLess(children.index(str(session.detail_full_label)), children.index(str(session.detail_insert_before)))

    def test_source_toggle_expands_before_output(self) -> None:
        session = self.monitor.sessions["Codex"]
        session.source_full_var.set("/tmp/rollout-1.jsonl")

        self.assertEqual("", session.source_full_label.winfo_manager())

        session.source_toggle_button.invoke()
        self.root.update_idletasks()

        self.assertTrue(session.source_expanded)
        self.assertEqual("Hide", session.source_toggle_button.cget("text"))
        self.assertEqual("pack", session.source_full_label.winfo_manager())
        children = [str(widget) for widget in session.source_full_label.master.winfo_children()]
        self.assertLess(children.index(str(session.source_full_label)), children.index(str(session.source_insert_before)))

    def test_thin_entrypoint_exports_main(self) -> None:
        module = importlib.import_module("app")
        self.assertTrue(callable(module.main))


if __name__ == "__main__":
    unittest.main()
