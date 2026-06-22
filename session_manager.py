import asyncio
import logging
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Optional

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
    client: paramiko.SSHClient
    host: str
    port: int


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

        # paramiko's connect() accepts None for password / key_filename, so the
        # conditional kwargs build is unnecessary.
        await asyncio.to_thread(
            client.connect,
            hostname=host, port=port, username=username,
            password=password, key_filename=key_filepath,
        )

        # Once connect() succeeds, the SSH socket is live. If banner capture
        # or any setup below fails, we must close the client ourselves —
        # otherwise the socket leaks (it isn't tracked in self.sessions yet).
        try:
            client.get_transport().set_keepalive(30)
            banner = await self._capture_ssh_banner(client, banner_timeout)
        except BaseException:
            # BaseException, not Exception: _capture_ssh_banner awaits
            # asyncio.sleep, so a CancelledError mid-banner would otherwise
            # skip past `except Exception` and leak the SSH socket.
            client.close()
            raise

        session_id = str(uuid.uuid4())
        self.sessions[session_id] = Session(
            client=client, host=host, port=port,
        )
        return {"session_id": session_id, "banner": banner}

    async def _capture_ssh_banner(self, client: paramiko.SSHClient, banner_timeout: float) -> str:
        ch = await asyncio.to_thread(client.invoke_shell)
        ch.settimeout(0.0)
        try:
            await asyncio.sleep(banner_timeout)
            chunks = []
            while ch.recv_ready():
                chunks.append(ch.recv(4096))
        finally:
            # try/finally so a cancel during the sleep still closes ch.
            ch.close()
        raw = b"".join(chunks).replace(b'\x00', b'').decode("utf-8", errors="replace").strip()
        return _ANSI_ESCAPE.sub('', raw)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute_command(
        self,
        command: str,
        session_id: Optional[str] = None,
        shell: Optional[str] = None,
        pause_timeout: float = 9.0,
        total_timeout: float = 20.0,
    ) -> dict:
        if session_id is not None:
            if session_id not in self.sessions:
                raise ValueError("Invalid session_id")
            session = self.sessions[session_id]
            def _open_ssh_channel(client, cmd):
                ch = client.get_transport().open_session()
                ch.get_pty(term='xterm-256color', width=500, height=200)
                ch.exec_command(cmd)
                return ch
            ch = await asyncio.to_thread(_open_ssh_channel, session.client, command)
            channel = SSHChannel(ch)
        else:
            if shell is None:
                shell = "cmd.exe" if sys.platform == "win32" else "/bin/bash"
            if PTY_AVAILABLE:
                channel = PtyChannel(command, shell)
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **_CTRL_FLAGS,
                )
                channel = LocalChannel(process)

        command_id = str(uuid.uuid4())
        self.commands[command_id] = RunningCommand(channel, command, session_id)
        return await self._wait_for_result(
            command_id, pause_timeout, total_timeout, is_first_exec=True,
        )

    async def respond_to_command(
        self,
        command_id: str,
        text: str,
        pause_timeout: float = 9.0,
        total_timeout: float = 20.0,
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
        pause_timeout: float = 9.0,
        total_timeout: float = 20.0,
    ) -> dict:
        if command_id not in self.commands:
            raise ValueError("Invalid command_id")
        return await self._wait_for_result(command_id, pause_timeout, total_timeout)

    async def send_control(
        self,
        command_id: str,
        signal: str,
        pause_timeout: float = 9.0,
        total_timeout: float = 20.0,
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
        pause_timeout: float = 9.0,
        total_timeout: float = 20.0,
        is_first_exec: bool = False,
    ) -> dict:
        cmd = self.commands[command_id]
        start = asyncio.get_running_loop().time()
        first_byte_timed_out = False

        try:
            # Phase 1: wait for the first byte to arrive.
            # Skip if buffer already has data (background output between
            # calls) or the command has already finished — otherwise the
            # clear() below would drop the "data arrived" signal and we'd
            # waste pause_timeout+1s for nothing.
            if not cmd.buffer and cmd.running:
                cmd.new_data_event.clear()
                initial_wait = min(pause_timeout + 1.0, total_timeout)
                try:
                    await asyncio.wait_for(cmd.new_data_event.wait(), timeout=initial_wait)
                except asyncio.TimeoutError:
                    # Phase 1 timing out means we already waited
                    # pause_timeout+1 seconds with zero bytes — that
                    # already satisfies the "silent for pause_timeout"
                    # contract. Skip Phase 2 to avoid waiting another
                    # full pause_timeout for no reason.
                    first_byte_timed_out = True

            # Phase 2: keep collecting until pause or total timeout.
            # remaining_pause uses cmd.last_data_time so a poll on a stream
            # that's already been quiet for a while returns near-instantly
            # instead of waiting another full pause_timeout.
            while cmd.running and not first_byte_timed_out:
                now = asyncio.get_running_loop().time()
                if now - start >= total_timeout:
                    break

                remaining_pause = pause_timeout - (now - cmd.last_data_time)
                if remaining_pause <= 0:
                    break

                cmd.new_data_event.clear()
                try:
                    await asyncio.wait_for(cmd.new_data_event.wait(), timeout=remaining_pause)
                except asyncio.TimeoutError:
                    break
        except BaseException:
            # If a brand-new execute is cancelled mid-wait, the caller never
            # received command_id and can't clean it up. Drop the orphan
            # here so the background read loop doesn't run forever.
            # Later poll / respond / send_control cancellations leave the
            # command alive so it can keep running in the background.
            if is_first_exec:
                self.commands.pop(command_id, None)
                try:
                    await cmd.close()
                except Exception:
                    pass
            raise

        # check_status BEFORE read_output: _read_loop's drain + tail-decode
        # both run before the finally block sets running=False, so once
        # check_status sees not-running, the buffer is the final value.
        # Doing read_output first leaves a race where _read_loop's last
        # write lands between the read and the status check, then we
        # report status=completed and pop the command — losing the tail.
        is_finished, exit_code = cmd.check_status()
        output = cmd.read_output()
        # Only run the pyte virtual-terminal pass when the output actually
        # contains ANSI escape sequences. Plain text would otherwise be
        # wrapped at 200 columns by the pyte screen.
        if PYTE_AVAILABLE and output and "\x1b" in output:
            output = self._render_pyte(output)

        if is_finished:
            await cmd.close()
            self.commands.pop(command_id, None)
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

        session = self.sessions.pop(session_id)

        # Close commands first (while transport is still open for clean
        # channel close). Wrap in try/finally so a failing cmd.close()
        # doesn't leak the underlying SSH client.
        try:
            dead = [cid for cid, cmd in self.commands.items()
                    if cmd.session_id == session_id]
            for cid in dead:
                try:
                    await self.commands[cid].close()
                except Exception as e:
                    logger.warning("Error closing command %s: %s", cid, e)
                finally:
                    self.commands.pop(cid, None)
        finally:
            session.client.close()

        return True

