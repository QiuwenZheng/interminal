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
mcp_server.py          ← FastMCP tool definitions (6 tools)
    ↓
session_manager.py     ← SessionManager: SSH sessions + transient local execution
    ↓
channel.py             ← Channel abstraction (SSHChannel | LocalChannel | PtyChannel)
    ↓
command.py             ← RunningCommand: background read loop, UTF-8 decoding
```

### Key design decisions

**Channel duck-typing** — `SSHChannel`, `LocalChannel`, and `PtyChannel` share an interface without ABC/abstractmethod. All three implement `read()`, `write()`, `close()`, `send_control()`, and `is_finished()`.

**Two-phase timeout** (`_wait_for_result` in `session_manager.py`) — Phase 1 waits up to `pause_timeout + 1s` for the first byte; if it times out we return immediately (already satisfies the "silent for pause_timeout" contract — going into Phase 2 would waste another full pause_timeout). Phase 2 only runs when bytes are actually arriving, collecting until a `pause_timeout` gap or `total_timeout` hits.

**PtyChannel platform split** — Windows uses `pywinpty.PtyProcess`; Unix uses `stdlib pty` with `fcntl` non-blocking I/O. `pywinpty` is an optional dependency.

**SSHChannel stdin reference** — `session_manager.py` explicitly retains a reference to `stdin` to prevent Python GC from calling `__del__` and closing the paramiko channel prematurely.

**Incremental UTF-8 decoding** — `command.py` uses `codecs.getincrementaldecoder` so multibyte characters split across reads are assembled correctly.

**pyte integration** (`_render_pyte` in `session_manager.py`) — optional ANSI escape sequence cleaning via a `pyte.Screen` subclass that works around a signature mismatch in the upstream library.

**`RunningCommand.close` ordering** — closes the channel *before* awaiting `read_task`. Windows PTY's `read()` runs inside `asyncio.to_thread(pty.read)`, which `task.cancel()` cannot actually interrupt — the future stays pending until the underlying thread exits, and the thread won't exit until the PTY is closed. Closing the channel first unblocks the read, then cancel + await tears the task down cleanly.

**`_read_loop` uses `try/finally`** — `CancelledError` is a `BaseException` subclass, not `Exception`, so a cancel on the read task bypasses the inner `except Exception`. The `finally` block guarantees `running=False` + `new_data_event.set()` always run, so any `_wait_for_result` waiter wakes immediately on cancel instead of stalling until its own `pause_timeout` fires.

### Session lifecycle

**SSH:** `connect_ssh()` creates a `Session` in `SessionManager.sessions`. `execute(session_id=...)` opens a channel on the SSH connection. `disconnect()` tears down the SSH client and all its commands.

**Local:** `execute()` without `session_id` creates a transient channel (PtyChannel or LocalChannel) — no Session is stored. The channel is cleaned up when the command completes or is interrupted via `send_control`.

In both cases, `execute()` wraps the channel in a `RunningCommand` with a background `asyncio` read loop. `respond()` / `send_control()` write directly to the running command's channel.

## Persistent shell via multiplexers (zellij, tmux)

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

# 3. Run a command (one execute call — && chains both steps):
execute("~/work/zellij -s train action write-chars 'bash start.sh' && ~/work/zellij -s train action send-keys Enter")

# 4. Read screen content (returned in execute's output field):
execute("~/work/zellij -s train action dump-screen")
execute("~/work/zellij -s train action dump-screen --full")  # + scrollback

execute("~/work/zellij list-sessions")
execute("~/work/zellij delete-session train --force")
```

### Why `write-chars` + `send-keys Enter` instead of `new-pane -- command`

`new-pane -- cmd` and `new-tab -- cmd` directly `exec` the command without
a shell. Shell builtins (`cd`, `echo`), pipes (`|`), redirects (`>`), and
variable expansion (`$VAR`) all fail. You'd have to wrap everything in
`powershell -c "..."` or `bash -c "..."` to make it work.

`write-chars` types into the pane's existing shell, so everything works
exactly as if typed by hand. It also avoids pane proliferation — no new
panes or tabs are created.

For other zellij operations (new-tab, new-pane, list-panes, etc.), run
`zellij action --help` to discover available commands.

### Why use the CLI path instead of `respond`

Prefer driving zellij via its CLI (`write-chars`, `send-keys`, `dump-screen`)
over interminal's `respond` / `send_control` on the live TUI channel:

1. **Output quality** — `respond` returns the TUI's raw screen rendering
   (borders, status bar, ANSI cursor moves), not clean command output.
   `dump-screen` returns clean text.
2. **Coupling** — `respond` requires the original partial `command_id` to
   stay alive. The CLI approach is stateless: the daemon survives
   independently of any channel.

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
- `pyte` — ANSI escape sequence rendering
- `pywinpty` (Windows only) — Windows PTY support

Python ≥ 3.11 required.
