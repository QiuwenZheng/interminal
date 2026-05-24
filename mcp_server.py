from mcp.server.fastmcp import FastMCP
from typing import Optional
from session_manager import SessionManager

mcp = FastMCP(
    "Interminal",
    instructions="""\
Terminal access for AI: SSH and local shells with support for interactive
and long-running commands.

KEY PATTERNS (read before first use to save discovery loops):

1. Each `execute` call is an independent channel — there is no persistent
   shell between calls. `cd /foo` does NOT carry over; chain with &&.

2. Long-running / interactive command returns status="partial" with a
   command_id. To get more output:
     - call `read_output(command_id)` when no input is needed (polling
       a training run, build, find, etc.)
     - call `respond(command_id, text)` only when the command is waiting
       for user input
     - call `send_control(command_id, "ctrl+c")` etc. for control keys

3. NEVER background a TUI app with `&` (zellij, tmux, vim, htop). The
   shell exits immediately, the process lands in a background process
   group, and the TUI's init bails out before it can daemonize. Start
   it in the FOREGROUND — execute returns "partial", and well-behaved
   servers (zellij, tmux) will have already fork+setsid'd themselves
   into independent daemons. The partial channel can then be ignored
   or left to time out; the daemon survives.

4. For multiplexers, drive them via their OWN CLI from separate execute
   calls — don't try to puppet the TUI by simulating keystrokes:
     execute("zellij --session train action new-pane -- bash start.sh")
     execute("zellij --session train action dump-screen")
     execute("tmux send-keys -t train 'bash start.sh' Enter")
     execute("tmux capture-pane -t train -p")

5. To send control keys (Ctrl+C, arrows, F-keys, etc.) use `send_control`,
   NOT `respond`. Most AI frameworks strip control bytes from string
   arguments; `send_control`'s string-keyed enum bypasses that filter.
""",
)
manager = SessionManager()


@mcp.tool()
async def connect_ssh(
    host: str,
    port: int = 22,
    username: Optional[str] = None,
    password: Optional[str] = None,
    key_filepath: Optional[str] = None,
    banner_timeout: float = 2.0,
) -> dict:
    """
    Establishes connection to an SSH server, automatically accepting host keys.
    Returns dict with session_id and banner (MOTD/welcome message).

    Args:
    - banner_timeout: seconds to wait for MOTD after shell open (default 2.0)
    """
    return await manager.connect_ssh(host, port, username, password, key_filepath, banner_timeout)


@mcp.tool()
def create_local(shell: Optional[str] = None) -> str:
    """
    Creates a local terminal session.
    Returns a session_id for subsequent operations.

    Args:
    - shell: shell to use (default: cmd.exe on Windows, /bin/bash on Unix)
    """
    return manager.create_local(shell)


@mcp.tool()
async def execute(
    session_id: str,
    command: str,
    pause_timeout: float = 2.0,
    total_timeout: float = 30.0,
) -> dict:
    """
    Execute a command in a session (SSH or local). Always use this to run
    shell commands — there is no persistent shell between calls, so chain
    state-changing commands with && (e.g. "cd /foo && ls").

    Returns a dict:
      status="completed":  exit_code, output filled in. Command is done.
      status="partial":    command_id filled in. Command is still running.
                           Continue with one of:
                             read_output(cid) — no input needed (logs/build)
                             respond(cid, text) — command awaits input
                             send_control(cid, key) — send Ctrl+C / arrows / F-keys
                             (do nothing, let it run — fine for daemons)

    Long-running TUI apps (zellij, tmux, vim) MUST be started in foreground.
    Do NOT background them with `&` — that breaks their initialization. The
    "partial" return after a short timeout is expected and correct; the
    server has already daemonized itself and is safe to abandon.

    Args:
    - pause_timeout: max seconds of output silence before returning (default 2.0)
    - total_timeout: hard cap on this call's duration (default 30.0)
    """
    return await manager.execute_command(session_id, command, pause_timeout, total_timeout)


@mcp.tool()
async def respond(
    command_id: str,
    text: str,
    pause_timeout: float = 2.0,
    total_timeout: float = 30.0,
) -> dict:
    """
    Send text input to a command that returned status="partial" and is
    waiting at a prompt. Auto-appends \\n if missing.

    Example: respond(command_id, "y") to answer a [Y/n] prompt.

    For control keys (Ctrl+C, arrows, F-keys, ESC, etc.) use send_control
    instead — AI frameworks routinely strip control characters from string
    arguments before this function ever sees them.

    Returns the same format as execute.
    """
    return await manager.respond_to_command(command_id, text, pause_timeout, total_timeout)


@mcp.tool()
async def read_output(
    command_id: str,
    pause_timeout: float = 2.0,
    total_timeout: float = 30.0,
) -> dict:
    """
    Read new output from a running command without sending any input.
    Use this after execute returns status "partial" when the command needs no interaction
    (e.g. long-running build, training loop, find).

    Returns the same format as execute/respond.
    """
    return await manager.poll_command(command_id, pause_timeout, total_timeout)


@mcp.tool()
async def send_control(
    command_id: str,
    signal: str = "ctrl+c",
    pause_timeout: float = 2.0,
    total_timeout: float = 10.0,
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
    """
    return await manager.send_control(command_id, signal, pause_timeout, total_timeout)


@mcp.tool()
async def disconnect(session_id: str) -> bool:
    """
    Disconnects from the session and cleans up all resources.
    Works for both SSH and local sessions.
    """
    return await manager.disconnect(session_id)


@mcp.tool()
def list_sessions() -> list[dict]:
    """
    List all active sessions with their type and metadata.
    Returns a list of dicts, each containing session_id, type, and
    type-specific info (host/port for SSH, shell for local).
    """
    return manager.list_sessions()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
