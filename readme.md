# Interminal

A lightweight MCP server that gives AI assistants real terminal access — SSH and local, with interactive command support.

## Features

- **Native SSH** via paramiko — no PTY emulation layer
- **Local terminal** with PTY support (pywinpty on Windows, stdlib pty on Linux/macOS)
- **Interactive commands** — `execute` returns `partial` when a command is waiting for input; use `respond` to answer prompts
- **Control signals** — send Ctrl+C / Ctrl+Z / Ctrl+\ to running commands
- **ANSI cleaning** — pyte strips escape sequences from PTY output (optional)
- **~700 lines, 4 files** — easy to read and modify

## Installation

```bash
pip install mcp paramiko
# Optional: PTY support on Windows
pip install pywinpty
# Optional: ANSI escape sequence cleaning
pip install pyte
```

## Usage

Add to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "interminal": {
      "command": "python",
      "args": ["path/to/interminal/mcp_server.py"]
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `create_local` | Create a local terminal session |
| `connect_ssh` | Connect to an SSH server, returns session_id and banner |
| `execute` | Run a command; returns `completed` or `partial` |
| `respond` | Send input to a waiting command |
| `send_control` | Send Ctrl+C / Ctrl+Z / Ctrl+\ |
| `disconnect` | Close a session and clean up |
| `list_sessions` | List all active sessions |

### Interaction model

```
execute(session_id, "apt install vim")
→ { status: "partial", output: "Do you want to continue? [Y/n]", command_id: "..." }

respond(command_id, "y")
→ { status: "completed", output: "...", exit_code: 0 }
```

Long-running processes (servers, REPLs, TUIs) always return `partial` — use `respond` for input and `send_control` to interrupt.

## Requirements

- Python 3.11+
- `mcp`, `paramiko`
