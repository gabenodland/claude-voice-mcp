#!/usr/bin/env python3
"""
Claude Voice Player UI — persistent background process.

Shows a dark-themed tkinter popup with:
- Current message text + agent label
- Pause / Replay / Mute controls
- Message history (clickable to replay)
- TCP socket server receiving commands from voice_mcp.py

Audio playback uses Windows MCI (zero dependencies).
TTS generation uses edge_tts.

TCP Protocol:
  {"cmd": "speak", "text": "...", "agent": "project/task/role", "model": "opus", "agent_display": "project/task/role (opus)"}
  {"cmd": "status"}          → returns JSON {state, agent, text}
"""

import asyncio
import ctypes
import json
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image

from voice_common import VOICE_SERVER_PORT, DEFAULT_RATE, LOG_FILE, validate_rate, send_command, kill_port_holder

# ── Constants ────────────────────────────────────────────────────────────────

HISTORY_DIR = Path(tempfile.gettempdir()) / "claude_voice_history"
MAX_HISTORY = 25
MAX_HISTORY_AGE_SECS = 86400  # Clean up orphaned MP3s older than 24 hours

# Dark theme (Catppuccin Mocha)
C = {
    "bg": "#1e1e2e",
    "surface": "#313244",
    "overlay": "#45475a",
    "text": "#cdd6f4",
    "subtext": "#a6adc8",
    "accent": "#89b4fa",
    "green": "#a6e3a1",
    "yellow": "#f9e2af",
    "red": "#f38ba8",
    "border": "#585b70",
    "muted_bg": "#4a2020",
    "muted_fg": "#ff6b6b",
}


# Curated voice pool — 42 English voices across 14 locales
VOICE_POOL = [
    # ── United States ──
    {"name": "en-US-AriaNeural", "gender": "Female", "locale": "en-US", "label": "Aria (US)"},
    {"name": "en-US-GuyNeural", "gender": "Male", "locale": "en-US", "label": "Guy (US)"},
    {"name": "en-US-JennyNeural", "gender": "Female", "locale": "en-US", "label": "Jenny (US)"},
    {"name": "en-US-ChristopherNeural", "gender": "Male", "locale": "en-US", "label": "Christopher (US)"},
    {"name": "en-US-EricNeural", "gender": "Male", "locale": "en-US", "label": "Eric (US)"},
    {"name": "en-US-MichelleNeural", "gender": "Female", "locale": "en-US", "label": "Michelle (US)"},
    {"name": "en-US-AnaNeural", "gender": "Female", "locale": "en-US", "label": "Ana (US)"},
    {"name": "en-US-AndrewNeural", "gender": "Male", "locale": "en-US", "label": "Andrew (US)"},
    {"name": "en-US-AvaNeural", "gender": "Female", "locale": "en-US", "label": "Ava (US)"},
    {"name": "en-US-BrianNeural", "gender": "Male", "locale": "en-US", "label": "Brian (US)"},
    {"name": "en-US-EmmaNeural", "gender": "Female", "locale": "en-US", "label": "Emma (US)"},
    {"name": "en-US-RogerNeural", "gender": "Male", "locale": "en-US", "label": "Roger (US)"},
    {"name": "en-US-SteffanNeural", "gender": "Male", "locale": "en-US", "label": "Steffan (US)"},
    # ── United Kingdom ──
    {"name": "en-GB-SoniaNeural", "gender": "Female", "locale": "en-GB", "label": "Sonia (UK)"},
    {"name": "en-GB-RyanNeural", "gender": "Male", "locale": "en-GB", "label": "Ryan (UK)"},
    {"name": "en-GB-LibbyNeural", "gender": "Female", "locale": "en-GB", "label": "Libby (UK)"},
    {"name": "en-GB-MaisieNeural", "gender": "Female", "locale": "en-GB", "label": "Maisie (UK)"},
    {"name": "en-GB-ThomasNeural", "gender": "Male", "locale": "en-GB", "label": "Thomas (UK)"},
    # ── Australia ──
    {"name": "en-AU-NatashaNeural", "gender": "Female", "locale": "en-AU", "label": "Natasha (AU)"},
    {"name": "en-AU-WilliamNeural", "gender": "Male", "locale": "en-AU", "label": "William (AU)"},
    # ── Canada ──
    {"name": "en-CA-ClaraNeural", "gender": "Female", "locale": "en-CA", "label": "Clara (CA)"},
    {"name": "en-CA-LiamNeural", "gender": "Male", "locale": "en-CA", "label": "Liam (CA)"},
    # ── Ireland ──
    {"name": "en-IE-EmilyNeural", "gender": "Female", "locale": "en-IE", "label": "Emily (IE)"},
    {"name": "en-IE-ConnorNeural", "gender": "Male", "locale": "en-IE", "label": "Connor (IE)"},
    # ── India ──
    {"name": "en-IN-NeerjaNeural", "gender": "Female", "locale": "en-IN", "label": "Neerja (IN)"},
    {"name": "en-IN-PrabhatNeural", "gender": "Male", "locale": "en-IN", "label": "Prabhat (IN)"},
    # ── New Zealand ──
    {"name": "en-NZ-MollyNeural", "gender": "Female", "locale": "en-NZ", "label": "Molly (NZ)"},
    {"name": "en-NZ-MitchellNeural", "gender": "Male", "locale": "en-NZ", "label": "Mitchell (NZ)"},
    # ── Hong Kong ──
    {"name": "en-HK-YanNeural", "gender": "Female", "locale": "en-HK", "label": "Yan (HK)"},
    {"name": "en-HK-SamNeural", "gender": "Male", "locale": "en-HK", "label": "Sam (HK)"},
    # ── Philippines ──
    {"name": "en-PH-RosaNeural", "gender": "Female", "locale": "en-PH", "label": "Rosa (PH)"},
    {"name": "en-PH-JamesNeural", "gender": "Male", "locale": "en-PH", "label": "James (PH)"},
    # ── Singapore ──
    {"name": "en-SG-LunaNeural", "gender": "Female", "locale": "en-SG", "label": "Luna (SG)"},
    {"name": "en-SG-WayneNeural", "gender": "Male", "locale": "en-SG", "label": "Wayne (SG)"},
    # ── Kenya ──
    {"name": "en-KE-AsiliaNeural", "gender": "Female", "locale": "en-KE", "label": "Asilia (KE)"},
    {"name": "en-KE-ChilembaNeural", "gender": "Male", "locale": "en-KE", "label": "Chilemba (KE)"},
    # ── Nigeria ──
    {"name": "en-NG-EzinneNeural", "gender": "Female", "locale": "en-NG", "label": "Ezinne (NG)"},
    {"name": "en-NG-AbeoNeural", "gender": "Male", "locale": "en-NG", "label": "Abeo (NG)"},
    # ── Tanzania ──
    {"name": "en-TZ-ImaniNeural", "gender": "Female", "locale": "en-TZ", "label": "Imani (TZ)"},
    {"name": "en-TZ-ElimuNeural", "gender": "Male", "locale": "en-TZ", "label": "Elimu (TZ)"},
    # ── South Africa ──
    {"name": "en-ZA-LeahNeural", "gender": "Female", "locale": "en-ZA", "label": "Leah (ZA)"},
    {"name": "en-ZA-LukeNeural", "gender": "Male", "locale": "en-ZA", "label": "Luke (ZA)"},
]


# ── Voice Registry ───────────────────────────────────────────────────────────


class VoiceRegistry:
    """Persistent voice assignment. Each agent gets a unique random voice, saved to disk."""

    _SAVE_FILE = HISTORY_DIR / "voice_assignments.json"

    def __init__(self):
        self._assignments = {}  # agent_id → {"voice": str, "label": str}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load saved assignments from disk."""
        try:
            if self._SAVE_FILE.exists():
                data = json.loads(self._SAVE_FILE.read_text("utf-8"))
                # Validate entries still reference pool voices
                pool_names = {v["name"] for v in VOICE_POOL}
                for agent, info in data.items():
                    if info.get("voice") in pool_names:
                        self._assignments[agent] = info
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self):
        """Persist assignments to disk."""
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            self._SAVE_FILE.write_text(json.dumps(self._assignments, indent=2), "utf-8")
        except OSError:
            pass

    def get_voice(self, agent_id: str) -> tuple[str, str]:
        """Get or auto-assign a voice for this agent. Returns (voice_name, label)."""
        with self._lock:
            if agent_id in self._assignments:
                a = self._assignments[agent_id]
                return a["voice"], a["label"]
            # Pick a random unused voice
            used = {a["voice"] for a in self._assignments.values()}
            available = [v for v in VOICE_POOL if v["name"] not in used]
            if not available:
                available = list(VOICE_POOL)  # all used, allow reuse
            pick = random.choice(available)
            self._assignments[agent_id] = {"voice": pick["name"], "label": pick["label"]}
            self._save()
            return pick["name"], pick["label"]


# ── Audio Player (Windows MCI) ──────────────────────────────────────────────


class MCIPlayer:
    """Audio player using Windows MCI — zero external dependencies."""

    def __init__(self):
        self._mci = ctypes.windll.winmm.mciSendStringW
        self._alias = "claude_voice"
        self.current_file = None
        self._open = False
        self._duration_ms = 0

    def _send(self, cmd):
        buf = ctypes.create_unicode_buffer(256)
        err = self._mci(cmd, buf, 255, 0)
        return err, buf.value

    def play(self, filepath):
        self.stop()
        self.current_file = str(filepath).replace("/", "\\")
        self._send(f'open "{self.current_file}" type mpegvideo alias {self._alias}')
        self._send(f"play {self._alias}")
        self._open = True
        # Query duration so we can detect premature "stopped" reports
        _, length_str = self._send(f"status {self._alias} length")
        try:
            self._duration_ms = int(length_str)
        except (ValueError, TypeError):
            self._duration_ms = 0

    def pause(self):
        if self._open:
            self._send(f"pause {self._alias}")

    def resume(self):
        if self._open:
            self._send(f"resume {self._alias}")

    def stop(self):
        self._send(f"stop {self._alias}")
        self._send(f"close {self._alias}")
        self._open = False

    def replay(self):
        if self.current_file and os.path.exists(self.current_file):
            self.play(self.current_file)

    @property
    def state(self):
        if not self._open:
            return "stopped"
        _, mode = self._send(f"status {self._alias} mode")
        if mode == "playing":
            return "playing"
        if mode == "paused":
            return "paused"
        return "stopped"

    @property
    def duration_ms(self):
        return self._duration_ms


# ── Voice Player UI ─────────────────────────────────────────────────────────


class VoicePlayerUI:
    def __init__(self):
        # Set AppUserModelID BEFORE creating the window so taskbar uses our icon
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("claude.voice.mcp")
        except (AttributeError, OSError):
            pass

        self.root = tk.Tk()
        self.root.title("Claude Voice")
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", True)
        self._set_icon()

        # Position bottom-right
        w, h = 430, 520
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 80}")
        self.root.minsize(340, 380)

        self.msg_queue = queue.Queue()   # raw messages from TCP
        self._play_queue = queue.Queue() # items with TTS generated, in order, ready to play
        self._muted = False
        self._shutting_down = False      # set True on quit to unblock worker
        self._msg_seq = 0               # sequence counter for ordering
        self._seq_lock = threading.Lock()
        self._reorder_buf = {}           # seq -> item, for resequencing TTS results
        self._next_play_seq = 0          # next sequence number to send to play queue
        self._reorder_lock = threading.Lock()
        self.history = []
        self.player = MCIPlayer()
        self.registry = VoiceRegistry()
        self.current_item = None

        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_audio()

        self._build_ui()
        self._bind_keys()
        self._setup_tray()
        self._start_server()
        self._start_tts_dispatcher()
        self._start_playback_worker()
        self._poll_state()
        self._poll_history_age()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_icon(self):
        """Set window icon from bundled pac-man .ico file."""
        ico_path = Path(__file__).parent / "pacman.ico"
        if ico_path.exists():
            self.root.iconbitmap(str(ico_path))

    def _setup_tray(self):
        """Create a system-tray icon with right-click menu."""
        ico_path = Path(__file__).parent / "pacman.ico"
        if ico_path.exists():
            icon_image = Image.open(str(ico_path))
        else:
            # Fallback: tiny colored square
            icon_image = Image.new("RGB", (64, 64), C["accent"])

        menu = pystray.Menu(
            pystray.MenuItem("Show Window", self._tray_show, default=True),
            pystray.MenuItem(
                lambda item: "\u2714 Muted" if self._muted else "Mute",
                self._tray_mute,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon("claude_voice", icon_image, "Claude Voice", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_show(self):
        """Show and raise the main window."""
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_mute(self):
        self.root.after(0, self._on_mute)

    def _tray_quit(self):
        """Actually exit the process."""
        self.root.after(0, self._real_quit)

    def _real_quit(self):
        self._shutting_down = True
        self.player.stop()
        if hasattr(self, "_tray"):
            self._tray.stop()
        self.root.destroy()

    @staticmethod
    def _cleanup_old_audio():
        """Remove orphaned MP3 files older than 24 hours from the history directory."""
        try:
            cutoff = time.time() - MAX_HISTORY_AGE_SECS
            for f in HISTORY_DIR.glob("voice_*.mp3"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        # === Current Message ===
        msg_frame = tk.Frame(self.root, bg=C["surface"])
        msg_frame.pack(fill="x", padx=8, pady=(8, 4))

        header = tk.Frame(msg_frame, bg=C["surface"])
        header.pack(fill="x", padx=12, pady=(10, 0))

        self.status_dot = tk.Label(
            header, text="\u25cf", fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 12),
        )
        self.status_dot.pack(side="left")

        self.agent_label = tk.Label(
            header, text="Waiting for messages\u2026", fg=C["subtext"],
            bg=C["surface"], font=("Consolas", 9),
        )
        self.agent_label.pack(side="left", padx=(6, 0))

        self.time_label = tk.Label(
            header, text="", fg=C["subtext"], bg=C["surface"],
            font=("Consolas", 9),
        )
        self.time_label.pack(side="right")

        self.msg_text = tk.Text(
            msg_frame, fg=C["text"], bg=C["surface"],
            font=("Segoe UI", 14), wrap="word", bd=0,
            highlightthickness=0, state="disabled", cursor="arrow",
            height=3, padx=12, pady=6,
        )
        self.msg_text.pack(fill="x", pady=(4, 10))

        # === Controls ===
        ctrl = tk.Frame(self.root, bg=C["bg"])
        ctrl.pack(fill="x", padx=8, pady=2)

        btn_box = tk.Frame(ctrl, bg=C["bg"])
        btn_box.pack()

        bs = dict(
            bg=C["surface"], fg=C["text"], activebackground=C["overlay"],
            activeforeground=C["text"], bd=0, padx=14, pady=5,
            font=("Consolas", 10), cursor="hand2", relief="flat",
        )
        self.pause_btn = tk.Button(btn_box, text="\u23f8 Pause", command=self._on_pause, **bs)
        self.pause_btn.pack(side="left", padx=2)
        self.replay_btn = tk.Button(btn_box, text="\u21ba Replay", command=self._on_replay, **bs)
        self.replay_btn.pack(side="left", padx=2)
        self.mute_btn = tk.Button(btn_box, text="\U0001f50a Sound", command=self._on_mute, **bs)
        self.mute_btn.pack(side="left", padx=2)

        status_row = tk.Frame(ctrl, bg=C["bg"])
        status_row.pack(pady=(4, 0))
        self.status_text = tk.Label(
            status_row, text="Idle", fg=C["subtext"], bg=C["bg"],
            font=("Consolas", 9),
        )
        self.status_text.pack(side="left")
        self.queue_label = tk.Label(
            status_row, text="", fg=C["accent"], bg=C["bg"],
            font=("Consolas", 9),
        )
        self.queue_label.pack(side="left", padx=(8, 0))

        # === History (Treeview table) ===
        hf = tk.Frame(self.root, bg=C["bg"])
        hf.pack(fill="both", expand=True, padx=8, pady=(6, 8))

        # Style the treeview to match dark theme
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Voice.Treeview",
            background=C["surface"], foreground=C["text"], fieldbackground=C["surface"],
            font=("Consolas", 9), rowheight=22, borderwidth=0,
        )
        style.configure("Voice.Treeview.Heading",
            background=C["overlay"], foreground=C["subtext"],
            font=("Consolas", 8, "bold"), borderwidth=0, relief="flat",
        )
        style.map("Voice.Treeview",
            background=[("selected", C["accent"])],
            foreground=[("selected", C["bg"])],
        )
        style.map("Voice.Treeview.Heading",
            background=[("active", C["overlay"])],
        )

        self.hist_tree = ttk.Treeview(
            hf, style="Voice.Treeview", columns=("ago", "agent", "msg"),
            show="headings", selectmode="browse",
        )
        self.hist_tree.heading("ago", text="Time", anchor="w")
        self.hist_tree.heading("agent", text="Agent", anchor="w")
        self.hist_tree.heading("msg", text="Message", anchor="w")
        self.hist_tree.column("ago", width=90, minwidth=60, stretch=False)
        self.hist_tree.column("agent", width=200, minwidth=120, stretch=False)
        self.hist_tree.column("msg", width=120, minwidth=80, stretch=True)

        sb = ttk.Scrollbar(hf, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=sb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.hist_tree.bind("<Double-Button-1>", self._on_hist_click)
        self.hist_tree.bind("<Return>", self._on_hist_click)

        # Configure tags for row highlighting (once, not per refresh)
        self.hist_tree.tag_configure("playing", background=C["accent"], foreground=C["bg"])
        self.hist_tree.tag_configure("queued", foreground=C["subtext"])
        self.hist_tree.tag_configure("done", foreground=C["text"])

        # === Footer with log file link ===
        log_path = Path(__file__).parent / "voice_log.jsonl"
        footer = tk.Frame(self.root, bg=C["bg"])
        footer.pack(fill="x", padx=8, pady=(0, 6))
        log_link = tk.Label(
            footer, text=str(log_path), font=("Consolas", 8),
            fg=C["accent"], bg=C["bg"], cursor="hand2", anchor="w",
        )
        log_link.pack(side="left")
        log_link.bind("<Button-1>", lambda e: self._open_log_file(log_path))
        log_link.bind("<Enter>", lambda e: log_link.configure(fg="#b4d0fb", font=("Consolas", 8, "underline")))
        log_link.bind("<Leave>", lambda e: log_link.configure(fg=C["accent"], font=("Consolas", 8)))

    def _open_log_file(self, path):
        """Open log file in default editor, creating it if it doesn't exist."""
        if not path.exists():
            path.write_text("", encoding="utf-8")
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            # TODO: cross-platform — test on macOS
            subprocess.Popen(["open", str(path)])
        else:
            # TODO: cross-platform — test on Linux
            subprocess.Popen(["xdg-open", str(path)])

    def _bind_keys(self):
        self.root.bind("<space>", lambda e: self._on_pause())
        self.root.bind("r", lambda e: self._on_replay())
        self.root.bind("m", lambda e: self._on_mute())
        self.root.bind("<Escape>", lambda e: self._on_close())
        self.root.bind("<Control-q>", lambda e: self._on_close())

    # ── TCP Server ───────────────────────────────────────────────────────

    def _start_server(self):
        def loop():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Retry bind up to 3 times, waiting for OS to release the port
            for attempt in range(3):
                try:
                    srv.bind(("127.0.0.1", VOICE_SERVER_PORT))
                    break
                except OSError:
                    if attempt < 2:
                        # Don't kill_port_holder() here — the named mutex in
                        # __main__ guarantees single instance. If bind fails,
                        # the OS just hasn't released the port yet.
                        time.sleep(1.5)
                    else:
                        self._log_error(f"Failed to bind port {VOICE_SERVER_PORT} after 3 attempts — exiting")
                        self.root.after(0, self.root.destroy)
                        return

            srv.listen(5)
            srv.settimeout(1.0)
            while True:
                try:
                    conn, _ = srv.accept()
                    threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue

        threading.Thread(target=loop, daemon=True).start()

    @staticmethod
    def _log_error(msg: str):
        """Append a timestamped error line to the error log."""
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass

    def _handle_conn(self, conn):
        try:
            data = b""
            conn.settimeout(10.0)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            msg = json.loads(data.decode("utf-8"))
            response = self._dispatch(msg)
            conn.sendall(response.encode("utf-8"))
        except Exception as e:
            self._log_error(f"TCP handler error: {e}")
            try:
                conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            except Exception:
                pass
        finally:
            conn.close()

    def _dispatch(self, msg: dict) -> str:
        """Route incoming TCP message to the right handler."""
        cmd = msg.get("cmd", "speak")  # default = speak for backward compat

        if cmd == "speak":
            agent = msg.get("agent", "unknown")
            model = msg.get("model", "unknown")
            agent_display = msg.get("agent_display", agent)
            registry_key = f"{agent}/{model}"
            voice_override = msg.get("voice")
            if voice_override:
                voice, label = voice_override, voice_override
            else:
                voice, label = self.registry.get_voice(registry_key)
            msg["voice"] = voice
            msg["agent_display"] = agent_display
            with self._seq_lock:
                msg["_seq"] = self._msg_seq
                self._msg_seq += 1
            self.msg_queue.put(msg)
            return json.dumps({"ok": True, "voice": voice, "label": label})

        if cmd == "status":
            state = self.player.state
            return json.dumps({
                "ok": True,
                "state": state,
                "agent": self.current_item.get("agent") if self.current_item else None,
                "text": self.current_item.get("text") if self.current_item else None,
            })

        return json.dumps({"ok": False, "error": f"Unknown command: {cmd}"})

    # ── Polling ──────────────────────────────────────────────────────────

    def _start_tts_dispatcher(self):
        """Dispatch incoming messages to TTS generation threads immediately."""
        def dispatcher():
            while True:
                msg = self.msg_queue.get()
                # Spawn a thread for each message — TTS generates in parallel
                threading.Thread(target=self._generate_and_enqueue, args=(msg,), daemon=True).start()

        threading.Thread(target=dispatcher, daemon=True).start()

    def _start_playback_worker(self):
        """Play items from the play queue one at a time, sequentially."""
        def worker():
            while not self._shutting_down:
                try:
                    item = self._play_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                done_event = threading.Event()
                self.root.after(0, lambda i=item, e=done_event: self._play_item(i, e))
                done_event.wait(timeout=60.0)  # safety cap: don't block forever

        threading.Thread(target=worker, daemon=True).start()

    def _poll_state(self):
        if self._muted:
            self.status_dot.configure(fg=C["yellow"])
            self.status_text.configure(text="Muted")
            self.pause_btn.configure(text="\u23f8 Pause", state="disabled")
            self.replay_btn.configure(state="disabled")
        else:
            state = self.player.state
            if state == "playing":
                self.status_dot.configure(fg=C["green"])
                self.status_text.configure(text="Playing\u2026")
                self.pause_btn.configure(text="\u23f8 Pause", state="normal")
                self.replay_btn.configure(state="normal")
            elif state == "paused":
                self.status_dot.configure(fg=C["yellow"])
                self.status_text.configure(text="Paused")
                self.pause_btn.configure(text="\u25b6 Resume", state="normal")
                self.replay_btn.configure(state="normal")
            else:
                self.status_dot.configure(fg=C["subtext"])
                self.status_text.configure(text="Finished" if self.current_item else "Idle")
                self.pause_btn.configure(text="\u23f8 Pause", state="disabled")
                self.replay_btn.configure(state="normal" if self.current_item else "disabled")
        self.root.after(250, self._poll_state)

    def _poll_history_age(self):
        """Re-render history list every 60s to update relative timestamps."""
        if self.history:
            self._refresh_history()
        self.root.after(60_000, self._poll_history_age)

    # ── Message Handling ─────────────────────────────────────────────────

    def _generate_and_enqueue(self, msg):
        """Generate TTS for a message and put the ready item on the play queue in order."""
        text = msg.get("text", "")
        agent = msg.get("agent", "unknown")
        agent_display = msg.get("agent_display", agent)
        voice = msg.get("voice", "")
        rate = msg.get("rate", DEFAULT_RATE)
        seq = msg.get("_seq", 0)
        now = datetime.now()
        ts = now.strftime("%H:%M:%S")

        self.root.after(0, self._on_new_message)

        try:
            path = self._generate_tts(text, voice, rate)
        except Exception as e:
            self.root.after(0, lambda err=str(e): self._show_error(err))
            # Still need to advance the sequence so we don't block the queue
            with self._reorder_lock:
                self._next_play_seq += 1
                self._flush_reorder_buf()
            return

        item = dict(text=text, agent=agent, agent_display=agent_display,
                    voice=voice, rate=rate, audio_path=str(path), timestamp=ts,
                    created=now, _seq=seq, status="queued")

        # Add to history immediately so it appears in the list
        self.root.after(0, lambda i=item: self._add_to_history_queued(i))

        # Put into reorder buffer and flush in-order items to play queue
        with self._reorder_lock:
            self._reorder_buf[seq] = item
            self._flush_reorder_buf()

    def _flush_reorder_buf(self):
        """Move consecutive ready items from reorder buffer to play queue. Must hold _reorder_lock."""
        while self._next_play_seq in self._reorder_buf:
            item = self._reorder_buf.pop(self._next_play_seq)
            self._play_queue.put(item)
            self._next_play_seq += 1
        self.root.after(0, self._update_queue_count)

    def _on_new_message(self):
        """Bring window to front when a new message arrives."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _update_queue_count(self):
        """Update the queue count display."""
        count = self._play_queue.qsize()
        if count > 0:
            self.queue_label.configure(text=f"{count} queued")
        else:
            self.queue_label.configure(text="")

    def _play_item(self, item, done_event):
        """Start playback and poll for completion (must be called on Tk thread)."""
        # Mark as playing in history
        item["status"] = "playing" if not self._muted else "done"
        self.current_item = item
        self._set_message(item["text"])
        agent_display = item.get("agent_display", item.get("agent", "?"))
        self.agent_label.configure(text=agent_display)
        self.time_label.configure(text=item["timestamp"])
        self._refresh_history()
        self._update_queue_count()

        if self._muted:
            done_event.set()
            return

        audio_path = item["audio_path"]
        if not os.path.exists(audio_path):
            item["status"] = "done"
            self._refresh_history()
            done_event.set()
            return

        self.player.play(audio_path)
        play_start = time.time()
        duration_s = self.player.duration_ms / 1000.0 if self.player.duration_ms else 0

        def wait_for_finish():
            if self._shutting_down or self._muted:
                item["status"] = "done"
                self._refresh_history()
                done_event.set()
                return
            state = self.player.state
            elapsed = time.time() - play_start
            min_wait = max(duration_s * 0.8, 2.0) if duration_s > 0 else 2.0
            if state == "playing" or state == "paused" or elapsed < min_wait:
                self.root.after(200, wait_for_finish)
            else:
                item["status"] = "done"
                self._refresh_history()
                self._update_queue_count()
                done_event.set()

        self.root.after(200, wait_for_finish)

    def _add_to_history_queued(self, item):
        """Add item to history in arrival order (by sequence number). Oldest first, newest last."""
        self.history.append(item)
        self.history.sort(key=lambda h: h.get("_seq", 0))
        while len(self.history) > MAX_HISTORY:
            old = self.history.pop(0)
            try:
                os.unlink(old["audio_path"])
            except OSError:
                pass
        self._refresh_history()

    def _generate_tts(self, text, voice, rate=DEFAULT_RATE):
        import edge_tts

        # Strip single-character backslash escapes (e.g. \! from shell escaping)
        clean_text = re.sub(r'\\(.)', r'\1', text)

        # Validate rate at point of consumption (defense in depth)
        rate = validate_rate(rate)

        path = HISTORY_DIR / f"voice_{uuid.uuid4().hex[:12]}.mp3"
        loop = asyncio.new_event_loop()
        try:
            comm = edge_tts.Communicate(clean_text, voice, rate=rate)
            loop.run_until_complete(comm.save(str(path)))
        finally:
            loop.close()
        return path

    def _set_message(self, text):
        """Update message display with auto-sizing height."""
        display = f"\u201c{text}\u201d"
        self.msg_text.configure(state="normal")
        self.msg_text.delete("1.0", "end")
        self.msg_text.insert("1.0", display)
        self.msg_text.configure(state="disabled")
        # Auto-size: count display lines (approx 45 chars per line at this font/width)
        lines = max(2, min(8, sum(1 + len(line) // 45 for line in display.split("\n"))))
        self.msg_text.configure(height=lines)

    def _show_error(self, msg):
        self.status_text.configure(text=f"Error: {msg}")
        self.status_dot.configure(fg=C["red"])

    @staticmethod
    def _time_ago(created):
        """Format a datetime as a relative time string."""
        delta = (datetime.now() - created).total_seconds()
        if delta < 60:
            return "just now"
        if delta < 3600:
            m = int(delta // 60)
            return f"{m}m ago"
        if delta < 86400:
            h = int(delta // 3600)
            return f"{h}h ago"
        d = int(delta // 86400)
        return f"{d}d ago"

    def _refresh_history(self):
        for row in self.hist_tree.get_children():
            self.hist_tree.delete(row)
        for i, item in enumerate(self.history):
            ago = self._time_ago(item["created"]) if "created" in item else ""
            clock = item.get("timestamp", "")
            time_col = f"{clock} ({ago})" if ago else clock
            agent_display = item.get("agent_display", item.get("agent", "?"))
            status = item.get("status", "done")
            if status == "playing":
                prefix = "\u25b6 "
            elif status == "queued":
                prefix = "\u23f3 "
            else:
                prefix = ""
            self.hist_tree.insert("", "end", iid=str(i),
                values=(time_col, agent_display, f"{prefix}{item['text']}"),
                tags=(status,))
        # Auto-scroll to bottom so newest items are visible
        children = self.hist_tree.get_children()
        if children:
            self.hist_tree.see(children[-1])

    # ── Button Handlers ──────────────────────────────────────────────────

    def _on_pause(self):
        state = self.player.state
        if state == "playing":
            self.player.pause()
            self.pause_btn.configure(text="\u25b6 Resume")
        elif state == "paused":
            self.player.resume()
            self.pause_btn.configure(text="\u23f8 Pause")

    def _on_replay(self):
        if self._muted:
            return
        if self.current_item:
            path = self.current_item["audio_path"]
            if os.path.exists(path):
                self.player.play(path)

    def _on_mute(self):
        self._muted = not self._muted
        if self._muted:
            self.mute_btn.configure(text="\U0001f507 Muted", bg=C["muted_bg"], fg=C["muted_fg"])
            self.player.stop()
        else:
            self.mute_btn.configure(text="\U0001f50a Sound", bg=C["surface"], fg=C["text"])

    def _on_hist_click(self, _event=None):
        sel = self.hist_tree.selection()
        if not sel:
            return
        try:
            item = self.history[int(sel[0])]
        except (IndexError, ValueError):
            return
        if item.get("status") in ("queued", "playing"):
            return  # can't replay items still in the queue
        self.current_item = item
        self._set_message(item["text"])
        agent_display = item.get("agent_display", item.get("agent", "?"))
        self.agent_label.configure(text=agent_display)
        self.time_label.configure(text=item["timestamp"])
        if not self._muted:
            path = item["audio_path"]
            if os.path.exists(path):
                self.player.play(path)

    def _on_close(self):
        self.player.stop()
        self.root.withdraw()

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # ── Single-instance guard using a Windows named mutex ──
    # Atomic and race-free. Only one process can hold the mutex.
    # The OS automatically releases it if the process crashes or is killed.
    _mutex = None
    if sys.platform == "win32":
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Claude_Voice_MCP_SingleInstance")
        _last_err = ctypes.windll.kernel32.GetLastError()
        if _last_err == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(_mutex)
            sys.exit(0)

    # If we hold the mutex, kill any zombie holding the port
    # (crashed process that released the mutex but didn't release the port)
    kill_port_holder()

    try:
        VoicePlayerUI().run()
    except Exception as _exc:
        # Log crash so it's diagnosable (stderr may go to DEVNULL when launched detached)
        try:
            import traceback
            with open(LOG_FILE, "a", encoding="utf-8") as _f:
                _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _f.write(f"[{_ts}] CRASH: {_exc}\n")
                traceback.print_exc(file=_f)
        except OSError:
            pass
        raise
    finally:
        if _mutex and sys.platform == "win32":
            ctypes.windll.kernel32.ReleaseMutex(_mutex)
            ctypes.windll.kernel32.CloseHandle(_mutex)
