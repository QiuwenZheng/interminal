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
    Local: just pass command (defaults to cmd.exe/bash).
    SSH: also pass session_id from connect_ssh.

    PARAMETER GUIDANCE: session_id selects SSH vs local; shell only
    applies to local. Chain with && for multi-step; use Zellij/tmux
    for persistent state. Raise pause_timeout (not total_timeout) for
    quiet jobs. Raises ValueError if session_id is invalid.

    WHEN NOT TO USE: respond (stdin), send_control (keys), read_output (poll).

    SIDE EFFECTS: Spawns a process. Partial commands live until finished
    or interrupted. TUI apps MUST start in foreground — never use &.

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

    WHEN NOT TO USE: Inside zellij/tmux, prefer multiplexer CLI via execute.

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
    pause_timeout: Annotated[float, Field(description="Seconds of silence after sending the key before returning (> 0, ≤ total_timeout)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap on call duration in seconds (≥ pause_timeout)")] = 20.0,
) -> dict:
    """
    Send a control key or escape sequence to a running command. Prefer
    this over respond — AI frameworks strip control bytes from strings.
    For printable text (y/n, passwords), use respond instead.

    SIDE EFFECTS: ctrl+c sends SIGINT (may terminate the command,
    invalidating command_id). ctrl+z suspends (SIGTSTP). ctrl+d sends EOF.

    PARAMETER GUIDANCE: signal names are whitespace-tolerant ("Ctrl + C"
    works). Common values: ctrl+c (interrupt), ctrl+z (suspend). Effectiveness
    varies by channel: local non-PTY only reacts to ctrl+c/z/\\, SSH/PTY
    accept all. Raise pause_timeout for slow TUI repaints; total_timeout
    only caps streaming. Raises ValueError on bad command_id or signal.

    RETURNS:
    - {"status": "partial", "output": str, "command_id": str}
    - {"status": "completed", "output": str, "exit_code": int}
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
