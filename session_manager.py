import asyncio
import logging
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Optional

_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|[0-?]|\[[0-?]*[ -/]*[@-~]|[ -/][@-~])')

import paramiko

from channel import SSHChannel, LocalChannel, PTY_AVAILABLE
if PTY_AVAILABLE:
    from channel import PtyChannel
from command import RunningCommand

_CTRL_FLAGS = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}

# Key-name -> raw byte sequence map for send_control.
#
# The string-keyed API is necessary because upstream AI frameworks often strip
# raw control characters from MCP tool `text` arguments — so the only way to
# inject a literal 0x01..0x1F byte (or an escape sequence) is via a printable
# enum string that we translate on the server side.
#
# Coverage:
#   - Ctrl+A..Ctrl+Z                  (0x01..0x1A)
#   - Ctrl+\, Ctrl+], Ctrl+^, Ctrl+_  (0x1C..0x1F)
#   - Named singletons (esc, tab, enter, backspace, space)
#   - Navigation keys (arrows, home/end/pageup/pagedown/insert/delete)
#   - Function keys F1..F12
#   - Shift+Tab (backtab)
#   - Alt+<char> is handled dynamically in _resolve_signal (ESC + char).
#
# Lookup is case-insensitive (see _resolve_signal).
_SIGNAL_MAP = {f"ctrl+{chr(ord('a') + i)}": bytes([i + 1]) for i in range(26)}
_SIGNAL_MAP.update({
    # Other Ctrl+ combinations
    "ctrl+\\": b'\x1c',
    "ctrl+]":  b'\x1d',
    "ctrl+^":  b'\x1e',
    "ctrl+_":  b'\x1f',
    "ctrl+[":  b'\x1b',  # same byte as esc

    # Named singletons
    "esc":       b'\x1b',
    "tab":       b'\x09',
    "enter":     b'\x0d',
    "return":    b'\x0d',
    "space":     b'\x20',
    # xterm convention: Backspace sends DEL (0x7f), the Delete key sends an
    # escape sequence. Older terminals reverse this — we follow xterm because
    # it matches what paramiko negotiates on most servers.
    "backspace": b'\x7f',
    "bs":        b'\x7f',

    # Arrow keys (xterm CSI)
    "up":    b'\x1b[A',
    "down":  b'\x1b[B',
    "right": b'\x1b[C',
    "left":  b'\x1b[D',

    # Navigation
    "home":     b'\x1b[H',
    "end":      b'\x1b[F',
    "pageup":   b'\x1b[5~',
    "pagedown": b'\x1b[6~',
    "insert":   b'\x1b[2~',
    "delete":   b'\x1b[3~',
    "del":      b'\x1b[3~',
    "backtab":  b'\x1b[Z',  # Shift+Tab
    "shift+tab": b'\x1b[Z',

    # Function keys (xterm SS3 for F1-F4, CSI for F5+)
    "f1":  b'\x1bOP',
    "f2":  b'\x1bOQ',
    "f3":  b'\x1bOR',
    "f4":  b'\x1bOS',
    "f5":  b'\x1b[15~',
    "f6":  b'\x1b[17~',
    "f7":  b'\x1b[18~',
    "f8":  b'\x1b[19~',
    "f9":  b'\x1b[20~',
    "f10": b'\x1b[21~',
    "f11": b'\x1b[23~',
    "f12": b'\x1b[24~',
})


def _resolve_signal(signal: str) -> Optional[bytes]:
    """Resolve a key name to raw bytes. Case- and whitespace-insensitive.

    Falls through to dynamic parsing for Alt+<char> (sent as ESC + char,
    matching bash readline / emacs conventions).
    """
    # Normalize: lowercase + strip internal whitespace so "Ctrl + C" works.
    sig = "".join(signal.lower().split())
    if sig in _SIGNAL_MAP:
        return _SIGNAL_MAP[sig]
    # Alt+<single char> -> ESC + char
    if sig.startswith("alt+") and len(sig) == 5:
        return b'\x1b' + sig[4].encode('utf-8')
    return None

PYTE_AVAILABLE = False
try:
    import pyte

    class _Screen(pyte.Screen):
        """pyte.Screen with a permissive report_device_status.

        Some pyte versions pass `private=True` from the Stream but the Screen
        method doesn't accept it, raising TypeError on CSI ? Ps n sequences
        (e.g. those emitted by zellij).  In headless mode we never respond to
        DSR anyway, so the method is a safe no-op regardless of arguments.
        """
        def report_device_status(self, *args, **kwargs):
            pass

    PYTE_AVAILABLE = True
except ImportError:
    pass

logger = logging.getLogger("interminal.manager")


@dataclass
class Session:
    type: str                          # "ssh" or "local"
    client: Any = None                 # paramiko.SSHClient for SSH, None for local
    shell: Optional[str] = None        # shell path for local sessions
    host: Optional[str] = None         # SSH host (for display)
    port: Optional[int] = None         # SSH port (for display)


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.commands: dict[str, RunningCommand] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def connect_ssh(
        self,
        host: str,
        port: int = 22,
        username: Optional[str] = None,
        password: Optional[str] = None,
        key_filepath: Optional[str] = None,
        banner_timeout: float = 2.0,
    ) -> dict:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = {"hostname": host, "port": port, "username": username}
        if password is not None:
            kwargs["password"] = password
        if key_filepath is not None:
            kwargs["key_filename"] = key_filepath

        await asyncio.to_thread(client.connect, **kwargs)

        client.get_transport().set_keepalive(30)

        banner = await self._capture_ssh_banner(client, banner_timeout)

        session_id = str(uuid.uuid4())
        self.sessions[session_id] = Session(
            type="ssh", client=client, host=host, port=port,
        )
        return {"session_id": session_id, "banner": banner}

    def create_local(self, shell: Optional[str] = None) -> str:
        if shell is None:
            shell = "cmd.exe" if sys.platform == "win32" else "/bin/bash"

        session_id = str(uuid.uuid4())
        self.sessions[session_id] = Session(
            type="local", shell=shell,
        )
        return session_id

    async def _capture_ssh_banner(self, client: paramiko.SSHClient, banner_timeout: float) -> str:
        ch = await asyncio.to_thread(client.invoke_shell)
        ch.settimeout(0.0)
        await asyncio.sleep(banner_timeout)
        chunks = []
        while ch.recv_ready():
            chunks.append(ch.recv(4096))
        ch.close()
        raw = b"".join(chunks).replace(b'\x00', b'').decode("utf-8", errors="replace").strip()
        return _ANSI_ESCAPE.sub('', raw)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute_command(
        self,
        session_id: str,
        command: str,
        pause_timeout: float = 2.0,
        total_timeout: float = 30.0,
    ) -> dict:
        if session_id not in self.sessions:
            raise ValueError("Invalid session_id")

        session = self.sessions[session_id]

        if session.type == "ssh":
            stdin, stdout, _ = await asyncio.to_thread(
                session.client.exec_command, command, get_pty=True
            )
            # Pass stdin so SSHChannel holds a reference and GC won't call
            # stdin.__del__() -> shutdown_write() -> eof_sent=True prematurely.
            channel = SSHChannel(stdout.channel, stdin=stdin)

        elif session.type == "local":
            if PTY_AVAILABLE:
                channel = PtyChannel(command, session.shell)
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **_CTRL_FLAGS,
                )
                channel = LocalChannel(process)

        else:
            raise ValueError(f"Unknown session type: {session.type}")

        command_id = str(uuid.uuid4())
        self.commands[command_id] = RunningCommand(channel, session_id)
        return await self._wait_for_result(command_id, pause_timeout, total_timeout)

    async def respond_to_command(
        self,
        command_id: str,
        text: str,
        pause_timeout: float = 2.0,
        total_timeout: float = 30.0,
    ) -> dict:
        if command_id not in self.commands:
            raise ValueError("Invalid command_id")
        if not text.endswith('\n'):
            text += '\n'
        await self.commands[command_id].write_input(text)
        return await self._wait_for_result(command_id, pause_timeout, total_timeout)

    async def poll_command(
        self,
        command_id: str,
        pause_timeout: float = 2.0,
        total_timeout: float = 30.0,
    ) -> dict:
        if command_id not in self.commands:
            raise ValueError("Invalid command_id")
        return await self._wait_for_result(command_id, pause_timeout, total_timeout)

    async def send_control(
        self,
        command_id: str,
        signal: str,
        pause_timeout: float = 2.0,
        total_timeout: float = 10.0,
    ) -> dict:
        if command_id not in self.commands:
            raise ValueError("Invalid command_id")
        sig = _resolve_signal(signal)
        if sig is None:
            raise ValueError(f"Unknown signal: {signal!r}")
        await self.commands[command_id].channel.send_signal(sig)
        return await self._wait_for_result(command_id, pause_timeout, total_timeout)

    # ------------------------------------------------------------------
    # Core wait logic
    # ------------------------------------------------------------------

    async def _wait_for_result(
        self,
        command_id: str,
        pause_timeout: float = 2.0,
        total_timeout: float = 30.0,
    ) -> dict:
        cmd = self.commands[command_id]
        start = asyncio.get_running_loop().time()

        # Phase 1: wait for initial data to arrive
        cmd.new_data_event.clear()
        initial_wait = min(pause_timeout + 1.0, total_timeout)
        try:
            await asyncio.wait_for(cmd.new_data_event.wait(), timeout=initial_wait)
        except asyncio.TimeoutError:
            pass

        # Phase 2: keep collecting until pause or total timeout
        while cmd.running:
            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= total_timeout:
                break

            cmd.new_data_event.clear()
            try:
                await asyncio.wait_for(cmd.new_data_event.wait(), timeout=pause_timeout)
            except asyncio.TimeoutError:
                break

        # Collect all buffered output and check status
        output = cmd.read_output()
        if PYTE_AVAILABLE and output:
            output = self._render_pyte(output)
        is_finished, exit_code = cmd.check_status()

        if is_finished:
            await cmd.close()
            del self.commands[command_id]
            return {
                "status": "completed",
                "output": output,
                "exit_code": exit_code,
            }

        return {
            "status": "partial",
            "output": output,
            "command_id": command_id,
        }

    def _render_pyte(self, raw_text: str) -> str:
        screen = _Screen(200, max(len(raw_text.splitlines()) + 10, 50))
        stream = pyte.Stream(screen)
        stream.feed(raw_text)
        return "\n".join(line.rstrip() for line in screen.display).strip()

    # ------------------------------------------------------------------
    # Session teardown
    # ------------------------------------------------------------------

    async def disconnect(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            raise ValueError("Invalid session_id")

        session = self.sessions[session_id]
        del self.sessions[session_id]

        # Close commands first (while transport is still open for clean channel close)
        dead = [cid for cid, cmd in self.commands.items()
                if cmd.session_id == session_id]
        for cid in dead:
            await self.commands[cid].close()
            del self.commands[cid]

        # Then close the underlying client
        if session.type == "ssh" and session.client is not None:
            session.client.close()

        return True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        result = []
        for sid, session in self.sessions.items():
            info = {"session_id": sid, "type": session.type}
            if session.type == "ssh":
                info["host"] = session.host
                info["port"] = session.port
            elif session.type == "local":
                info["shell"] = session.shell
            result.append(info)
        return result
