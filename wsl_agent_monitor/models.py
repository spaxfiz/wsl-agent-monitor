from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import scrolledtext, ttk


@dataclass
class AgentSession:
    name: str
    accent: str
    status_var: tk.StringVar
    summary_var: tk.StringVar
    detail_var: tk.StringVar
    source_var: tk.StringVar
    usage_var: tk.StringVar
    detail_full_var: tk.StringVar
    source_full_var: tk.StringVar
    text_widget: scrolledtext.ScrolledText
    toggle_button: ttk.Button
    detail_toggle_button: ttk.Button
    source_toggle_button: ttk.Button
    usage_label: ttk.Label
    detail_full_label: ttk.Label
    source_full_label: ttk.Label
    detail_insert_before: tk.Widget
    source_insert_before: tk.Widget
    watcher_thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    watching: bool = False
    offsets: dict[str, int] | None = None
    known_sessions: dict[str, str] | None = None
    last_notice: str = ""
    detail_expanded: bool = False
    source_expanded: bool = False
