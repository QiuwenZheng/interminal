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

**Two-phase timeout** (`_wait_for_result` in `session_manager.py`) — Phase 1 waits up to `pause_timeout + 1s` for the first byte; if it times out we return immediately (already satisfies the "silent for pause_timeout" contract — going into Phase 2 would waste another full pause_timeout). Phase 2 only runs when bytes are actually arriving, collecting until a `pause_timeout` gap or `total_timeout` hits.

**PtyChannel platform split** — Windows uses `pywinpty.PtyProcess`; Unix uses `stdlib pty` with `fcntl` non-blocking I/O. `pywinpty` is an optional dependency.

**SSHChannel stdin reference** — `session_manager.py` explicitly retains a reference to `stdin` to prevent Python GC from calling `__del__` and closing the paramiko channel prematurely.

**Incremental UTF-8 decoding** — `command.py` uses `codecs.getincrementaldecoder` so multibyte characters split across reads are assembled correctly.

**pyte integration** (`_render_pyte` in `session_manager.py`) — optional ANSI escape sequence cleaning via a `pyte.Screen` subclass that works around a signature mismatch in the upstream library.

**`RunningCommand.close` ordering** — closes the channel *before* awaiting `read_task`. Windows PTY's `read()` runs inside `asyncio.to_thread(pty.read)`, which `task.cancel()` cannot actually interrupt — the future stays pending until the underlying thread exits, and the thread won't exit until the PTY is closed. Closing the channel first unblocks the read, then cancel + await tears the task down cleanly.

**`_read_loop` uses `try/finally`** — `CancelledError` is a `BaseException` subclass, not `Exception`, so a cancel on the read task bypasses the inner `except Exception`. The `finally` block guarantees `running=False` + `new_data_event.set()` always run, so any `_wait_for_result` waiter wakes immediately on cancel instead of stalling until its own `pause_timeout` fires.

### Session lifecycle

1. `create_local()` or `connect_ssh()` creates a `Session` dataclass entry in `SessionManager.sessions`.
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

### Running a command in a new tab's default pane

zellij's `action new-tab` does **not** accept `-- command` (unlike `new-pane`).
A naive "open a tab and train" therefore ends up as `new-tab` (creates an empty
default pane running the shell) + `new-pane -- bash start.sh` (a second pane
split into the tab), which leaves a leftover empty pane.

To run the command in the new tab's default pane instead, chain `write-chars`
+ `write 13` — `new-tab` focuses the new tab automatically, so the keystrokes
land in its default pane:

```
execute("~/work/zellij --session train action new-tab --name v4")
execute("~/work/zellij --session train action write-chars 'bash start.sh'")
execute("~/work/zellij --session train action write 13")   # 13 = Enter (CR)
```

tmux is simpler — `new-window` takes the command directly:

```
execute("tmux new-window -t main -n train 'bash start.sh'")
```

This is NOT the same as "puppeting the TUI": `write-chars` / `send-keys` are
the multiplexer's own structured CLI for injecting input. The thing to avoid
is using interminal's `respond` / `send_control` against the live TUI display
— that races against the renderer.

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
