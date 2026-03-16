from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any

from .constants import (
    ACCENT,
    CLAUDE_ACCENT,
    CODEX_ACCENT,
    DOCK_COLLAPSED_WIDTH,
    DOCK_HOTZONE_WIDTH,
    DOCK_POLL_MS,
    MAX_LINES,
    MUTED_FG,
    PANEL_BG,
    POLL_INTERVAL_MS,
    TEXT_BG,
    TEXT_FG,
    WINDOW_BG,
)
from .models import AgentSession
from .probes import run_probe_once


class FloatingMonitor:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.queue: queue.Queue[tuple[str, str, dict[str, object] | str]] = queue.Queue()
        self.sessions: dict[str, AgentSession] = {}
        self.drag_origin: tuple[int, int] | None = None
        self.tray_icon: Any | None = None
        self.tray_thread: threading.Thread | None = None
        self.tray_supported = False
        self.all_toggle_button: ttk.Button | None = None
        self.hidden_to_tray = False
        self.is_closing = False
        self.dock_enabled = False
        self.dock_expanded = True
        self.dock_width = 1160
        self.dock_height = 760
        self.hover_deadline = 0.0
        self.probe_target = "wsl"
        self.host_toggle_button: ttk.Button | None = None
        self.wsl_toggle_button: ttk.Button | None = None
        self.distro_entry: tk.Entry | None = None

        self.distro_var = tk.StringVar(value="")
        self.distro_text = ""
        self.pin_var = tk.BooleanVar(value=True)
        self.dock_var = tk.BooleanVar(value=False)
        self.opacity_var = tk.DoubleVar(value=0.96)
        self.distro_var.trace_add("write", self._sync_distro_text)

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._register_session("Claude", CLAUDE_ACCENT)
        self._register_session("Codex", CODEX_ACCENT)
        self._apply_topmost()
        self._apply_opacity()
        self._setup_tray()

        self.root.after(80, self._pump_queue)
        self.root.after(DOCK_POLL_MS, self._dock_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _sync_distro_text(self, *_args: object) -> None:
        self.distro_text = self.distro_var.get().strip()

    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            self.tray_supported = False
            return

        self.tray_supported = True
        image = self._create_tray_image(Image, ImageDraw)

        def on_toggle(_icon: Any, _item: Any) -> None:
            self.root.after(0, self._toggle_from_tray)

        def on_quit(_icon: Any, _item: Any) -> None:
            self.root.after(0, self._quit_from_tray)

        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide", on_toggle, default=True),
            pystray.MenuItem("Exit", on_quit),
        )
        self.tray_icon = pystray.Icon("wsl-agent-monitor", image, "Agent Monitor", menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def _create_tray_image(self, Image: Any, ImageDraw: Any) -> Any:
        image = Image.new("RGBA", (64, 64), (8, 17, 31, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=(14, 26, 43, 255), outline=(114, 230, 200, 255), width=3)
        draw.rectangle((17, 18, 47, 24), fill=(127, 179, 255, 255))
        draw.rectangle((17, 29, 38, 35), fill=(246, 178, 107, 255))
        draw.rectangle((17, 40, 52, 46), fill=(114, 230, 200, 255))
        return image

    def _toggle_from_tray(self) -> None:
        if self.hidden_to_tray:
            self._show_window()
        else:
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if not self.tray_supported:
            self.root.overrideredirect(False)
            self.root.iconify()
            self.root.after(180, lambda: self.root.overrideredirect(True))
            return
        self.hidden_to_tray = True
        self.root.withdraw()

    def _show_window(self) -> None:
        self.hidden_to_tray = False
        self.root.deiconify()
        self.root.overrideredirect(True)
        self._apply_topmost()
        self.root.lift()
        self.root.focus_force()

    def _quit_from_tray(self) -> None:
        self.is_closing = True
        self.close()

    def _toggle_dock(self) -> None:
        self.dock_enabled = self.dock_var.get()
        if self.dock_enabled:
            self.root.minsize(DOCK_COLLAPSED_WIDTH, 560)
            self.dock_width = max(self.root.winfo_width(), 920)
            self.dock_height = max(self.root.winfo_height(), 560)
            self.dock_expanded = True
            self._snap_to_right_edge(self.dock_width)
        else:
            self.root.minsize(920, 560)
            self.dock_expanded = True
            self._snap_to_right_edge(max(self.dock_width, 920))

    def _snap_to_right_edge(self, width: int) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        height = max(self.root.winfo_height(), min(self.dock_height, screen_height))
        y = min(max(self.root.winfo_y(), 0), max(screen_height - height, 0))
        x = screen_width - width
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _pointer_inside_window(self) -> bool:
        x1 = self.root.winfo_x()
        y1 = self.root.winfo_y()
        x2 = x1 + self.root.winfo_width()
        y2 = y1 + self.root.winfo_height()
        px = self.root.winfo_pointerx()
        py = self.root.winfo_pointery()
        return x1 <= px <= x2 and y1 <= py <= y2

    def _dock_tick(self) -> None:
        if self.dock_enabled and not self.hidden_to_tray:
            screen_width = self.root.winfo_screenwidth()
            px = self.root.winfo_pointerx()
            py = self.root.winfo_pointery()
            inside = self._pointer_inside_window()
            hotzone = px >= screen_width - DOCK_HOTZONE_WIDTH
            vertical_match = self.root.winfo_y() - 24 <= py <= self.root.winfo_y() + self.root.winfo_height() + 24

            if self.dock_expanded:
                if inside or (hotzone and vertical_match):
                    self.hover_deadline = time.time() + 0.6
                elif time.time() > self.hover_deadline:
                    self.dock_expanded = False
                    self._snap_to_right_edge(DOCK_COLLAPSED_WIDTH)
            else:
                if hotzone and vertical_match:
                    self.dock_expanded = True
                    self.hover_deadline = time.time() + 0.6
                    self._snap_to_right_edge(max(self.dock_width, 920))

        self.root.after(DOCK_POLL_MS, self._dock_tick)

    def _configure_window(self) -> None:
        self.root.title("Agent Monitor")
        self.root.geometry("1160x760+80+80")
        self.root.minsize(920, 560)
        self.root.configure(bg=WINDOW_BG)
        self.root.overrideredirect(True)

    def _build_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Root.TFrame", background=WINDOW_BG)
        style.configure("Card.TFrame", background=PANEL_BG, borderwidth=0)
        style.configure("Title.TLabel", background=WINDOW_BG, foreground=TEXT_FG, font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Hint.TLabel", background=WINDOW_BG, foreground=MUTED_FG, font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background=PANEL_BG, foreground=TEXT_FG, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background=PANEL_BG, foreground=MUTED_FG, font=("Microsoft YaHei UI", 9))
        style.configure("PanelValue.TLabel", background=PANEL_BG, foreground=TEXT_FG, font=("Consolas", 10))
        style.configure("Usage.TLabel", background=TEXT_BG, foreground="#9EB1C7", font=("Microsoft YaHei UI", 8))
        style.configure("Panel.TCheckbutton", background=WINDOW_BG, foreground=TEXT_FG, font=("Microsoft YaHei UI", 9))
        style.map("Panel.TCheckbutton", background=[("active", WINDOW_BG)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#08111F", borderwidth=0, padding=(10, 6), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#9CF2DB")])
        style.configure("Ghost.TButton", background=PANEL_BG, foreground=TEXT_FG, borderwidth=0, padding=(10, 6), font=("Microsoft YaHei UI", 9))
        style.map("Ghost.TButton", background=[("active", "#16304B")])

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, style="Root.TFrame", padding=10)
        container.pack(fill="both", expand=True)

        shell = tk.Frame(container, bg="#102033", highlightthickness=1, highlightbackground="#1D3652")
        shell.pack(fill="both", expand=True)

        titlebar = tk.Frame(shell, bg=WINDOW_BG, height=42)
        titlebar.pack(fill="x")
        titlebar.bind("<ButtonPress-1>", self._start_drag)
        titlebar.bind("<B1-Motion>", self._do_drag)

        title = ttk.Label(titlebar, text="Agent Monitor", style="Title.TLabel")
        title.pack(side="left", padx=(12, 10), pady=8)
        title.bind("<ButtonPress-1>", self._start_drag)
        title.bind("<B1-Motion>", self._do_drag)

        subtitle = ttk.Label(
            titlebar,
            text="Watches live Claude and Codex session files on host or inside WSL. No log files are written.",
            style="Hint.TLabel",
        )
        subtitle.pack(side="left", pady=10)
        subtitle.bind("<ButtonPress-1>", self._start_drag)
        subtitle.bind("<B1-Motion>", self._do_drag)

        close_btn = tk.Button(titlebar, text="X", command=self.close, bg=WINDOW_BG, fg=TEXT_FG, bd=0, font=("Segoe UI", 12, "bold"), activebackground="#1B2E46", activeforeground="white")
        close_btn.pack(side="right", padx=(8, 10), pady=4)
        mini_btn = tk.Button(titlebar, text="_", command=self._minimize, bg=WINDOW_BG, fg=TEXT_FG, bd=0, font=("Segoe UI", 12), activebackground="#1B2E46", activeforeground="white")
        mini_btn.pack(side="right", pady=4)

        controls = tk.Frame(shell, bg=WINDOW_BG)
        controls.pack(fill="x", padx=12, pady=(0, 8))

        distro_label = ttk.Label(controls, text="WSL distro", style="Hint.TLabel")
        distro_label.grid(row=0, column=0, sticky="w", padx=(0, 6))

        self.distro_entry = tk.Entry(
            controls,
            textvariable=self.distro_var,
            bg="#11243B",
            fg=TEXT_FG,
            insertbackground=TEXT_FG,
            relief="flat",
            font=("Consolas", 10),
            width=18,
            disabledbackground="#0C1828",
            disabledforeground=MUTED_FG,
        )
        self.distro_entry.grid(row=1, column=0, sticky="we", padx=(0, 12))

        source_label = ttk.Label(controls, text="Source", style="Hint.TLabel")
        source_label.grid(row=0, column=1, sticky="w", padx=(0, 6))

        source_buttons = tk.Frame(controls, bg=WINDOW_BG)
        source_buttons.grid(row=1, column=1, sticky="w", padx=(0, 12))

        self.host_toggle_button = ttk.Button(source_buttons, text="Host", style="Ghost.TButton", command=lambda: self._set_probe_target("host"))
        self.host_toggle_button.pack(side="left", padx=(0, 6))
        self.wsl_toggle_button = ttk.Button(source_buttons, text="WSL", style="Accent.TButton", command=lambda: self._set_probe_target("wsl"))
        self.wsl_toggle_button.pack(side="left")

        actions = tk.Frame(controls, bg=WINDOW_BG)
        actions.grid(row=1, column=2, sticky="w")

        self.all_toggle_button = ttk.Button(actions, text="Watch all", style="Accent.TButton", command=self.toggle_all)
        self.all_toggle_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Clear view", style="Ghost.TButton", command=self.clear_all).pack(side="left")

        pin_toggle = ttk.Checkbutton(controls, text="Always on top", variable=self.pin_var, style="Panel.TCheckbutton", command=self._apply_topmost)
        pin_toggle.grid(row=0, column=3, sticky="w", padx=(12, 0))

        dock_toggle = ttk.Checkbutton(controls, text="Edge dock", variable=self.dock_var, style="Panel.TCheckbutton", command=self._toggle_dock)
        dock_toggle.grid(row=1, column=5, sticky="w", padx=(12, 0))

        opacity_label = ttk.Label(controls, text="Opacity", style="Hint.TLabel")
        opacity_label.grid(row=0, column=4, sticky="w", padx=(12, 0))

        opacity_scale = tk.Scale(
            controls,
            from_=0.7,
            to=1.0,
            resolution=0.02,
            orient="horizontal",
            variable=self.opacity_var,
            command=lambda _value: self._apply_opacity(),
            bg=WINDOW_BG,
            fg=MUTED_FG,
            troughcolor="#16304B",
            activebackground=ACCENT,
            highlightthickness=0,
            length=160,
        )
        opacity_scale.grid(row=1, column=4, sticky="w", padx=(12, 0))
        controls.grid_columnconfigure(0, weight=1)

        body = tk.Frame(shell, bg="#102033")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.cards_parent = body
        self._sync_probe_target_buttons()

    def _clip_text(self, text: str, limit: int = 120) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _toggle_detail_section(self, agent_name: str, section: str) -> None:
        session = self.sessions[agent_name]
        if section == "sessions":
            session.detail_expanded = not session.detail_expanded
            if session.detail_expanded:
                session.detail_full_label.pack(anchor="w", fill="x", pady=(4, 0), before=session.detail_insert_before)
                session.detail_toggle_button.configure(text="Hide")
            else:
                session.detail_full_label.pack_forget()
                session.detail_toggle_button.configure(text="Show")
            return

        session.source_expanded = not session.source_expanded
        if session.source_expanded:
            session.source_full_label.pack(anchor="w", fill="x", pady=(4, 0), before=session.source_insert_before)
            session.source_toggle_button.configure(text="Hide")
        else:
            session.source_full_label.pack_forget()
            session.source_toggle_button.configure(text="Show")

    def _register_session(self, name: str, accent: str) -> None:
        card = ttk.Frame(self.cards_parent, style="Card.TFrame", padding=12)
        card.pack(side="left", fill="both", expand=True, padx=(0, 8 if not self.sessions else 0))
        if self.sessions:
            card.pack_configure(padx=(8, 0))

        header = tk.Frame(card, bg=PANEL_BG)
        header.pack(fill="x")

        dot = tk.Canvas(header, width=10, height=10, bg=PANEL_BG, highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=accent, outline=accent)
        dot.pack(side="left", pady=(2, 0))

        ttk.Label(header, text=name, style="CardTitle.TLabel").pack(side="left", padx=(8, 10))

        status_var = tk.StringVar(value="Paused")
        ttk.Label(header, textvariable=status_var, style="Body.TLabel").pack(side="left")

        buttons = tk.Frame(header, bg=PANEL_BG)
        buttons.pack(side="right")

        toggle_btn = ttk.Button(buttons, text="Watch", style="Accent.TButton", command=lambda agent=name: self.toggle_agent(agent))
        toggle_btn.pack(side="left")

        summary_var = tk.StringVar(value="Waiting to watch")
        detail_var = tk.StringVar(value="No live sessions")
        source_var = tk.StringVar(value="No watched files")
        usage_var = tk.StringVar(value="Usage unavailable")
        detail_full_var = tk.StringVar(value="")
        source_full_var = tk.StringVar(value="")

        ttk.Label(card, text="Current activity", style="Body.TLabel").pack(anchor="w", pady=(8, 4))
        ttk.Label(card, textvariable=summary_var, style="PanelValue.TLabel", wraplength=480, justify="left").pack(anchor="w", fill="x")

        detail_header = tk.Frame(card, bg=PANEL_BG)
        detail_header.pack(fill="x", pady=(8, 0))
        ttk.Label(detail_header, text="Sessions", style="Body.TLabel").pack(side="left")
        detail_toggle_button = ttk.Button(detail_header, text="Show", style="Ghost.TButton", command=lambda agent=name: self._toggle_detail_section(agent, "sessions"))
        detail_toggle_button.pack(side="right")
        ttk.Label(card, textvariable=detail_var, style="Body.TLabel", wraplength=480, justify="left").pack(anchor="w", fill="x", pady=(2, 0))
        detail_full_label = ttk.Label(card, textvariable=detail_full_var, style="Body.TLabel", wraplength=480, justify="left")

        source_header = tk.Frame(card, bg=PANEL_BG)
        source_header.pack(fill="x", pady=(8, 0))
        ttk.Label(source_header, text="Watched files", style="Body.TLabel").pack(side="left")
        source_toggle_button = ttk.Button(source_header, text="Show", style="Ghost.TButton", command=lambda agent=name: self._toggle_detail_section(agent, "files"))
        source_toggle_button.pack(side="right")
        ttk.Label(card, textvariable=source_var, style="Body.TLabel", wraplength=480, justify="left").pack(anchor="w", fill="x", pady=(2, 0))
        source_full_label = ttk.Label(card, textvariable=source_full_var, style="Body.TLabel", wraplength=480, justify="left")

        output_area = tk.Frame(card, bg=TEXT_BG, highlightthickness=0, bd=0)
        output_area.pack(fill="both", expand=True, pady=(10, 0))

        usage_label = ttk.Label(output_area, textvariable=usage_var, style="Usage.TLabel", wraplength=480, justify="left")
        usage_label.pack(side="bottom", anchor="w", fill="x", padx=10, pady=(0, 8))

        output = scrolledtext.ScrolledText(
            output_area,
            wrap="word",
            bg=TEXT_BG,
            fg=TEXT_FG,
            insertbackground=TEXT_FG,
            relief="flat",
            bd=0,
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        output.pack(side="top", fill="both", expand=True)
        output.configure(state="disabled")

        self.sessions[name] = AgentSession(
            name=name,
            accent=accent,
            status_var=status_var,
            summary_var=summary_var,
            detail_var=detail_var,
            source_var=source_var,
            usage_var=usage_var,
            detail_full_var=detail_full_var,
            source_full_var=source_full_var,
            text_widget=output,
            toggle_button=toggle_btn,
            detail_toggle_button=detail_toggle_button,
            source_toggle_button=source_toggle_button,
            usage_label=usage_label,
            detail_full_label=detail_full_label,
            source_full_label=source_full_label,
            detail_insert_before=source_header,
            source_insert_before=output_area,
            offsets={},
            known_sessions={},
        )

    def _probe_target_text(self) -> str:
        return "WSL" if self.probe_target == "wsl" else "Host"

    def _sync_probe_target_buttons(self) -> None:
        if self.host_toggle_button is not None:
            self.host_toggle_button.configure(style="Accent.TButton" if self.probe_target == "host" else "Ghost.TButton")
        if self.wsl_toggle_button is not None:
            self.wsl_toggle_button.configure(style="Accent.TButton" if self.probe_target == "wsl" else "Ghost.TButton")
        if self.distro_entry is not None:
            self.distro_entry.configure(state="normal" if self.probe_target == "wsl" else "disabled")

    def _set_probe_target(self, probe_target: str) -> None:
        if probe_target == self.probe_target:
            return

        self.probe_target = probe_target
        self._sync_probe_target_buttons()
        target_name = self._probe_target_text()
        for agent_name, session in self.sessions.items():
            session.offsets = {}
            session.known_sessions = {}
            session.last_notice = ""
            session.usage_var.set("Usage unavailable")
            if session.watching:
                session.status_var.set("Switching")
                session.summary_var.set(f"Switching to {target_name} monitoring...")
                self._append_system(agent_name, f"Switched to {target_name} monitoring.\n")

    def _start_drag(self, event: tk.Event[tk.Misc]) -> None:
        self.drag_origin = (event.x_root, event.y_root)

    def _do_drag(self, event: tk.Event[tk.Misc]) -> None:
        if self.drag_origin is None:
            return

        last_x, last_y = self.drag_origin
        dx = event.x_root - last_x
        dy = event.y_root - last_y
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        if self.dock_enabled:
            x = self.root.winfo_screenwidth() - self.root.winfo_width()
        self.root.geometry(f"+{x}+{y}")
        self.drag_origin = (event.x_root, event.y_root)

    def _minimize(self) -> None:
        self._hide_to_tray()

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", self.pin_var.get())

    def _apply_opacity(self) -> None:
        self.root.attributes("-alpha", self.opacity_var.get())

    def start_all(self) -> None:
        for name in self.sessions:
            self.start_agent(name)

    def stop_all(self) -> None:
        for name in self.sessions:
            self.stop_agent(name)

    def clear_all(self) -> None:
        for session in self.sessions.values():
            self._set_text(session.text_widget, "")

    def toggle_all(self) -> None:
        if any(session.watching for session in self.sessions.values()):
            self.stop_all()
        else:
            self.start_all()

    def toggle_agent(self, agent_name: str) -> None:
        session = self.sessions[agent_name]
        if session.watching:
            self.stop_agent(agent_name)
        else:
            self.start_agent(agent_name)

    def _sync_toggle_button(self, agent_name: str) -> None:
        session = self.sessions[agent_name]
        session.toggle_button.configure(text="Pause" if session.watching else "Watch")
        self._sync_all_toggle_button()

    def _sync_all_toggle_button(self) -> None:
        if self.all_toggle_button is None:
            return
        any_watching = any(session.watching for session in self.sessions.values())
        self.all_toggle_button.configure(text="Pause all" if any_watching else "Watch all")

    def start_agent(self, agent_name: str) -> None:
        session = self.sessions[agent_name]
        if session.watching:
            return

        session.stop_event = threading.Event()
        session.watching = True
        session.offsets = {}
        session.known_sessions = {}
        session.status_var.set("Connecting")
        self._sync_toggle_button(agent_name)
        self._append_system(agent_name, f"Watching live {self._probe_target_text()} session files.\n")

        watcher = threading.Thread(target=self._watch_agent, args=(agent_name,), daemon=True)
        session.watcher_thread = watcher
        watcher.start()

    def stop_agent(self, agent_name: str) -> None:
        session = self.sessions[agent_name]
        if not session.watching:
            session.status_var.set("Paused")
            self._sync_toggle_button(agent_name)
            return

        session.watching = False
        if session.stop_event is not None:
            session.stop_event.set()
        session.status_var.set("Paused")
        session.known_sessions = {}
        self._sync_toggle_button(agent_name)
        self._append_system(agent_name, "Monitoring paused.\n")

    def _watch_agent(self, agent_name: str) -> None:
        session = self.sessions[agent_name]
        assert session.stop_event is not None
        offsets_state = dict(session.offsets or {})

        while not session.stop_event.is_set():
            try:
                result = run_probe_once(agent_name, self.distro_text, offsets_state, self.probe_target)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "status": "error",
                    "summary": "Watcher crashed",
                    "detail": str(exc),
                    "sources": [],
                    "offsets": offsets_state,
                    "sessions": [],
                    "events": [],
                    "usage_text": "Usage unavailable",
                }

            offsets_state = dict(result.get("offsets") or offsets_state)
            self.queue.put(("probe", agent_name, result))

            if session.stop_event.wait(POLL_INTERVAL_MS / 1000):
                break

        self.queue.put(("paused", agent_name, {"summary": "Monitoring paused"}))

    def _pump_queue(self) -> None:
        while True:
            try:
                event, agent_name, payload = self.queue.get_nowait()
            except queue.Empty:
                break

            if event == "probe":
                self._apply_probe(agent_name, payload if isinstance(payload, dict) else {})
            elif event == "paused":
                self.sessions[agent_name].status_var.set("Paused")
                self._sync_toggle_button(agent_name)

        self.root.after(80, self._pump_queue)

    def _apply_probe(self, agent_name: str, result: dict[str, object]) -> None:
        session = self.sessions[agent_name]

        status = str(result.get("status") or "idle")
        summary = str(result.get("summary") or "Waiting for data")
        detail = str(result.get("detail") or "-")
        events = [str(item) for item in (result.get("events") or []) if str(item).strip()]
        sessions = [
            {"id": str(item.get("id") or "?"), "name": str(item.get("name") or "-")}
            for item in (result.get("sessions") or [])
            if isinstance(item, dict)
        ]
        sources = [str(item) for item in (result.get("sources") or []) if str(item).strip()]
        usage_text = str(result.get("usage_text") or "Usage unavailable")
        session.offsets = dict(result.get("offsets") or {})

        if status == "watching":
            session.status_var.set("Watching")
        elif status == "stale":
            session.status_var.set("Stale")
        elif status == "idle":
            session.status_var.set("Idle")
        else:
            session.status_var.set("Error")
        self._sync_toggle_button(agent_name)

        session.summary_var.set(summary)
        if sessions:
            detail_preview = " | ".join(self._clip_text(f"{item['id']} {item['name']}", 48) for item in sessions[:3])
            if len(sessions) > 3:
                detail_preview += f" | +{len(sessions) - 3} more"
            detail_full = "\n".join(f"{item['id']}  {item['name']}" for item in sessions)
        else:
            detail_preview = detail
            detail_full = detail

        if sources:
            source_preview = f"{len(sources)} file(s) hidden"
            source_full = "\n".join(sources)
        else:
            source_preview = "No watched files"
            source_full = "-"

        session.detail_var.set(detail_preview)
        session.detail_full_var.set(detail_full)
        session.source_var.set(source_preview)
        session.source_full_var.set(source_full)
        session.usage_var.set(usage_text)

        previous_sessions = session.known_sessions or {}
        current_sessions = {item["id"]: item["name"] for item in sessions}
        for session_id, name in current_sessions.items():
            if previous_sessions.get(session_id) != name:
                self._append_system(agent_name, f"Tracking {session_id} {name}\n")
        for session_id in previous_sessions:
            if session_id not in current_sessions:
                self._append_system(agent_name, f"Session {session_id} is no longer active.\n")
        session.known_sessions = current_sessions

        if status == "error":
            notice = f"error:{detail}"
            if notice != session.last_notice:
                self._append_system(agent_name, detail + "\n", is_error=True)
                session.last_notice = notice
        elif status == "idle":
            notice = f"idle:{summary}"
            if notice != session.last_notice:
                self._append_system(agent_name, summary + "\n")
                session.last_notice = notice
        else:
            session.last_notice = ""

        for line in events:
            self._append_text(session.text_widget, line + "\n")

    def _append_system(self, agent_name: str, text: str, is_error: bool = False) -> None:
        prefix = f"[{agent_name}] "
        if is_error:
            prefix = f"[{agent_name} / error] "
        self._append_text(self.sessions[agent_name].text_widget, prefix + text)

    def _append_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", text)
        self._trim_text(widget)
        widget.see("end")
        widget.configure(state="disabled")

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _trim_text(self, widget: scrolledtext.ScrolledText) -> None:
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count <= MAX_LINES:
            return

        extra = line_count - MAX_LINES
        widget.delete("1.0", f"{extra + 1}.0")

    def close(self) -> None:
        self.stop_all()
        if not self.is_closing:
            self._hide_to_tray()
            return

        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(120, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    FloatingMonitor(root)
    root.mainloop()
