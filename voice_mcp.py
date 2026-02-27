#!/usr/bin/env python3
"""
Claude Voice MCP Server — exposes voice tools via Model Context Protocol.

Proxies all commands to the voice_ui.py TCP server (localhost:52718).
Agents get native MCP tools instead of shelling out to voice.py.

Usage in ~/.claude/settings.json or claude_desktop_config.json:
    {
        "mcpServers": {
            "voice": {
                "command": "python",
                "args": ["c:/projects/claude-voice-mcp/voice_mcp.py"]
            }
        }
    }
"""

import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Server Setup ─────────────────────────────────────────────────────────────

mcp = FastMCP("voice")

VOICE_SERVER_PORT = 52718
LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / "voice_log.jsonl"


def log_command(tool: str, params: dict, result: str):
    """Append a JSONL entry to the voice log file."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        **params,
        "result": result,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── TCP Client ───────────────────────────────────────────────────────────────


def send_command(message: dict) -> dict | None:
    """Send a command to the voice UI TCP server."""
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


_LAUNCH_LOCK = Path(__file__).parent / ".voice_ui_launching"


def ensure_server() -> bool:
    """Make sure the voice UI server is running."""
    result = send_command({"cmd": "status"})
    if result:
        return True

    # Use a lock file to prevent multiple processes from launching the UI
    try:
        # Check if another process is already launching (lock file < 15s old)
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

        # We're the launcher — create lock file
        _LAUNCH_LOCK.write_text(str(time.time()))
    except OSError:
        pass

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


# ── MCP Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def voice_speak(
    text: str,
    model: str,
    role: str = "main",
    project: str = "default",
    session: str = "default",
    task: str = "main",
    voice: str | None = None,
) -> str:
    """Speak a message aloud using text-to-speech. Each unique combination of
    project + session + task + model + role gets a persistent voice from a pool of 42 voices.

    IMPORTANT — How to fill in the parameters:
    - model: Your AI model name in lowercase. One of: "opus", "sonnet", "haiku".
    - role: Your role/purpose. One of: "main", "explore", "test", "plan", "review".
    - project: The project/codebase you are working in. Use the name of the current
      working directory (e.g. "trace", "myapp"). Just one word, no slashes.
    - session: What the user asked you to work on (e.g. "editor", "map-perf", "auth-fix").
      Pick a short name on your first call and reuse it for the entire conversation.
      Just one word or hyphenated-word, no slashes.
    - task: What THIS agent is doing. Main agents use "main". Subagents describe their
      specific job (e.g. "research", "tests", "table-investigation", "dessert").
      If the parent agent told you a task name, use it.

    Examples:
      Main agent working on editor:         project="trace", session="editor", task="main"
      Subagent researching for editor:      project="trace", session="editor", task="research"
      Subagent running tests for editor:    project="trace", session="editor", task="tests"

    Args:
        text: The message to speak aloud. Keep it concise (1-3 sentences).
        model: Your model name: "opus", "sonnet", or "haiku".
        role: Your role: "main", "explore", "test", "plan", "review", etc.
        project: Project name — just your current working directory name. No slashes.
        session: What the user asked for — one word like "editor" or "auth-fix". No slashes.
        task: What you are doing — "main" for main agents, or a short description for subagents.
        voice: Optional override. A full Edge TTS voice ID like "en-US-AriaNeural". Rarely needed.
    """
    if not ensure_server():
        return "Error: Voice UI server is not running and could not be started."

    agent_id = f"{model}-{role}"
    session_path = f"{project}/{session}/{task}"
    message = {
        "cmd": "speak",
        "text": text,
        "agent": agent_id,
        "name": agent_id,
        "session": session_path,
    }
    if voice:
        message["voice"] = voice

    result = send_command(message)
    if result and result.get("ok"):
        assigned_voice = result.get("voice", "?")
        label = result.get("label", "")
        response = f"[{session_path}/{agent_id}] {label}: {text}"
        log_command("voice_speak", {"model": model, "role": role, "project": project, "session": session, "task": task, "text": text}, response)
        return response
    error = f"Error: {result.get('error', 'unknown') if result else 'server unreachable'}"
    log_command("voice_speak", {"model": model, "role": role, "project": project, "session": session, "task": task, "text": text}, error)
    return error


@mcp.tool()
def voice_stop() -> str:
    """Stop any currently playing voice audio."""
    result = send_command({"cmd": "stop"})
    if result and result.get("ok"):
        log_command("voice_stop", {}, "Playback stopped.")
        return "Playback stopped."
    log_command("voice_stop", {}, "Voice UI not running.")
    return "Voice UI not running."


@mcp.tool()
def voice_assignments() -> str:
    """Show all current voice assignments (which agent has which voice).

    Returns a formatted table of all project/session/agent → voice mappings.
    """
    if not ensure_server():
        return "Error: Voice UI server is not running."

    result = send_command({"cmd": "assignments"})
    if not result or not result.get("ok"):
        return "Error: Failed to get assignments."

    assignments = result["assignments"]
    if not assignments:
        return "No voice assignments yet. Agents get voices on first speak."

    lines = [f"{'Key':<40} {'Voice':<30} {'Label'}"]
    lines.append("-" * 85)
    for key, info in sorted(assignments.items()):
        lines.append(f"{key:<40} {info['voice']:<30} {info['label']}")
    return "\n".join(lines)


@mcp.tool()
def voice_pool() -> str:
    """Show the curated voice pool with assignment status.

    Returns a table of all 42 available voices with their gender, locale,
    and which agent (if any) they're currently assigned to.
    """
    if not ensure_server():
        return "Error: Voice UI server is not running."

    result = send_command({"cmd": "pool"})
    if not result or not result.get("ok"):
        return "Error: Failed to get pool."

    pool = result["pool"]
    lines = [f"{'Voice':<30} {'Gender':<8} {'Locale':<8} {'Assigned To'}"]
    lines.append("-" * 80)
    for v in pool:
        assigned = v.get("assigned_to") or ""
        lines.append(f"{v['name']:<30} {v['gender']:<8} {v['locale']:<8} {assigned}")
    return "\n".join(lines)


@mcp.tool()
def voice_log(limit: int = 20) -> str:
    """Show recent voice command history from the log file.

    Args:
        limit: Number of recent entries to show (default 20, max 100).
    """
    if not LOG_FILE.exists():
        return "No voice log entries yet."

    limit = min(max(1, limit), 100)
    lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-limit:]

    output = []
    for line in recent:
        entry = json.loads(line)
        ts = entry.get("ts", "?")[:19].replace("T", " ")
        tool = entry.get("tool", "?")
        if tool == "voice_speak":
            model = entry.get("model", "?")
            role = entry.get("role", "?")
            session = entry.get("session", "?")
            text = entry.get("text", "")
            preview = text[:60] + "..." if len(text) > 60 else text
            output.append(f"[{ts}] {tool} | {session}/{model}-{role} | {preview}")
        else:
            result = entry.get("result", "")
            output.append(f"[{ts}] {tool} | {result}")

    return "\n".join(output)


@mcp.tool()
def voice_setup() -> str:
    """Get the CLAUDE.md voice rule to add to any project.

    Returns the exact text to paste into a project's CLAUDE.md file so that
    all agents (main and sub) know how to use the voice system correctly.
    Copy the output and add it to your CLAUDE.md rules section.
    """
    return '''## Voice Rule — paste this into your CLAUDE.md rules section:

**ALWAYS USE VOICE** — MANDATORY: call the `voice_speak` MCP tool with every completion or question.
- `model`: your model — `opus`, `sonnet`, or `haiku`
- `role`: your role — `main`, `explore`, `test`, `plan`, `review`, etc.
- `project`: project name — just your cwd name (e.g. `trace`). No slashes.
- `session`: what the user asked for — one word (e.g. `editor`, `map-perf`). No slashes.
- `task`: what you are doing — `main` for main agents, or a short name for subagents (e.g. `research`, `tests`).
- When spawning subagents, tell them the project, session, and their task name.'''


# ── Entry Point ──────────────────────────────────────────────────────────────


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
