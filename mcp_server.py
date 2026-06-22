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
    host: Annotated[str, Field(description="The hostname or IP address of the SSH server to connect to (e.g., '192.168.1.10' or 'example.com')")],
    port: Annotated[int, Field(description="The port number of the SSH server (default is 22)")] = 22,
    username: Annotated[Optional[str], Field(description="Optional username for authentication. If omitted, the connection will use SSH agent or system defaults")] = None,
    password: Annotated[Optional[str], Field(description="Optional password for password-based authentication. Omit if using key-based authentication")] = None,
    key_filepath: Annotated[Optional[str], Field(description="Optional absolute path to a private key file for key-based auth; must be readable by the server process. If both this and password are supplied, the key is attempted first")] = None,
    banner_timeout: Annotated[float, Field(description="The timeout in seconds to wait for the MOTD/welcome banner after the connection opens")] = 2.0,
) -> dict:
    """
    Opens a persistent SSH connection to a REMOTE host and returns a session_id.
    The connection stays open across tool calls until closed with `disconnect`;
    drive it with `execute`. Host keys are auto-accepted (no prompt).

    WHEN TO USE:
    - Use this for a shell on a remote host. For local commands, just call
      `execute` directly — no session needed.
    - Reuse an existing session_id rather than reconnecting per command.

    AUTHENTICATION (key-based recommended):
    - Password: pass `username` + `password`.
    - Key: pass `username` + `key_filepath` (absolute path). If both a password
      and a key are given, the key is tried first.

    Captures the MOTD/welcome banner for up to `banner_timeout` seconds. Raises
    on authentication failure, hostname/DNS errors, or connection timeout.

    Returns a dict:
        - session_id: identifier for this connection (pass to `execute`).
        - banner: the server's MOTD, or "" if none arrived before banner_timeout.
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
    command: Annotated[str, Field(description="The shell command to execute. Each call is stateless; for persistent state (cd, venv, env vars), drive a Zellij session instead of chaining &&")],
    session_id: Annotated[Optional[str], Field(description="SSH session_id returned by connect_ssh. Omit for local commands — no session needed")] = None,
    shell: Annotated[Optional[str], Field(description="Shell for local execution (e.g. 'powershell.exe', '/bin/bash'). Ignored when session_id is provided. Defaults to cmd.exe on Windows, /bin/bash on Unix")] = None,
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning a partial response (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Execute a command locally or over SSH. Each call runs in an isolated
    channel — there is NO persistent shell between calls.

    LOCAL (default): just pass `command`. No session needed — a transient
    shell is created and torn down automatically.

    SSH: pass `command` + `session_id` from `connect_ssh`.

    For persistent state (cd, venv, env vars), start a Zellij/tmux session
    and drive it via CLI instead of chaining &&.

    Returns a dict:
      status="completed":  exit_code, output filled in. Command is done.
      status="partial":    command_id filled in. Command is still running.
                           Continue with one of:
                             read_output(cid) — no input needed (logs/build)
                             respond(cid, text) — command awaits input
                             send_control(cid, key) — send Ctrl+C / arrows / F-keys
                             (do nothing, let it run — fine for daemons)

    Long-running TUI apps (zellij, vim) MUST be started in foreground. Do NOT
    background them with `&` — it breaks their init. The "partial" return after
    a short timeout is expected: the server has already daemonized and is safe
    to abandon.

    Args:
    - pause_timeout: seconds of OUTPUT SILENCE before returning (default 9.0).
      Dominates return time for silent commands — raise this (not
      total_timeout) when polling a quiet long-running job.
    - total_timeout: hard cap on this call's duration (default 20.0). Only
      binds while output is actively streaming.
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
    command_id: Annotated[str, Field(description="The active command_id returned in a partial status response that is waiting for input")],
    text: Annotated[str, Field(description="The text input to send to the command (e.g. 'y' for prompts, passwords, etc.). Newline is auto-appended")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Send text input to any running command (status="partial"). This is the
    general-purpose "write to stdin" tool — works for interactive prompts
    (y/n, passwords), shell commands, or any text the process expects.
    Auto-appends \\n if missing.

    NOTE ON MULTIPLEXERS: For commands running inside zellij/tmux, prefer
    the multiplexer's own CLI (e.g. `zellij action write-chars`) via
    `execute` instead of `respond`. Reasons:
      1. Output quality: `respond` returns the TUI's raw screen rendering
         (borders, status bar, ANSI redraws), not clean command output.
      2. Coupling: `respond` requires the original partial command_id to
         stay alive; the CLI approach is stateless — the daemon survives
         independently.

    For control keys (Ctrl+C, arrows, F-keys, ESC, etc.) use send_control
    instead — AI frameworks routinely strip control characters from string
    arguments before this function ever sees them.

    Returns the same format as execute.

    Args:
    - pause_timeout: seconds of OUTPUT SILENCE before returning (default 9.0).
      Raise this (not total_timeout) when the response is expected to take
      a long time to start producing output.
    - total_timeout: hard cap on call duration (default 20.0). Only binds
      while output is actively streaming.
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
    command_id: Annotated[str, Field(description="The active command_id returned in a partial status response")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Read new output from a running command without sending any input.
    Use this after execute returns status "partial" when the command needs no
    interaction (e.g. long-running build, training loop, find).

    Returns the same dict as execute:
      status="partial":    output + command_id; poll again for more.
      status="completed":  output + exit_code. The command_id is now spent —
                           it is closed and removed, so a later read_output on
                           it raises "Invalid command_id".
    Each call returns only the output produced since the previous call.

    Args:
    - pause_timeout: seconds of OUTPUT SILENCE before returning (default 9.0).
      This is the dial that controls how long a silent-poll call waits.
      Raise it (e.g. 30, 60) when polling a very quiet job; raising
      total_timeout instead does nothing while the process stays silent.
    - total_timeout: hard cap on call duration (default 20.0). Only binds
      while output is actively streaming.
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
    command_id: Annotated[str, Field(description="The active command_id returned in a partial status response")],
    signal: Annotated[str, Field(description="The control signal or key to send. Supported values: 'ctrl+c', 'ctrl+z', 'ctrl+d', arrow keys, enter, f1-f12, etc.")] = "ctrl+c",
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Send a control key/signal to a running command. Use this whenever a
    raw control byte or escape sequence is needed — interrupting a stuck
    command OR driving a TUI (zellij, vim, less, htop, etc.). Prefer this
    over `respond` for non-printable input: many AI frameworks strip
    control characters from MCP string arguments, but the string-keyed
    enum here is always safe.

    Signal names are case-insensitive. Supported values:

      ctrl+a .. ctrl+z              0x01..0x1A (e.g. ctrl+o for zellij detach)
      ctrl+\\, ctrl+], ctrl+^, ctrl+_, ctrl+[
      esc, tab, enter, return, space, backspace, bs
      up, down, left, right
      home, end, pageup, pagedown, insert, delete, del
      backtab / shift+tab
      f1 .. f12
      alt+<char>                    ESC + char (bash readline, emacs)

    Local non-PTY subprocesses only react to ctrl+c, ctrl+z, ctrl+\\
    (mapped to SIGINT/SIGTSTP/SIGQUIT). SSH and PTY channels accept all
    of the above as raw bytes / escape sequences.

    Returns the same format as execute/respond.

    Args:
    - pause_timeout: seconds of output silence after sending the key
      before returning (default 9.0). Raise this if the key is expected
      to trigger slow output (e.g. a TUI repaint over high-latency SSH).
    - total_timeout: hard cap on call duration (default 20.0). Only binds
      while output is actively streaming.
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
    session_id: Annotated[str, Field(description="The SSH session_id returned by connect_ssh that you want to close")]
) -> bool:
    """
    Gracefully disconnects an SSH session and cleans up all associated resources.
    NOT needed for local commands — those are transient and clean up automatically.

    BEHAVIOR:
    - Terminates any running commands associated with this SSH session.
    - Closes SSH channels and network sockets.
    - Removes the session from the active session manager.

    USAGE GUIDELINES:
    - Call this when finished with an SSH session to prevent resource leaks.
    - Do NOT call if you intend to run more commands in this session later.
    - ALTERNATIVES:
      * To stop a single running command, use `send_control` with "ctrl+c".
    """
    return await manager.disconnect(session_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
