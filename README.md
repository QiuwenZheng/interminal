# Interminal

Lightweight MCP server that gives AI assistants terminal access — SSH and local shells — with support for interactive and long-running commands.

## Installation

```bash
# Run directly, no install needed (recommended)
uvx mcp-interminal

# Or install permanently
pip install mcp-interminal
```

Requires Python ≥ 3.11.

## MCP Client Configuration

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "interminal": {
      "command": "uvx",
      "args": ["mcp-interminal"]
    }
  }
}
```

**Cursor / other clients**: same `command` + `args` format above.

## Tools

| Tool | Description |
|------|-------------|
| `connect_ssh` | Connect to an SSH server; returns `session_id` and welcome banner |
| `create_local` | Create a local shell session |
| `execute` | Run a command; returns output or `status=partial` + `command_id` for long-running commands |
| `read_output` | Poll a running command for new output without sending input |
| `respond` | Send text input to a command waiting at a prompt |
| `send_control` | Send control keys: `ctrl+c`, `ctrl+z`, arrow keys, F-keys, etc. |
| `disconnect` | Close a session and release all resources |
| `list_sessions` | List all active sessions |

## Key Behaviors

- **No persistent shell between `execute` calls** — chain state with `&&` (e.g. `cd /foo && ls`)
- **Long-running commands** return `status="partial"` with a `command_id`; poll with `read_output` or send input with `respond`
- **TUI apps** (zellij, tmux, vim, htop) must be started in the foreground — never background with `&`; after the server daemonizes, the partial channel can be abandoned
- **SSH PTY** is 500×200 xterm-256color so multiplexer sessions render at your actual terminal size

## Optional Dependencies

```bash
pip install "mcp-interminal[pty]"       # Windows PTY support (pywinpty)
pip install "mcp-interminal[ansi]"      # ANSI escape rendering (pyte)
pip install "mcp-interminal[pty,ansi]"  # both
```
