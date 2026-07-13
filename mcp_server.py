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
     long-running processes): use a terminal multiplexer like zellij.

2. SSH COMMANDS — CONNECT FIRST:
   connect_ssh(host, ...) returns a session_id. Pass it to execute().
   The connection stays open until disconnect(session_id).

3. INTERACTIVE & LONG-RUNNING COMMANDS:
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
    host: Annotated[str, Field(description="Hostname or IP of the SSH server (e.g. '192.168.1.10', 'example.com')")],
    port: Annotated[int, Field(description="SSH port number, 1–65535")] = 22,
    username: Annotated[Optional[str], Field(description="Login user for authentication; omit to use SSH agent or OS default")] = None,
    password: Annotated[Optional[str], Field(description="Password for password-based auth; omit for key-based auth")] = None,
    key_filepath: Annotated[Optional[str], Field(description="Absolute path to a private key file (e.g. '/home/user/.ssh/id_rsa')")] = None,
    banner_timeout: Annotated[float, Field(description="Max seconds to capture the MOTD/welcome banner after login")] = 2.0,
) -> dict:
    """
    Opens a persistent SSH connection and returns a session_id for use with
    `execute`. The connection stays open until `disconnect`. Host keys are
    auto-accepted. For local commands, call `execute` directly — no session needed.

    PARAMETER GUIDANCE: Reuse session_id across multiple execute calls —
    each connect_ssh opens a new TCP connection. key_filepath is tried
    first when both key and password are provided; password acts as
    fallback. With neither, SSH agent and system defaults are used. The
    username defaults to the OS user if omitted. host is resolved at
    connect time; unresolvable names raise an error. banner_timeout
    controls MOTD capture — if exceeded, banner returns "" (not an error);
    set to 0 to skip capture entirely.

    SIDE EFFECTS: Opens a TCP socket with a 30-second keepalive. Leaks
    the socket if `disconnect` is never called.

    ERRORS: Raises on auth failure, unresolvable host, refused connection,
    or network timeout.

    RETURNS: {"session_id": str, "banner": str}
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
    command: Annotated[str, Field(description="Shell command to run (stateless — cd does not persist between calls)")],
    session_id: Annotated[Optional[str], Field(description="SSH session_id from connect_ssh; omit for local execution")] = None,
    shell: Annotated[Optional[str], Field(description="Shell for local execution (e.g. 'powershell.exe', '/bin/bash'); ignored for SSH")] = None,
    pause_timeout: Annotated[float, Field(description="Seconds of silence before returning a partial result (> 0, ≤ total_timeout)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on call duration in seconds (≥ pause_timeout)")] = 20.0,
) -> dict:
    """
    Execute a command locally or over SSH in an isolated channel.

    PARAMETER RELATIONSHIPS & VALIDATION:
    - `session_id` vs `shell`: `session_id` determines the execution environment. If provided (must be a valid active session), it runs over SSH and `shell` is completely ignored. If omitted, it runs locally, and `shell` (e.g., 'powershell.exe', '/bin/bash') is used.
    - `pause_timeout` vs `total_timeout`: These interact to manage execution time. `pause_timeout` (must be > 0) triggers an early return if the command goes silent for that many seconds. `total_timeout` (must be >= `pause_timeout`) sets a hard wall-clock limit even if output is constantly streaming. To wait longer for a quiet command (e.g., a build), increase `pause_timeout`.
    - Both timeouts accept floats but invalid ranges (e.g., pause_timeout <= 0, or total_timeout < pause_timeout) or an unknown `session_id` will raise a ValueError.

    WHEN NOT TO USE: 
    Do not use this to send input to an existing command (`respond`), send control keys (`send_control`), or poll a running command (`read_output`).

    SIDE EFFECTS: 
    Spawns a new, stateless process. `cd` or environment variables do NOT persist between calls. For persistent state, start a terminal multiplexer in the foreground. Never use `&` to background TUI apps.

    RETURNS:
    - {"status": "completed", "output": str, "exit_code": int}
    - {"status": "partial", "output": str, "command_id": str}
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
    command_id: Annotated[str, Field(description="The command_id from a status='partial' response. Raises ValueError if invalid or already completed")],
    text: Annotated[str, Field(description="Text to write to stdin (e.g. 'y', a password, a shell command)")],
    pause_timeout: Annotated[float, Field(description="Seconds of silence before returning (> 0, ≤ total_timeout)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on call duration in seconds (≥ pause_timeout)")] = 20.0,
) -> dict:
    """
    Write text to a running command's stdin. Works for prompts (y/n,
    passwords), shell input, or any text the process expects.

    PARAMETER GUIDANCE: text auto-appends \\n if missing. For control
    keys use send_control — AI frameworks strip control bytes. Raise
    pause_timeout (not total_timeout) for slow responses after input.
    Raises ValueError if command_id is invalid or completed.

    WHEN NOT TO USE: Inside zellij, prefer multiplexer CLI via execute.

    SIDE EFFECTS: Writes to stdin; may trigger output, state change, or exit.

    RETURNS:
    - {"status": "partial", "output": str, "command_id": str}
    - {"status": "completed", "output": str, "exit_code": int}
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
    command_id: Annotated[str, Field(description="The command_id from a status='partial' response. Raises ValueError if invalid or already completed")],
    pause_timeout: Annotated[float, Field(description="Seconds of silence before returning. Primary dial for polling — raise it (e.g. 30, 60) for quiet jobs instead of total_timeout. Must be > 0 and ≤ total_timeout")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on call duration in seconds. Only binds while output is streaming — silent polls return at pause_timeout. Must be ≥ pause_timeout")] = 20.0,
) -> dict:
    """
    Poll new output from a running command without sending input. Use after
    execute returns status="partial" for non-interactive commands (builds,
    training loops, long searches).

    Each call returns only output produced since the last read. When the
    command finishes, status changes to "completed" and the command_id is
    retired — further calls raise ValueError.

    WHEN NOT TO USE: If the command expects input, use respond. If you
    need to interrupt or send keys, use send_control.

    PARAMETER GUIDANCE: pause_timeout is the primary dial — it controls
    how long to wait when the command is silent. total_timeout only caps
    actively streaming output and has no effect during silence.

    ERRORS: Raises ValueError if command_id is invalid or already completed.

    RETURNS:
    - {"status": "partial", "output": str, "command_id": str}
    - {"status": "completed", "output": str, "exit_code": int}
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
    signal: Annotated[str, Field(description="Case-insensitive key name. Values: ctrl+a..ctrl+z, ctrl+[/]/^/_/\\, esc, tab, enter, return, space, backspace, up/down/left/right, home, end, pageup, pagedown, insert, delete, f1..f12, backtab, alt+<char>. Raises ValueError if unrecognized")],
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
    session_id: Annotated[str, Field(description="SSH session identifier obtained from connect_ssh")]
) -> bool:
    """
    Close an SSH session and release all associated resources. NOT needed
    for local commands — those clean up automatically.

    SESSION_ID LIFECYCLE: The id is an opaque UUID created by connect_ssh,
    used with execute, and retired by this call. Each connect_ssh produces
    a unique id — reconnecting the same host gives a new one. After
    disconnect, execute() with the old id raises ValueError. Calling
    disconnect on an already-closed or unknown id is a safe no-op
    (idempotent), so cleanup logic never needs to guard against double-close.

    SIDE EFFECTS: Terminates all running commands on this session (their
    command_ids become invalid), closes SSH channels and TCP socket.

    WHEN NOT TO USE: To stop a single command without closing the session,
    use send_control with "ctrl+c" instead.

    RETURNS: true (always succeeds).
    """
    return await manager.disconnect(session_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
