# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Interminal** is an MCP server that gives AI assistants terminal access — SSH and local shells — with support for interactive commands. It is distributed via `uvx` and has no build step.

## Running & Installation

```powershell
# Run directly (no install)
python mcp_server.py

# Install from source
pip install .

# Run via uvx (published package)
uvx interminal
```

There are no test, lint, or build commands configured in this project.

## Architecture

```
mcp_server.py          ← FastMCP tool definitions (7 tools)
    ↓
session_manager.py     ← SessionManager: SSH + local session lifecycle
    ↓
channel.py             ← Channel abstraction (SSHChannel | LocalChannel | PtyChannel)
    ↓
command.py             ← RunningCommand: background read loop, UTF-8 decoding
```

### Key design decisions

**Channel duck-typing** — `SSHChannel`, `LocalChannel`, and `PtyChannel` share an interface without ABC/abstractmethod. All three implement `read()`, `write()`, `close()`, `send_control()`, and `is_finished()`.

**Two-phase timeout** (`_wait_for_result` in `session_manager.py`) — first phase waits for any data to arrive; second phase collects output until a pause longer than the threshold occurs. This handles both fast-completing and slow-streaming commands.

**PtyChannel platform split** — Windows uses `pywinpty.PtyProcess`; Unix uses `stdlib pty` with `fcntl` non-blocking I/O. `pywinpty` is an optional dependency.

**SSHChannel stdin reference** — `session_manager.py` explicitly retains a reference to `stdin` to prevent Python GC from calling `__del__` and closing the paramiko channel prematurely.

**Incremental UTF-8 decoding** — `command.py` uses `codecs.getincrementaldecoder` so multibyte characters split across reads are assembled correctly.

**pyte integration** (`_render_pyte` in `session_manager.py`) — optional ANSI escape sequence cleaning via a `pyte.Screen` subclass that works around a signature mismatch in the upstream library.

### Session lifecycle

1. `create_local()` or `connect_ssh()` creates a `Session` dataclass entry in `SessionManager._sessions`.
2. `execute()` wraps the session's shell in a `Channel`, then a `RunningCommand` with a background `asyncio` read loop.
3. `respond()` / `send_control()` write directly to the running command's channel.
4. `disconnect()` closes the channel, terminates the subprocess/SSH client, and removes the session.

## Using TUI multiplexers (zellij, tmux) over interminal

Each `execute()` call creates an independent `exec_command` channel — there is no
persistent shell between calls.

### Why `zellij ... &` fails

Backgrounding a TUI multiplexer with `&` does **not** work, but not because of
SIGHUP. The failure is in zellij's init sequence:

1. Shell parses `zellij &`, forks zellij into a **background process group**, and
   immediately exits (only command in the shell was the backgrounded one).
2. zellij's early init does TTY setup (`tcsetattr`, query terminal size, raw
   mode). A background process group can't safely do this — it triggers
   SIGTTOU/SIGTTIN, and the operations fail.
3. Meanwhile the shell has exited, so the PTY is also collapsing.
4. zellij aborts before it ever reaches the `fork()` + `setsid()` that would
   daemonize the server. No server is ever created.

So the surface symptom "`list-sessions` shows nothing" is correct, but the cause
is "init never completed", not "SIGHUP killed a running server".

### The working pattern

Start zellij **in the foreground** (no `&`) and let `execute` return `partial`.
zellij's server has time to fork + setsid, becoming a daemon in its own session.
After that, the client/channel state is irrelevant — the server lives until
explicitly killed.

```
# 1. Start the session. execute returns "partial" once init is done.
execute("TERM=xterm-256color ~/work/zellij --session train", total_timeout=4)

# 2. The partial channel can be ignored, disconnected, or left to time out.
#    The daemonized server survives all of these.

# 3. Drive the session from independent execute calls:
execute("~/work/zellij --session train action new-pane -- bash start.sh")
execute("~/work/zellij --session train action dump-screen")
execute("~/work/zellij list-sessions")
execute("~/work/zellij delete-session train --force")
```

`TERM=xterm-256color` is required: zellij reads `TERM` at startup to pick its
renderer, and the default inherited from paramiko's PTY is often missing or
minimal. The same prefix is useful for other TUIs.

tmux works similarly but supports detached startup directly:
`tmux new-session -d -s train "bash start.sh"` returns immediately and leaves
a usable session — no partial-channel dance needed, because tmux's `-d` flag
explicitly forks the server before any TTY operations.

## Dependencies

- `mcp[cli]` — FastMCP framework
- `paramiko` — SSH client
- `pywinpty` (optional) — Windows PTY support
- `pyte` (optional) — ANSI escape sequence rendering

Python ≥ 3.11 required.
