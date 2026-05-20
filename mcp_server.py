from mcp.server.fastmcp import FastMCP
from typing import Optional
from session_manager import SessionManager

mcp = FastMCP("Interminal")
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
    Execute a command in a session (SSH or local). ALWAYS use this to run commands.

    Returns a dict with:
    - status: "completed" or "partial"
    - output: command output so far
    - exit_code: only when status is "completed"
    - command_id: only when status is "partial", use with respond

    When status is "partial", check the output text to decide what to do:
    - If output ends with a prompt like [Y/n], call respond to answer it.
    - If output is progress logs (no input needed), call read_output to collect more.
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
    Respond to a command that has not yet completed.
    Use this after execute returns status "partial".

    Example: respond(command_id, "y\\n") to answer a [Y/n] prompt.

    Returns the same format as execute. If the command asks another question,
    status will be "partial" again - call respond once more.
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
    Send a control signal to a running command. Use when a command is stuck or needs interrupting.

    Args:
    - signal: "ctrl+c" (interrupt), "ctrl+z" (suspend), "ctrl+\\" (quit)

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
