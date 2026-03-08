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
                "args": ["/path/to/voice_mcp.py"]
            }
        }
    }
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from voice_common import (
    DEFAULT_RATE,
    send_command,
    ensure_server,
    validate_rate,
)

# ── Server Setup ─────────────────────────────────────────────────────────────

mcp = FastMCP("voice")

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


# ── MCP Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def voice_speak(
    text: str,
    model: str,
    role: str = "main",
    project: str = "default",
    task: str = "main",
    voice: str | None = None,
    rate: str = DEFAULT_RATE,
) -> str:
    """Speak a message aloud using text-to-speech.

    Each agent gets a unique, persistent voice based on its identity parameters.
    CRITICAL: Always use the SAME parameter values across calls so your voice stays consistent.
    Changing any parameter creates a new identity and assigns a different voice.

    Agent identity = project/task/role (model)
    These four values determine your voice. Pick them once and reuse them every call.

    Parameters:
    - model (required): Your AI model in lowercase: "opus", "sonnet", or "haiku".
    - project: The project name — your current working directory (e.g. "myapp"). One word, no slashes.
    - task: What the user asked you to work on (e.g. "mute-button", "auth-fix").
      Pick a short name on your first call and reuse it for the entire conversation.
      One word or hyphenated-word, no slashes.
    - role: What you are doing. Use "main" if you are the primary agent.
      Subagents use their function: "explore", "test", "plan", "review".

    Examples:
      Main agent on mute-button feature:    project="voicemcp", task="mute-button", role="main"
        -> voicemcp/mute-button/main (opus)
      Subagent exploring code for same:     project="voicemcp", task="mute-button", role="explore"
        -> voicemcp/mute-button/explore (sonnet)
      Subagent running tests:               project="voicemcp", task="mute-button", role="test"
        -> voicemcp/mute-button/test (haiku)
      Different task, same project:         project="voicemcp", task="dark-mode", role="main"
        -> voicemcp/dark-mode/main (opus)

    When spawning subagents, pass them your project and task values so they share the
    same task context. Only the role (and possibly model) should differ.

    Args:
        text: The message to speak aloud. Keep it concise (1-3 sentences).
        model: Your model name: "opus", "sonnet", or "haiku".
        role: Your function: "main", "explore", "test", "plan", or "review".
        project: Project name — your current working directory name. No slashes.
        task: The feature/task you are working on. No slashes.
        voice: Optional override. A full Edge TTS voice ID like "en-US-AriaNeural". Rarely needed.
        rate: Speech rate adjustment. Default "+25%". Use "+0%" for normal, up to "+100%" for fast, or negative like "-10%" for slower.
    """
    if not ensure_server():
        return "Error: Voice UI server is not running and could not be started."

    rate = validate_rate(rate)

    agent_key = f"{project}/{task}/{role}"
    agent_display = f"{agent_key} ({model})"
    message = {
        "cmd": "speak",
        "text": text,
        "agent": agent_key,
        "model": model,
        "agent_display": agent_display,
        "rate": rate,
    }
    if voice:
        message["voice"] = voice

    result = send_command(message)
    if result and result.get("ok"):
        label = result.get("label", "")
        response = f"[{agent_display}] {label}: {text}"
        log_command("voice_speak", {"model": model, "role": role, "project": project, "task": task, "rate": rate, "text": text}, response)
        return response
    error = f"Error: {result.get('error', 'unknown') if result else 'server unreachable'}"
    log_command("voice_speak", {"model": model, "role": role, "project": project, "task": task, "rate": rate, "text": text}, error)
    return error


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
            project = entry.get("project", "?")
            task = entry.get("task", "?")
            text = entry.get("text", "")
            preview = text[:60] + "..." if len(text) > 60 else text
            output.append(f"[{ts}] {project}/{task}/{role} ({model}) | {preview}")
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
- `model`: your model in lowercase — `opus`, `sonnet`, or `haiku`.
- `project`: project name — just your cwd name (e.g. `trace`). No slashes.
- `task`: the feature/task the user asked you to work on (e.g. `mute-button`, `auth-fix`). Pick once, reuse every call. No slashes.
- `role`: your function — `main` for the primary agent, or `explore`, `test`, `plan`, `review` for subagents.
- CRITICAL: Use the SAME values every call. project + task + role + model = your voice identity. Changing any value changes your voice.
- When spawning subagents, pass them your `project` and `task` values. Only `role` (and possibly `model`) should differ.
- Messages queue up and play one after the next automatically.'''


# ── Entry Point ──────────────────────────────────────────────────────────────


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
