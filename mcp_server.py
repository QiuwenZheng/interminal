from mcp.server.fastmcp import FastMCP
from typing import Optional, Annotated
from pydantic import Field
from session_manager import SessionManager

mcp = FastMCP(
    "Interminal",
    instructions="""\
Terminal access for AI: SSH and local shells with support for interactive,
long-running, and stateful tasks.

KEY PATTERNS (read before first use to save discovery loops):

1. SINGLE EXECUTE IS STATELESS:
   Each `execute` call runs in an isolated channel. `cd /foo` does NOT
   persist to the next call.
   - Simple 1-2 step tasks: chain with && (e.g. "cd /foo && ls").
   - Multi-step workflows needing persistent state (cd, env vars, venv,
     long-running processes): start a Zellij session and drive it via CLI
     — see patterns 2-4 below.

2. PERSISTENT SHELL via Zellij:
   Start a Zellij session for any stateful, ongoing workspace. The daemon
   keeps the shell alive on the host; the user can `zellij attach <name>`
   to observe your work in real-time or provide input (sudo, credentials).
   NEVER background with `&` — start in the FOREGROUND. `execute` returns
   "partial" once the daemon is up; ignore/abandon this partial channel.
   Use a deterministic session name based on the project or task (e.g.
   "myproject-dev") so you can resume after reconnection.
   If Zellij is not available, tmux is a viable alternative.

3. DRIVING ZELLIJ (Avoid Pane Proliferation):
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

4. INTERACTIVE & LONG-RUNNING COMMANDS (without multiplexer):
   Commands that produce ongoing output return status="partial" with a
   command_id. To continue:
     - read_output(command_id) — poll for logs, build output, etc.
     - respond(command_id, text) — answer a prompt (y/n, password).
     - send_control(command_id, signal) — send Ctrl+C, arrows, F-keys.
       Do NOT write control bytes into `respond`; AI frameworks strip them.

""",
)
manager = SessionManager()


@mcp.tool()
async def connect_ssh(
    host: Annotated[str, Field(description="The hostname or IP address of the SSH server to connect to (e.g., '192.168.1.10' or 'example.com')")],
    port: Annotated[int, Field(description="The port number of the SSH server (default is 22)")] = 22,
    username: Annotated[Optional[str], Field(description="Optional username for authentication. If omitted, the connection will use SSH agent or system defaults")] = None,
    password: Annotated[Optional[str], Field(description="Optional password for password-based authentication. If using key-based authentication, this can be omitted")] = None,
    key_filepath: Annotated[Optional[str], Field(description="Optional absolute path to a private key file (SSH key) for key-based authentication")] = None,
    banner_timeout: Annotated[float, Field(description="The timeout in seconds to wait for the MOTD/welcome banner after the connection opens")] = 2.0,
) -> dict:
    """
    Establishes a persistent, stateful connection to a remote SSH server, automatically accepting host keys.
    This session remains active across tool calls, allowing multiple execute commands to run sequentially.

    CONNECTION LIFECYCLE:
    - Initiates connection and performs handshake.
    - Waits up to `banner_timeout` seconds to capture the MOTD/welcome banner.
    - Returns a persistent session_id representing this connection.
    - The connection remains open until explicitly closed using the `disconnect` tool.

    AUTHENTICATION METHODS:
    - Username & Password: Use 'username' and 'password' arguments.
    - Key-based (Recommended): Provide 'username' and 'key_filepath' (absolute path to private key).
    - If both password and private key are provided, key-based authentication is attempted first.

    DEFAULT BEHAVIORS:
    - Automatically trusts and accepts SSH host keys (no prompt).
    - Starts a remote shell session ready to execute commands.

    ERROR HANDLING:
    - Raises exceptions for authentication failures, hostname resolution issues, or connection timeouts.
    - Ensure paths provided in 'key_filepath' are absolute and readable by the server process.

    Returns:
        A dictionary containing:
            - session_id: Unique identifier for the created SSH session (use this for execute commands).
            - banner: The server's welcome message/MOTD, or an empty string if timeout.
    """
    return await manager.connect_ssh(host, port, username, password, key_filepath, banner_timeout)


@mcp.tool()
def create_local(
    shell: Annotated[Optional[str], Field(description="Optional absolute path or executable name of the shell to use (e.g., 'powershell.exe', '/bin/bash', '/bin/zsh'). If omitted, defaults to cmd.exe on Windows or /bin/bash on Unix/macOS.")] = None
) -> str:
    """
    Creates a persistent local terminal session (PTY on supported platforms).
    Starts a local shell process that remains active across tool calls.

    BEHAVIOR & SIDE EFFECTS:
    - Spawns a shell process running locally on the server host machine.
    - Commands run in this shell execute with the permissions of the user running the MCP server process.
    - Side effects include full read/write file access and execution privileges on the host system.

    SAFETY PROFILE:
    - This tool gives the client access to the local machine's shell. Use with caution.
    - No automatic rate limits or destructive filters are applied. Ensure commands executed are safe.

    Returns:
        A unique session_id to identify and drive the created local shell session.
    """
    return manager.create_local(shell)


@mcp.tool()
async def execute(
    session_id: Annotated[str, Field(description="The unique session identifier returned by connect_ssh or create_local")],
    command: Annotated[str, Field(description="The shell command to execute. Each call is stateless; for persistent state (cd, venv, env vars), drive a Zellij session instead of chaining &&")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning a partial response (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Execute a command in a session (SSH or local). Each call runs in an
    isolated channel — there is no persistent shell between calls.

    For simple tasks, chain with && (e.g. "cd /foo && ls").
    For multi-step workflows needing persistent state (cd, venv, env vars),
    start a Zellij session and drive it via CLI instead of chaining &&.

    Returns a dict:
      status="completed":  exit_code, output filled in. Command is done.
      status="partial":    command_id filled in. Command is still running.
                           Continue with one of:
                             read_output(cid) — no input needed (logs/build)
                             respond(cid, text) — command awaits input
                             send_control(cid, key) — send Ctrl+C / arrows / F-keys
                             (do nothing, let it run — fine for daemons)

    Long-running TUI apps (zellij, vim) MUST be started in foreground.
    Do NOT background them with `&` — that breaks their initialization. The
    "partial" return after a short timeout is expected and correct; the
    server has already daemonized itself and is safe to abandon.

    Args:
    - pause_timeout: seconds of OUTPUT SILENCE before returning (default 9.0).
      Dominates return time for silent commands — raise this (not
      total_timeout) when polling a quiet long-running job.
    - total_timeout: hard cap on this call's duration (default 20.0). Only
      binds while output is actively streaming.
    """
    return await manager.execute_command(session_id, command, pause_timeout, total_timeout)


@mcp.tool()
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


@mcp.tool()
async def read_output(
    command_id: Annotated[str, Field(description="The active command_id returned in a partial status response")],
    pause_timeout: Annotated[float, Field(description="Seconds of output silence to wait before returning (default is 9.0)")] = 9.0,
    total_timeout: Annotated[float, Field(description="Hard cap in seconds on the maximum duration of this call (default is 20.0)")] = 20.0,
) -> dict:
    """
    Read new output from a running command without sending any input.
    Use this after execute returns status "partial" when the command needs no interaction
    (e.g. long-running build, training loop, find).

    Returns the same format as execute/respond.

    Args:
    - pause_timeout: seconds of OUTPUT SILENCE before returning (default 9.0).
      This is the dial that controls how long a silent-poll call waits.
      Raise it (e.g. 30, 60) when polling a very quiet job; raising
      total_timeout instead does nothing while the process stays silent.
    - total_timeout: hard cap on call duration (default 20.0). Only binds
      while output is actively streaming.
    """
    return await manager.poll_command(command_id, pause_timeout, total_timeout)


@mcp.tool()
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


@mcp.tool()
async def disconnect(
    session_id: Annotated[str, Field(description="The unique session identifier returned by connect_ssh or create_local that you want to close")]
) -> bool:
    """
    Gracefully disconnects from an active terminal session (SSH or local) and cleans up all associated resources.

    BEHAVIOR:
    - Terminates any running background commands and subprocesses associated with this session.
    - Closes open PTY channels, SSH channels, and network sockets to release system resources.
    - Removes the session from the active session manager.

    USAGE GUIDELINES:
    - ALWAYS call this tool when you are finished executing commands on a session to prevent resource leaks (dangling processes/sockets).
    - Do NOT call this tool if you intend to run more commands in this session later.
    - ALTERNATIVES:
      * To see all active sessions before disconnecting, use `list_sessions`.
      * To stop a single running command inside the session without closing the entire connection, use `send_control` with "ctrl+c" instead of `disconnect`.
    """
    return await manager.disconnect(session_id)


@mcp.tool()
def list_sessions() -> list[dict]:
    """
    List all currently active terminal sessions (SSH and local) managed by this server.

    USAGE GUIDELINES:
    - Use this tool to discover existing sessions, check connection statuses, and retrieve active session_ids.
    - Recommended prerequisite: Call this tool before executing commands if you need to resume or verify an existing session.
    - Do NOT use this tool if you already know the session_id and just want to run commands directly.

    ALTERNATIVES:
    - If you want to create a new session instead of listing existing ones, use create_local or connect_ssh.
    - If you want to stop/close an active session, use disconnect.

    Returns:
        A list of dictionaries, each containing:
            - session_id: Unique identifier of the session.
            - type: Either 'ssh' or 'local'.
            - host (SSH only): The remote hostname connected to.
            - port (SSH only): The remote port connected to.
            - shell (local only): The shell executable running.
    """
    return manager.list_sessions()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
