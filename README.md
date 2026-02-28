# Claude Voice MCP

TTS voice system for Claude Code agents — MCP server with a 42-voice pool, persistent per-agent assignment, and tkinter UI player.

## Requirements

- Python 3.10+
- Windows (uses Windows MCI for audio playback)
- tkinter (included with Python on Windows/macOS; install `python3-tk` on Linux)

## Install

```bash
pip install -r requirements.txt
```

## Setup

Add the MCP server to your Claude Code settings (`~/.claude/settings.json`):

```json
{
    "mcpServers": {
        "voice": {
            "command": "python",
            "args": ["/path/to/voice_mcp.py"]
        }
    }
}
```

## Usage

The MCP server exposes these tools to Claude agents:

| Tool | Description |
|---|---|
| `voice_speak` | Speak a message with auto-assigned voice |
| `voice_stop` | Stop current playback |
| `voice_assignments` | Show agent-to-voice mappings |
| `voice_pool` | Show all available voices |
| `voice_log` | Show recent voice command history |
| `voice_setup` | Get the CLAUDE.md rule to enable voice in a project |

The voice UI launches automatically on first use — a dark-themed tkinter window with playback controls and message history.

## CLI

You can also use `voice.py` directly:

```bash
python voice.py "Hello world" --agent opus-main --session myproject
python voice.py "Hello world" --agent opus-main --session myproject --rate "+50%"
python voice.py --assignments
python voice.py --pool
python voice.py --stop
```

## How It Works

1. **voice_common.py** — Shared constants, TCP client, and server launcher
2. **voice_mcp.py** — MCP server (stdio transport) that agents call
3. **voice_ui.py** — Background tkinter app with TCP server on port 52718, handles TTS generation and audio playback
4. **voice.py** — CLI client for manual use

Each unique `project/session/task/model-role` combination gets a persistent voice from a pool of 42 English voices across 14 locales.
