#!/usr/bin/env python3
"""
Claude Voice client — sends commands to the Voice Player UI.

If the UI isn't running, launches it as a background process.
Falls back to direct ffplay playback if the UI can't be reached.

Voice assignment is identity-scoped. Each unique project/task/role/model combo
gets a random voice from the pool. Voices persist across restarts.

Usage:
    python voice.py "Your message here" --project myapp --task auth-fix --role main --model opus
    python voice.py "Message" --project myapp --task auth-fix --role explore --model sonnet --rate "+50%"
    python voice.py --assignments
    python voice.py --pool
    python voice.py --stop
    python voice.py --reset
"""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from voice_common import (
    DEFAULT_RATE,
    DEFAULT_VOICE,
    send_command,
    ensure_server,
    validate_rate,
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


async def _fallback_speak(text: str, voice: str, rate: str = DEFAULT_RATE):
    """Direct playback without UI — used if server can't be reached."""
    import subprocess
    import edge_tts

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        temp_path = f.name
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(temp_path)
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", temp_path],
            check=True,
        )
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def cmd_speak(args):
    """Send a speak command."""
    if not args.text:
        print("Error: text is required for speak", file=sys.stderr)
        sys.exit(1)

    rate = validate_rate(args.rate) if args.rate else DEFAULT_RATE
    project = args.project or Path.cwd().name
    task = args.task or "default"
    role = args.role or "main"
    model = args.model or "unknown"

    agent_key = f"{project}/{task}/{role}"
    agent_display = f"{agent_key} ({model})"

    message = {
        "cmd": "speak",
        "text": args.text,
        "agent": agent_key,
        "model": model,
        "agent_display": agent_display,
        "rate": rate,
    }
    if args.voice:
        message["voice"] = args.voice

    if not ensure_server():
        print("Voice UI unavailable, falling back to direct playback.", file=sys.stderr)
        asyncio.run(_fallback_speak(args.text, args.voice or DEFAULT_VOICE, rate))
        return

    result = send_command(message)
    if result and result.get("ok"):
        voice = result.get("voice", "?")
        label = result.get("label", "")
        if label:
            print(f"[{agent_display}] {label}: {voice}")
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
        description="Claude Voice — TTS with identity-scoped voice assignment"
    )
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("--voice", "-v", help="Override voice (bypasses pool assignment)")
    parser.add_argument("--project", "-p", help="Project name (default: cwd folder name)")
    parser.add_argument("--task", "-t", help="Task name (e.g. 'auth-fix', 'mute-button')")
    parser.add_argument("--role", help="Agent role: main, explore, test, plan, review (default: main)")
    parser.add_argument("--model", "-m", help="Model name: opus, sonnet, haiku")
    parser.add_argument("--rate", "-r", help='Speech rate (e.g. "+25%%", "-10%%", "+0%%")')

    # Query commands
    parser.add_argument("--assignments", action="store_true", help="Show agent voice assignments")
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
