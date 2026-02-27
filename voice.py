#!/usr/bin/env python3
"""
Claude Voice client — sends commands to the Voice Player UI.

If the UI isn't running, launches it as a background process.
Falls back to direct ffplay playback if the UI can't be reached.

Voice assignment is session-scoped. Each unique session/agent combo gets
a random voice from the pool. Voices persist across restarts.

Usage:
    python voice.py "Your message here" --agent opus-main --session settings
    python voice.py "Message" --agent sonnet-explore --session settings
    python voice.py --assignments
    python voice.py --pool
    python voice.py --stop
    python voice.py --reset
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Server port (must match voice_ui.py)
VOICE_SERVER_PORT = 52718

DEFAULT_VOICE = "en-US-AriaNeural"


def send_command(message: dict) -> dict | None:
    """Send a command to the voice UI server. Returns parsed JSON response or None."""
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


def ensure_server() -> bool:
    """Make sure the voice UI server is running. Returns True if reachable."""
    result = send_command({"cmd": "status"})
    if result:
        return True

    launch_ui_server()
    for _ in range(10):
        time.sleep(0.5)
        result = send_command({"cmd": "status"})
        if result:
            return True
    return False


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
        subprocess.Popen(
            [sys.executable, str(ui_script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


async def list_voices(language_filter: str | None = None):
    """List available voices, optionally filtered by language."""
    import edge_tts

    voices = await edge_tts.list_voices()
    if language_filter:
        voices = [v for v in voices if v["Locale"].lower().startswith(language_filter.lower())]
    voices.sort(key=lambda v: (v["Locale"], v["ShortName"]))
    print(f"{'Voice Name':<30} {'Gender':<8} {'Locale':<10}")
    print("-" * 50)
    for voice in voices:
        print(f"{voice['ShortName']:<30} {voice['Gender']:<8} {voice['Locale']:<10}")


async def _fallback_speak(text: str, voice: str):
    """Direct playback without UI — used if server can't be reached."""
    import edge_tts

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        temp_path = f.name
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(temp_path)
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", temp_path],
            check=True,
        )
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def get_session(args) -> str:
    """Get session name: explicit --session, or cwd folder name as fallback."""
    if args.session:
        return args.session
    return Path.cwd().name


def cmd_speak(args):
    """Send a speak command."""
    if not args.text:
        print("Error: text is required for speak", file=sys.stderr)
        sys.exit(1)

    session = get_session(args)
    message = {
        "cmd": "speak",
        "text": args.text,
        "agent": args.agent or "unknown",
        "name": args.name or args.agent or "unknown",
        "session": session,
    }
    if args.voice:
        message["voice"] = args.voice

    if not ensure_server():
        print("Voice UI unavailable, falling back to direct playback.", file=sys.stderr)
        asyncio.run(_fallback_speak(args.text, args.voice or DEFAULT_VOICE))
        return

    result = send_command(message)
    if result and result.get("ok"):
        voice = result.get("voice", "?")
        label = result.get("label", "")
        if label:
            print(f"[{session}/{label}] {voice}")
        return

    print("Failed to send message", file=sys.stderr)


def cmd_assignments(args):
    """Show current voice assignments."""
    if not ensure_server():
        print("Voice UI not running.", file=sys.stderr)
        sys.exit(1)

    result = send_command({"cmd": "assignments"})
    if not result or not result.get("ok"):
        print("Failed to get assignments", file=sys.stderr)
        sys.exit(1)

    assignments = result["assignments"]
    if not assignments:
        print("No voice assignments yet. Agents get voices on first speak.")
        return

    print(f"{'Key':<35} {'Voice':<30} {'Label'}")
    print("-" * 80)
    for key, info in sorted(assignments.items()):
        print(f"{key:<35} {info['voice']:<30} {info['label']}")


def cmd_assign(args):
    """Manually assign a voice to an agent."""
    if not args.assign or len(args.assign) != 2:
        print("Usage: --assign AGENT VOICE", file=sys.stderr)
        sys.exit(1)

    agent, voice = args.assign
    session = get_session(args)
    key = f"{session}/{agent}"

    if not ensure_server():
        print("Voice UI not running.", file=sys.stderr)
        sys.exit(1)

    result = send_command({"cmd": "assign", "agent": key, "voice": voice})
    if result and result.get("ok"):
        print(f"Assigned {voice} to {key}")
    else:
        error = result.get("error", "unknown error") if result else "server unreachable"
        print(f"Failed: {error}", file=sys.stderr)
        sys.exit(1)


def cmd_pool(args):
    """Show the voice pool with assignment status."""
    if not ensure_server():
        print("Voice UI not running.", file=sys.stderr)
        sys.exit(1)

    result = send_command({"cmd": "pool"})
    if not result or not result.get("ok"):
        print("Failed to get pool", file=sys.stderr)
        sys.exit(1)

    pool = result["pool"]
    print(f"{'Voice':<30} {'Gender':<8} {'Locale':<8} {'Assigned To'}")
    print("-" * 80)
    for v in pool:
        assigned = v.get("assigned_to") or ""
        print(f"{v['name']:<30} {v['gender']:<8} {v['locale']:<8} {assigned}")


def cmd_stop(args):
    """Stop current playback."""
    result = send_command({"cmd": "stop"})
    if result and result.get("ok"):
        print("Stopped.")
    else:
        print("Voice UI not running.", file=sys.stderr)


def cmd_status(args):
    """Show playback status."""
    result = send_command({"cmd": "status"})
    if not result:
        print("Voice UI not running.", file=sys.stderr)
        sys.exit(1)

    state = result.get("state", "unknown")
    agent = result.get("agent")
    text = result.get("text")
    print(f"State: {state}")
    if agent:
        print(f"Agent: {agent}")
    if text:
        preview = text[:80] + "..." if len(text) > 80 else text
        print(f"Text:  {preview}")


def cmd_reset(args):
    """Reset all voice assignments."""
    if not ensure_server():
        print("Voice UI not running.", file=sys.stderr)
        sys.exit(1)

    result = send_command({"cmd": "reset"})
    if result and result.get("ok"):
        print("All voice assignments cleared.")
    else:
        print("Failed to reset.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Claude Voice — TTS with session-scoped voice assignment"
    )
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("--voice", "-v", help="Override voice (bypasses pool assignment)")
    parser.add_argument("--agent", "-a", help="Agent ID (e.g. opus-main, sonnet-explore)")
    parser.add_argument("--name", "-n", help="Display name (e.g. 'Code Reviewer')")
    parser.add_argument("--session", "-s", help="Session name (default: cwd folder name)")

    # Query commands
    parser.add_argument("--assignments", action="store_true", help="Show agent voice assignments")
    parser.add_argument("--assign", nargs=2, metavar=("AGENT", "VOICE"), help="Assign voice to agent")
    parser.add_argument("--pool", action="store_true", help="Show curated voice pool")
    parser.add_argument("--stop", action="store_true", help="Stop current playback")
    parser.add_argument("--status", action="store_true", help="Show playback status")
    parser.add_argument("--reset", action="store_true", help="Clear all voice assignments")

    # Voice discovery
    parser.add_argument("--list-voices", "-l", action="store_true", help="List all Edge TTS voices")
    parser.add_argument("--language", help="Filter voices by language code (e.g., 'en', 'es')")

    args = parser.parse_args()

    if args.list_voices:
        asyncio.run(list_voices(args.language))
    elif args.assignments:
        cmd_assignments(args)
    elif args.assign:
        cmd_assign(args)
    elif args.pool:
        cmd_pool(args)
    elif args.stop:
        cmd_stop(args)
    elif args.status:
        cmd_status(args)
    elif args.reset:
        cmd_reset(args)
    elif args.text:
        cmd_speak(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
