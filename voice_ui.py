#!/usr/bin/env python3
"""
Claude Voice Player UI — persistent background process.

Shows a dark-themed tkinter popup with:
- Current message text + agent label
- Play / Pause / Replay controls
- Message history (clickable to replay)
- TCP socket server receiving commands from voice.py

Audio playback uses Windows MCI (zero dependencies).
TTS generation uses edge_tts.

TCP Protocol:
  {"cmd": "speak", "text": "...", "agent": "opus-main", "name": "Main"}
  {"cmd": "assignments"}     → returns JSON {agent: {voice, label}}
  {"cmd": "assign", "agent": "opus-main", "voice": "en-GB-SoniaNeural"}
  {"cmd": "pool"}            → returns JSON [{name, gender, locale, label, assigned_to}]
  {"cmd": "stop"}            → stops current playback
  {"cmd": "status"}          → returns JSON {state, agent, text}
  {"cmd": "reset"}           → clears all voice assignments
  Legacy: no "cmd" field     → treated as "speak"
"""

import asyncio
import ctypes
import json
import os
import queue
import random
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

VOICE_SERVER_PORT = 52718
HISTORY_DIR = Path(tempfile.gettempdir()) / "claude_voice_history"
MAX_HISTORY = 25

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
}

# Model display names (derived from agent ID prefix)
MODEL_DISPLAY = {
    "opus": "Opus 4.6",
    "sonnet": "Sonnet 4.6",
    "haiku": "Haiku 4.5",
}


def _model_from_agent(agent_id: str) -> str:
    """Extract model display name from agent ID prefix (e.g. 'opus-main' → 'Opus 4.6')."""
    prefix = agent_id.split("-")[0].lower() if agent_id else ""
    return MODEL_DISPLAY.get(prefix, prefix or "?")


# Curated voice pool — 14 distinct English voices
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

    def assign(self, agent_id: str, voice_name: str) -> bool:
        """Manually assign a voice. Returns True if voice was found in pool."""
        with self._lock:
            match = next((v for v in VOICE_POOL if v["name"] == voice_name), None)
            if not match:
                return False
            self._assignments[agent_id] = {"voice": match["name"], "label": match["label"]}
            self._save()
            return True

    def get_assignments(self) -> dict:
        """Return all current assignments."""
        with self._lock:
            return dict(self._assignments)

    def get_pool(self) -> list[dict]:
        """Return pool with assignment info."""
        with self._lock:
            used_by = {}
            for agent, info in self._assignments.items():
                used_by[info["voice"]] = agent
            return [
                {**v, "assigned_to": used_by.get(v["name"])}
                for v in VOICE_POOL
            ]

    def reset(self):
        """Clear all assignments and delete save file."""
        with self._lock:
            self._assignments.clear()
            try:
                self._SAVE_FILE.unlink(missing_ok=True)
            except OSError:
                pass


# ── Audio Player (Windows MCI) ──────────────────────────────────────────────


class MCIPlayer:
    """Audio player using Windows MCI — zero external dependencies."""

    def __init__(self):
        self._mci = ctypes.windll.winmm.mciSendStringW
        self._alias = "claude_voice"
        self.current_file = None
        self._open = False

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
        # "stopped" or empty → playback ended
        self._open = False
        return "stopped"


# ── Voice Player UI ─────────────────────────────────────────────────────────


class VoicePlayerUI:
    def __init__(self):
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

        self.msg_queue = queue.Queue()
        self._play_queue = queue.Queue()  # items ready to play, in order
        self._playing = False  # whether something is currently playing
        self._play_started_at = 0  # time.time() when playback last started
        self.history = []  # [{text, agent, voice, audio_path, timestamp}]
        self.player = MCIPlayer()
        self.registry = VoiceRegistry()
        self.current_item = None
        self._gen_id = 0  # generation counter for cancellation

        HISTORY_DIR.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._bind_keys()
        self._start_server()
        self._poll_queue()
        self._poll_state()
        self._poll_history_age()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_icon(self):
        """Set window icon from bundled pac-man .ico file."""
        ico_path = Path(__file__).parent / "pacman.ico"
        if ico_path.exists():
            self.root.iconbitmap(str(ico_path))

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
        self.play_btn = tk.Button(btn_box, text="\u25b6 Play", command=self._on_play, **bs)
        self.play_btn.pack(side="left", padx=2)
        self.pause_btn = tk.Button(btn_box, text="\u23f8 Pause", command=self._on_pause, **bs)
        self.pause_btn.pack(side="left", padx=2)
        self.replay_btn = tk.Button(btn_box, text="\u21ba Replay", command=self._on_replay, **bs)
        self.replay_btn.pack(side="left", padx=2)

        self.status_text = tk.Label(
            ctrl, text="Idle", fg=C["subtext"], bg=C["bg"],
            font=("Consolas", 9),
        )
        self.status_text.pack(pady=(4, 0))

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
            hf, style="Voice.Treeview", columns=("ago", "session", "model", "name", "msg"),
            show="headings", selectmode="browse",
        )
        self.hist_tree.heading("ago", text="Time", anchor="w")
        self.hist_tree.heading("session", text="Session", anchor="w")
        self.hist_tree.heading("model", text="Model", anchor="w")
        self.hist_tree.heading("name", text="Agent", anchor="w")
        self.hist_tree.heading("msg", text="Message", anchor="w")
        self.hist_tree.column("ago", width=90, minwidth=60, stretch=False)
        self.hist_tree.column("session", width=70, minwidth=50, stretch=False)
        self.hist_tree.column("model", width=70, minwidth=50, stretch=False)
        self.hist_tree.column("name", width=85, minwidth=60, stretch=False)
        self.hist_tree.column("msg", width=120, minwidth=80, stretch=True)

        sb = ttk.Scrollbar(hf, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=sb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.hist_tree.bind("<Double-Button-1>", self._on_hist_click)
        self.hist_tree.bind("<Return>", self._on_hist_click)

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
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _bind_keys(self):
        self.root.bind("<space>", lambda e: self._on_pause())
        self.root.bind("r", lambda e: self._on_replay())
        self.root.bind("<Escape>", lambda e: self._on_close())
        self.root.bind("<Control-q>", lambda e: self._on_close())

    # ── TCP Server ───────────────────────────────────────────────────────

    def _start_server(self):
        def loop():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("127.0.0.1", VOICE_SERVER_PORT))
            except OSError:
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

    def _handle_conn(self, conn):
        try:
            data = b""
            conn.settimeout(5.0)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            msg = json.loads(data.decode("utf-8"))
            response = self._dispatch(msg)
            conn.sendall(response.encode("utf-8"))
        except Exception:
            pass
        finally:
            conn.close()

    def _dispatch(self, msg: dict) -> str:
        """Route incoming TCP message to the right handler."""
        cmd = msg.get("cmd", "speak")  # default = speak for backward compat

        if cmd == "speak":
            agent = msg.get("agent", "unknown")
            session = msg.get("session", "default")
            registry_key = f"{session}/{agent}"
            voice_override = msg.get("voice")
            if voice_override:
                voice = voice_override
                self.registry.assign(registry_key, voice)
            else:
                voice, _ = self.registry.get_voice(registry_key)
            msg["voice"] = voice
            msg["session"] = session
            self.msg_queue.put(msg)
            _, label = self.registry.get_voice(registry_key)
            return json.dumps({"ok": True, "voice": voice, "label": label})

        if cmd == "assignments":
            return json.dumps({"ok": True, "assignments": self.registry.get_assignments()})

        if cmd == "assign":
            agent = msg.get("agent", "")
            voice = msg.get("voice", "")
            ok = self.registry.assign(agent, voice)
            return json.dumps({"ok": ok, "error": None if ok else f"Voice {voice} not in pool"})

        if cmd == "pool":
            return json.dumps({"ok": True, "pool": self.registry.get_pool()})

        if cmd == "stop":
            self.msg_queue.put({"_internal": "stop"})
            return json.dumps({"ok": True})

        if cmd == "status":
            state = self.player.state
            return json.dumps({
                "ok": True,
                "state": state,
                "agent": self.current_item.get("agent") if self.current_item else None,
                "text": self.current_item.get("text") if self.current_item else None,
            })

        if cmd == "reset":
            self.registry.reset()
            return json.dumps({"ok": True})

        return json.dumps({"ok": False, "error": f"Unknown command: {cmd}"})

    # ── Polling ──────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if msg.get("_internal") == "stop":
                    self.player.stop()
                else:
                    self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _poll_state(self):
        state = self.player.state
        if state == "playing":
            self.status_dot.configure(fg=C["green"])
            self.status_text.configure(text="Playing\u2026")
        elif state == "paused":
            self.status_dot.configure(fg=C["yellow"])
            self.status_text.configure(text="Paused")
        else:
            if self._playing and (time.time() - self._play_started_at) > 0.3:
                self._playing = False
                self.root.after(50, self._drain_play_queue)
            self.status_dot.configure(fg=C["subtext"])
            self.status_text.configure(text="Finished" if self.current_item else "Idle")
        self.root.after(250, self._poll_state)

    def _poll_history_age(self):
        """Re-render history list every 60s to update relative timestamps."""
        if self.history:
            self._refresh_history()
        self.root.after(60_000, self._poll_history_age)

    # ── Message Handling ─────────────────────────────────────────────────

    def _handle_message(self, msg):
        text = msg.get("text", "")
        agent = msg.get("agent", "unknown")
        name = msg.get("name", agent)
        model = _model_from_agent(name)
        session = msg.get("session", "default")
        voice = msg.get("voice", "")
        now = datetime.now()
        ts = now.strftime("%H:%M:%S")

        # Bring window to front
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        # Show generating status if nothing else is playing
        if not self._playing:
            self._set_message(text)
            self.agent_label.configure(text=f"{model} \u2022 {name}")
            self.root.title(f"Claude Voice \u2014 {session}")
            self.time_label.configure(text=ts)
            self.status_text.configure(text="Generating\u2026")
            self.status_dot.configure(fg=C["yellow"])

        def gen_and_enqueue():
            try:
                path = self._generate_tts(text, voice)
                item = dict(text=text, agent=agent, name=name, model=model, session=session, voice=voice, audio_path=str(path), timestamp=ts, created=now)
                self._play_queue.put(item)
                self.root.after(0, self._drain_play_queue)
            except Exception as e:
                self.root.after(0, lambda: self._show_error(str(e)))

        threading.Thread(target=gen_and_enqueue, daemon=True).start()

    def _generate_tts(self, text, voice):
        import edge_tts

        path = HISTORY_DIR / f"voice_{int(time.time() * 1000)}.mp3"
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(edge_tts.Communicate(text, voice).save(str(path)))
        finally:
            loop.close()
        return path

    def _drain_play_queue(self):
        """Start the next queued item if nothing is currently playing."""
        if self._playing:
            return
        try:
            item = self._play_queue.get_nowait()
        except queue.Empty:
            return
        self._playing = True
        self._start_playback(item)

    def _start_playback(self, item):
        self.current_item = item
        # Update display for this item
        self._set_message(item["text"])
        self.agent_label.configure(text=f"{item['model']} \u2022 {item['name']}")
        self.root.title(f"Claude Voice \u2014 {item['session']}")
        self.time_label.configure(text=item["timestamp"])
        self._play_started_at = time.time()
        self.player.play(item["audio_path"])

        # Push to history
        self.history.insert(0, item)
        while len(self.history) > MAX_HISTORY:
            old = self.history.pop()
            try:
                os.unlink(old["audio_path"])
            except OSError:
                pass
        self._refresh_history()

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
            session = item.get("session", "?")
            model = item.get("model", _model_from_agent(item.get("agent", "")))
            name = item.get("name", item["agent"])
            self.hist_tree.insert("", "end", iid=str(i), values=(time_col, session, model, name, item["text"]))

    # ── Button Handlers ──────────────────────────────────────────────────

    def _on_play(self):
        state = self.player.state
        if state == "paused":
            self.player.resume()
        elif self.current_item:
            self.player.play(self.current_item["audio_path"])

    def _on_pause(self):
        state = self.player.state
        if state == "playing":
            self.player.pause()
        elif state == "paused":
            self.player.resume()

    def _on_replay(self):
        if self.current_item:
            self.player.play(self.current_item["audio_path"])

    def _on_hist_click(self, _event=None):
        sel = self.hist_tree.selection()
        if not sel:
            return
        item = self.history[int(sel[0])]
        self.current_item = item
        self._set_message(item["text"])
        model = item.get("model", _model_from_agent(item.get("agent", "")))
        name = item.get("name", item["agent"])
        self.agent_label.configure(text=f"{model} \u2022 {name}")
        self.root.title(f"Claude Voice — {item.get('session', '?')}")
        self.time_label.configure(text=item["timestamp"])
        self.player.play(item["audio_path"])

    def _on_close(self):
        self.player.stop()
        self.root.destroy()

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # Exit immediately if another instance already has the port
    _check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _check.bind(("127.0.0.1", VOICE_SERVER_PORT))
        _check.close()
    except OSError:
        sys.exit(0)
    VoicePlayerUI().run()
