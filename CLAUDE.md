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

## Persistent shell via multiplexers

Each `execute()` call creates an independent `exec_command` channel — there is no persistent shell between calls. 

For workflows needing state (e.g. `cd`, `export VAR=val`), use a terminal multiplexer (e.g., zellij). Start the multiplexer daemon in the foreground, and interact with it using its CLI features instead of relying on Interminal's interactive tools to manipulate the UI.

## Dependencies

- `mcp[cli]` — FastMCP framework
- `paramiko` — SSH client
- `pyte` — ANSI escape sequence rendering
- `pywinpty` (Windows only) — Windows PTY support

Python ≥ 3.11 required.
