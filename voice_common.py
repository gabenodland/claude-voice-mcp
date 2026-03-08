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

_PROJECT_DIR = Path(__file__).parent
LOG_FILE = _PROJECT_DIR / "voice_ui_errors.log"

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


def send_command(message: dict, timeout: float = 10.0) -> dict | None:
    """Send a command to the voice UI TCP server. Returns parsed JSON response or None."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", VOICE_SERVER_PORT))
        sock.sendall(json.dumps(message).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        max_response = 1024 * 1024  # 1 MB safety cap
        data = b""
        while len(data) < max_response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        return json.loads(data.decode("utf-8"))
    except (ConnectionRefusedError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError:
        return None


# ── Zombie Cleanup ───────────────────────────────────────────────────────────


def kill_port_holder(port: int = VOICE_SERVER_PORT) -> bool:
    """Find and kill any process holding the given port. Returns True if a process was killed."""
    if sys.platform != "win32":
        return False  # TODO: cross-platform support

    try:
        # Use netstat to find the PID holding the port
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            # Match lines like: TCP    127.0.0.1:52718    ...    LISTENING    12345
            if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) > 0:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=5,
                    )
                    time.sleep(0.5)  # Let the OS release the port
                    return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


# ── Server Launcher ──────────────────────────────────────────────────────────


def launch_ui_server():
    """Launch voice_ui.py outside the current process tree.

    On Windows, the MCP server runs inside Electron's Job Object, which causes
    ~30s subprocess startup delays (Windows Defender + Job Object inheritance).
    Using WMI Win32_Process.Create launches via svchost.exe, fully escaping
    the Electron process tree. Falls back to subprocess.Popen if WMI fails.
    """
    ui_script = _PROJECT_DIR / "voice_ui.py"

    if sys.platform == "win32":
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        exe = str(pythonw) if pythonw.exists() else sys.executable
        cmd_line = f'"{exe}" "{ui_script}"'

        try:
            # WMI Win32_Process.Create runs via svchost.exe — outside Electron's
            # Job Object, avoiding the 30-second Defender/job-throttle delay.
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"([wmiclass]'Win32_Process').Create('{cmd_line}')"],
                capture_output=True, text=True, timeout=10,
            )
            # WMI returns ReturnValue : 0 on success, non-zero on failure
            if result.returncode == 0 and re.search(r"ReturnValue\s*:\s*0\b", result.stdout):
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Fallback: direct Popen (may be slow inside Electron's Job Object)
        _popen_ui_server(exe, ui_script)
    else:
        _popen_ui_server(sys.executable, ui_script)


def _popen_ui_server(exe: str, script: Path):
    """Launch voice_ui.py via subprocess.Popen with platform-appropriate flags."""
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe, str(script)], **kwargs)


_LAUNCH_MUTEX_NAME = "Claude_Voice_MCP_LaunchLock"


def ensure_server() -> bool:
    """Make sure the voice UI server is running. Returns True if reachable.

    Uses a cross-process mutex to prevent concurrent launch attempts.
    Strategy: ping → acquire lock → re-ping → kill zombie → launch → poll.
    """
    # Fast path: already running
    result = send_command({"cmd": "status"}, timeout=1.0)
    if result:
        return True

    if sys.platform == "win32":
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, _LAUNCH_MUTEX_NAME)
        # Wait up to 12s to acquire (covers another process's launch+poll cycle)
        wait_result = ctypes.windll.kernel32.WaitForSingleObject(mutex, 12000)
        if wait_result not in (0, 128):  # WAIT_OBJECT_0=0, WAIT_ABANDONED=128
            # Could not acquire lock — someone else is launching, just poll
            ctypes.windll.kernel32.CloseHandle(mutex)
            return _poll_for_server(timeout=10.0)
        try:
            return _launch_under_lock()
        finally:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
    else:
        return _launch_under_lock()


def _launch_under_lock() -> bool:
    """The actual launch logic, called while holding the launch mutex."""
    # Re-check after acquiring lock — another process may have launched while we waited
    result = send_command({"cmd": "status"}, timeout=1.0)
    if result:
        return True

    # Kill anything holding the port (zombie from a previous crash)
    kill_port_holder()

    launch_ui_server()

    return _poll_for_server(timeout=10.0)


def _poll_for_server(timeout: float = 10.0) -> bool:
    """Poll for server readiness up to `timeout` seconds (wall-clock)."""
    deadline = time.time() + timeout
    while True:
        result = send_command({"cmd": "status"}, timeout=0.5)
        if result:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)
