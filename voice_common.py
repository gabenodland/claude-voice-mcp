#!/usr/bin/env python3
"""
Shared constants and TCP client for Claude Voice.

Used by voice_mcp.py, voice.py, and voice_ui.py to avoid duplication.
"""

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

VOICE_SERVER_PORT = 52718
DEFAULT_RATE = "+25%"
DEFAULT_VOICE = "en-US-AriaNeural"

# Rate validation: allows +/- 0-200%
_RATE_PATTERN = re.compile(r'^[+-]\d{1,3}%$')
_RATE_MIN = -50
_RATE_MAX = 200


def validate_rate(rate: str) -> str:
    """Validate and return a safe rate string. Returns DEFAULT_RATE if invalid."""
    if not _RATE_PATTERN.match(rate):
        return DEFAULT_RATE
    val = int(rate[1:-1]) * (1 if rate[0] == "+" else -1)
    if not (_RATE_MIN <= val <= _RATE_MAX):
        return DEFAULT_RATE
    return rate


# ── TCP Client ───────────────────────────────────────────────────────────────


def send_command(message: dict) -> dict | None:
    """Send a command to the voice UI TCP server. Returns parsed JSON response or None."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(("127.0.0.1", VOICE_SERVER_PORT))
        sock.sendall(json.dumps(message).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        return json.loads(data.decode("utf-8"))
    except (ConnectionRefusedError, socket.timeout, OSError):
        return None
    except json.JSONDecodeError:
        return None


# ── Server Launcher ──────────────────────────────────────────────────────────

_LAUNCH_LOCK = Path(__file__).parent / ".voice_ui_launching"


def launch_ui_server():
    """Launch voice_ui.py as a detached background process."""
    ui_script = Path(__file__).parent / "voice_ui.py"
    if sys.platform == "win32":
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        subprocess.Popen(
            [sys.executable, str(ui_script)],
            creationflags=DETACHED | NEW_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # TODO: cross-platform — audio player needs a non-MCI backend
        subprocess.Popen(
            [sys.executable, str(ui_script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def ensure_server() -> bool:
    """Make sure the voice UI server is running. Returns True if reachable."""
    result = send_command({"cmd": "status"})
    if result:
        return True

    # Use a lock file to prevent multiple processes from launching the UI
    try:
        # Atomic lock file creation — raises FileExistsError if already held
        if _LAUNCH_LOCK.exists():
            age = time.time() - _LAUNCH_LOCK.stat().st_mtime
            if age < 15:
                # Someone else is launching, just wait for it
                for _ in range(10):
                    time.sleep(0.5)
                    result = send_command({"cmd": "status"})
                    if result:
                        return True
                return False
            # Stale lock — remove and take over
            _LAUNCH_LOCK.unlink(missing_ok=True)

        # Atomic creation: 'x' mode raises FileExistsError if file exists
        with open(_LAUNCH_LOCK, "x") as f:
            f.write(str(time.time()))
    except (FileExistsError, OSError):
        # Another process grabbed the lock between our check and creation
        for _ in range(10):
            time.sleep(0.5)
            result = send_command({"cmd": "status"})
            if result:
                return True
        return False

    launch_ui_server()

    for _ in range(10):
        time.sleep(0.5)
        result = send_command({"cmd": "status"})
        if result:
            try:
                _LAUNCH_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
            return True

    try:
        _LAUNCH_LOCK.unlink(missing_ok=True)
    except OSError:
        pass
    return False
