from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from typing import Optional, Annotated
from pydantic import Field
from session_manager import SessionManager

mcp = FastMCP(
    "Interminal",
    instructions="""\
Terminal access for AI: SSH and local shells with support for interactive,
long-running, and stateful tasks.

KEY PATTERNS (read before first use to save discovery loops):

1. LOCAL COMMANDS — JUST CALL EXECUTE:
   execute("ls -la") works immediately — no session setup needed.
   Each call is stateless: `cd /foo` does NOT persist to the next call.
   - Simple 1-2 step tasks: chain with && (e.g. "cd /foo && ls").
   - Multi-step workflows needing persistent state (cd, env vars, venv,
     long-running processes): start a Zellij session and drive it via CLI
     — see patterns 3-5 below.

2. SSH COMMANDS — CONNECT FIRST:
   connect_ssh(host, ...) returns a session_id. Pass it to execute().
   The connection stays open until disconnect(session_id).

3. PERSISTENT SHELL via Zellij:
   Start a Zellij session for any stateful, ongoing workspace. The daemon
   keeps the shell alive on the host; the user can `zellij attach <name>`
   to observe your work in real-time or provide input (sudo, credentials).
   NEVER background with `&` — start in the FOREGROUND. `execute` returns
   "partial" once the daemon is up; ignore/abandon this partial channel.
   Use a deterministic session name based on the project or task (e.g.
   "myproject-dev") so you can resume after reconnection.
   If Zellij is not available, tmux is a viable alternative.

4. DRIVING ZELLIJ (Avoid Pane Proliferation):
   Once the session is running, use its CLI. REUSE the active pane by
   default — do NOT create a new pane/tab for every command.
   - Run command in current pane:
       execute("zellij --session s action write-chars 'npm run build'")
       execute("zellij --session s action write 13")   # 13 = Enter byte
   - Run command in a NEW tab (`new-tab` does NOT accept `-- cmd`):
       execute("zellij --session s action new-tab --name v4")
       execute("zellij --session s action write-chars 'bash start.sh'")
       execute("zellij --session s action write 13")
   - Read current screen content:
       execute("zellij --session s action dump-screen /tmp/out.txt")
       execute("cat /tmp/out.txt")

5. INTERACTIVE & LONG-RUNNING COMMANDS (without multiplexer):
   Commands that produce ongoing output return status="partial" with a
   command_id. To continue:
     - read_output(command_id) — poll for logs, build output, etc.
     - respond(command_id, text) — answer a prompt (y/n, password).
     - send_control(command_id, signal) — send Ctrl+C, arrows, F-keys.
       Do NOT write control bytes into `respond`; AI frameworks strip them.

""",
)
manager = SessionManager()


@mcp.tool(
    annotations=ToolAnnotations(
        title="Connect to SSH Server",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def connect_ssh(
    host: Annotated[str, Field(description="Hostname or IP of the SSH server (e.g. '192.168.1.10', 'example.com'). DNS resolution happens at connect time; unresolvable hosts raise an error")],
    port: Annotated[int, Field(description="SSH port, 1–65535. Most servers listen on 22; non-standard ports are common for hardened hosts")] = 22,
    username: Annotated[Optional[str], Field(description="Login user for authentication. If omitted, falls back to SSH agent or OS default user. Required when the remote user differs from the local one")] = None,
    password: Annotated[Optional[str], Field(description="Password for password-based auth. If both password and key_filepath are provided, key is tried first, password is the fallback")] = None,
    key_filepath: Annotated[Optional[str], Field(description="Absolute path to a private key file (e.g. '/home/user/.ssh/id_rsa'). Must be readable by the server process. Preferred over password for non-interactive use")] = None,
    banner_timeout: Annotated[float, Field(description="Max seconds to capture the MOTD/welcome banner after login. If exceeded, banner returns '' (not an error). Increase for slow hosts; set to 0 to skip banner capture entirely")] = 2.0,
) -> dict:
    """
    Opens a persistent SSH connection and returns a session_id for use with
    `execute`. The connection stays open until `disconnect`. Host keys are
    auto-accepted. For local commands, call `execute` directly — no session needed.

    AUTHENTICATION: key_filepath is tried first when both key and password
    are provided; password acts as fallback. With neither, SSH agent and
    system defaults are used. The username defaults to the OS user if omitted.

    SIDE EFFECTS: Opens a TCP socket with a 30-second keepalive. Leaks the
    socket if `disconnect` is never called.

    ERRORS: Raises on authentication failure, unresolvable hostname, refused
    connection, or network timeout.

    RETURNS: {"session_id": str, "banner": str}
    - session_id: pass to `execute(session_id=...)` and `disconnect`.
    - banner: server MOTD captured during banner_timeout, or "" if none.
    """
    return await manager.connect_ssh(host, port, username, password, key_filepath, banner_timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Execute Command",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def execute(
    command: Annotated[str, Field(description="Shell command to run. Each call is stateless — `cd /foo` does NOT persist. Chain with && for multi-step (e.g. 'cd /foo && ls'), or use a Zellij/tmux session for persistent state")],
    session_id: Annotated[Optional[str], Field(description="SSH session_id from connect_ssh. Omit (or null) for local execution. Raises ValueError if the session_id is invalid or was already disconnected")] = None,
    shell: Annotated[Optional[str], Field(description="Shell for local execution (e.g. 'powershell.exe', '/bin/bash'). Only used when session_id is null — ignored for SSH. Defaults to cmd.exe on Windows, /bin/bash on Unix")] = None,
    pause_timeout: Annotated[float, Field(description="Seconds of output silence before returning. Controls how long to wait for a quiet command — raise this (not total_timeout) for slow-starting jobs. Must be > 0 and ≤ total_timeout")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on total call duration in seconds. Only binds while output is actively streaming — a silent command returns at pause_timeout, not total_timeout. Must be ≥ pause_timeout")] = 20.0,
) -> dict:
    """
    Execute a command locally or over SSH. Each call runs in an isolated
    channel — no persistent shell between calls.

    LOCAL (default): just pass `command`. A transient shell spawns and is
    torn down automatically when the command finishes.
    SSH: also pass `session_id` from `connect_ssh`.

    WHEN NOT TO USE:
    - To send input to an already-running command, use `respond` instead.
    - To send control keys (Ctrl+C, arrows) to a running command, use
      `send_control` instead.
    - To poll output from a running command, use `read_output` instead.

    SIDE EFFECTS: Spawns a subprocess (local) or opens an SSH exec channel.
    Completed commands are cleaned up automatically. Partial commands stay
    alive until finished, interrupted via send_control, or the session is
    disconnected.

    TIMEOUT INTERACTION: pause_timeout controls return time for silent
    commands; total_timeout only binds while output is actively streaming.
    For quiet long-running jobs, raise pause_timeout (not total_timeout).

    TUI apps (zellij, vim) MUST start in foreground — never use `&`.

    ERRORS: Raises ValueError on invalid session_id.

    RETURNS:
    - {"status": "completed", "output": str, "exit_code": int}
    - {"status": "partial", "output": str, "command_id": str}
      Use command_id with read_output / respond / send_control.
    """
    return await manager.execute_command(command, session_id, shell, pause_timeout, total_timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Input to Command",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def respond(
    command_id: Annotated[str, Field(description="The command_id from a status='partial' response. Must be an active (not yet completed) command. Raises ValueError if invalid or already finished")],
    text: Annotated[str, Field(description="Text to write to the command's stdin (e.g. 'y', a password, a shell command). A trailing newline is auto-appended if missing. For control keys (Ctrl+C, arrows, etc.) use send_control instead — AI frameworks strip control bytes from strings")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence before returning. Raise this (not total_timeout) when the command is slow to respond after receiving input. Must be > 0 and ≤ total_timeout")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on total call duration in seconds. Only binds while output is actively streaming. Must be ≥ pause_timeout")] = 20.0,
) -> dict:
    """
    Write text to a running command's stdin. Works for interactive prompts
    (y/n, passwords), shell input, or any text the process expects.

    For commands inside zellij/tmux, prefer the multiplexer CLI
    (e.g. `zellij action write-chars`) via `execute` — it gives cleaner
    output and doesn't depend on the original command_id staying alive.

    SIDE EFFECTS: Writes to the command's stdin. May trigger the command
    to produce output, change state, or exit.

    ERRORS: Raises ValueError if command_id is invalid or already completed.

    RETURNS: Same format as execute —
    {"status": "completed"|"partial", "output": str, ...}
    """
    return await manager.respond_to_command(command_id, text, pause_timeout, total_timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read Command Output",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
async def read_output(
    command_id: Annotated[str, Field(description="The command_id from a status='partial' response. Must be an active command. Raises ValueError if invalid or already completed")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence before returning. This is the primary dial for polling quiet jobs — raise it (e.g. 30, 60) instead of total_timeout. Must be > 0 and ≤ total_timeout")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on total call duration in seconds. Only binds while output is actively streaming — a silent poll returns at pause_timeout regardless. Must be ≥ pause_timeout")] = 20.0,
) -> dict:
    """
    Poll new output from a running command without sending input. Use after
    execute returns status="partial" for non-interactive commands (builds,
    training loops, long searches).

    Each call returns only output produced since the last read. When the
    command finishes, status="completed" is returned and the command_id
    becomes invalid — further calls raise ValueError.

    TIMEOUT INTERACTION: pause_timeout is the primary dial for polling —
    it controls how long to wait when the command is silent. Raise it
    (e.g. 30, 60) for quiet long-running jobs. total_timeout only binds
    while output is actively streaming.

    ERRORS: Raises ValueError if command_id is invalid or already completed.

    RETURNS: Same format as execute —
    {"status": "completed"|"partial", "output": str, ...}
    """
    return await manager.poll_command(command_id, pause_timeout, total_timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Control Key/Signal",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def send_control(
    command_id: Annotated[str, Field(description="The command_id from a status='partial' response. Must be an active command. Raises ValueError if invalid or already completed")],
    signal: Annotated[str, Field(description="Case-insensitive key name. Values: ctrl+a..ctrl+z, ctrl+[/]/^/_/\\, esc, tab, enter, return, space, backspace, up/down/left/right, home, end, pageup, pagedown, insert, delete, f1..f12, backtab, alt+<char>. Raises ValueError if unrecognized")] = "ctrl+c",
    pause_timeout: Annotated[float, Field(description="Seconds of output silence after sending the key before returning. Raise for slow TUI repaints (e.g. over high-latency SSH). Must be > 0 and ≤ total_timeout")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on total call duration in seconds. Only binds while output is actively streaming. Must be ≥ pause_timeout")] = 20.0,
) -> dict:
    """
    Send a control key or escape sequence to a running command. Use for
    interrupts (ctrl+c), TUI navigation (arrows, F-keys), or any
    non-printable input. Prefer this over `respond` for control keys —
    AI frameworks strip raw control bytes from string arguments.

    SIGNAL HANDLING: The signal parameter accepts any key name from the
    supported list (case-insensitive, whitespace-tolerant — "Ctrl + C"
    works). Common signals: ctrl+c (SIGINT/interrupt), ctrl+z (SIGTSTP/
    suspend), ctrl+d (EOF), ctrl+\\ (SIGQUIT). Local non-PTY subprocesses
    only react to ctrl+c, ctrl+z, ctrl+\\; SSH and PTY channels accept all.

    SIDE EFFECTS: The signal may terminate the command (e.g. ctrl+c),
    making the command_id invalid on the next read.

    TIMEOUT INTERACTION: pause_timeout controls how long to wait for
    output after the signal. Raise it for slow TUI repaints over
    high-latency SSH; total_timeout only binds during active streaming.

    ERRORS: Raises ValueError if command_id is invalid, already completed,
    or signal name is unrecognized.

    RETURNS: Same format as execute —
    {"status": "completed"|"partial", "output": str, ...}
    """
    return await manager.send_control(command_id, signal, pause_timeout, total_timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Disconnect Session",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def disconnect(
    session_id: Annotated[str, Field(description="The session_id returned by connect_ssh. After this call, the session_id becomes invalid — further execute() calls with it raise ValueError. Safe to call multiple times: disconnecting an already-closed or unknown session_id silently returns true (idempotent)")]
) -> bool:
    """
    Close an SSH session and release all associated resources. Idempotent:
    calling on an already-disconnected or unknown session_id returns true
    without error. NOT needed for local commands — those clean up automatically.

    SIDE EFFECTS: On first call — terminates all running commands on this
    session (their command_ids become invalid), closes SSH channels and the
    TCP socket, removes the session. On repeated calls — no-op.

    WHEN NOT TO USE: To stop a single command without closing the entire
    SSH session, use send_control with "ctrl+c" instead.

    RETURNS: true (always succeeds).
    """
    return await manager.disconnect(session_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
