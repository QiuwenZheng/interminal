# Interminal

<!-- mcp-name: io.github.QiuwenZheng/interminal -->

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
| `execute` | Run a command locally (no session needed) or over SSH; returns output or `status=partial` + `command_id` |
| `read_output` | Poll a running command for new output without sending input |
| `respond` | Send text input to a command waiting at a prompt |
| `send_control` | Send control keys: `ctrl+c`, `ctrl+z`, arrow keys, F-keys, etc. |
| `disconnect` | Close an SSH session and release all resources |

## Recommended: Install Zellij

Each `execute` call runs in an isolated channel — there is no persistent shell between calls. For simple tasks, chaining with `&&` works. For **multi-step workflows** (project development, debugging, deployment), a terminal multiplexer provides persistent state that survives across calls.

**[Zellij](https://zellij.dev/)** is strongly recommended on the host machine (local or remote):

```bash
# Linux / macOS / WSL
cargo install zellij    # or: brew install zellij

# Check if installed
zellij --version
```

With Zellij installed, the AI agent will automatically create a persistent session where `cd`, environment variables, virtual environments, and long-running processes carry over naturally. As a bonus, you can run `zellij attach <session-name>` to watch the AI's terminal work in real-time.

## Key Behaviors

- **Stateless execute** — each call is an isolated channel; `cd /foo` does not persist. Simple tasks: chain with `&&`. Multi-step workflows: the AI will use a Zellij session for persistent state
- **Long-running commands** return `status="partial"` with a `command_id`; poll with `read_output` or send input with `respond`
- **TUI apps** (zellij, vim, htop) must be started in the foreground — never background with `&`; after the server daemonizes, the partial channel can be abandoned
- **SSH PTY** is 500×200 xterm-256color so multiplexer sessions render at your actual terminal size

## Optional Dependencies

```bash
pip install "mcp-interminal[pty]"       # Windows PTY support (pywinpty)
pip install "mcp-interminal[ansi]"      # ANSI escape rendering (pyte)
pip install "mcp-interminal[pty,ansi]"  # both
```
