import asyncio
import os
import sys
import signal as _signal

PTY_AVAILABLE = False
if sys.platform == "win32":
    try:
        from pywinpty import PtyProcess  # type: ignore
        PTY_AVAILABLE = True
    except ImportError:
        pass
else:
    import pty as pty_mod
    import fcntl
    PTY_AVAILABLE = True


if sys.platform == "win32":
    # CREATE_NEW_PROCESS_GROUP disables CTRL_C_EVENT for the child group;
    # CTRL_BREAK_EVENT is the only interrupt that works with that flag.
    # Windows has no SIGTSTP, so Ctrl+Z is intentionally left unmapped.
    _LOCAL_SIG_MAP = {b'\x03': _signal.CTRL_BREAK_EVENT}
else:
    _LOCAL_SIG_MAP = {b'\x03': _signal.SIGINT}
    if hasattr(_signal, 'SIGTSTP'):
        _LOCAL_SIG_MAP[b'\x1a'] = _signal.SIGTSTP
    if hasattr(_signal, 'SIGQUIT'):
        _LOCAL_SIG_MAP[b'\x1c'] = _signal.SIGQUIT


class Channel:
    """I/O interface for a running command's data stream."""

    async def read(self) -> bytes | None:
        """Return data, None (no data yet), or b"" (EOF)."""

    async def write(self, data: bytes) -> None:
        """Send data to the command's stdin."""

    async def is_finished(self) -> bool:
        """True when the remote process has exited."""

    async def get_exit_code(self) -> int | None:
        """Return the exit code (only valid after is_finished)."""

    async def close(self) -> None:
        """Release underlying resources."""

    async def send_signal(self, sig: bytes) -> None:
        """Send a control byte (e.g., b'\\x03' for Ctrl+C)."""


# ---------------------------------------------------------------------------
# SSH implementation (paramiko)
# ---------------------------------------------------------------------------

class SSHChannel(Channel):
    """Wraps a paramiko.Channel into the Channel interface."""

    def __init__(self, paramiko_channel, stdin=None):
        self._ch = paramiko_channel
        # Keep a reference to the stdin ChannelFile so Python GC does NOT call
        # its __del__ / shutdown_write() early, which would set eof_sent=True
        # on the underlying channel and prevent any future writes.
        self._stdin = stdin

    async def read(self) -> bytes | None:
        if self._ch.recv_ready():
            return self._ch.recv(4096)
        return None

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._ch.sendall, data)

    async def is_finished(self) -> bool:
        return self._ch.exit_status_ready()

    async def get_exit_code(self) -> int | None:
        return self._ch.recv_exit_status()

    async def close(self) -> None:
        try:
            self._ch.close()
        except Exception:
            pass

    async def send_signal(self, sig: bytes) -> None:
        await self.write(sig)


# ---------------------------------------------------------------------------
# Local subprocess implementation
# ---------------------------------------------------------------------------

class LocalChannel(Channel):
    """Wraps an asyncio.subprocess.Process into the Channel interface."""

    def __init__(self, process: asyncio.subprocess.Process):
        self._proc = process

    async def read(self) -> bytes | None:
        try:
            data = await asyncio.wait_for(
                self._proc.stdout.read(4096), timeout=0.05
            )
            # read() returns b"" on EOF
            return data
        except asyncio.TimeoutError:
            return None

    async def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def is_finished(self) -> bool:
        return self._proc.returncode is not None

    async def get_exit_code(self) -> int | None:
        return self._proc.returncode

    async def close(self) -> None:
        if self._proc.returncode is None:
            self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()

    async def send_signal(self, sig: bytes) -> None:
        os_sig = _LOCAL_SIG_MAP.get(sig)
        if os_sig:
            self._proc.send_signal(os_sig)


# ---------------------------------------------------------------------------
# PTY implementation (pywinpty on Windows, stdlib pty on Linux)
# ---------------------------------------------------------------------------

if PTY_AVAILABLE:
    import subprocess

    class PtyChannel(Channel):
        """PTY-backed channel. Windows: pywinpty. Linux: stdlib pty."""

        def __init__(self, command: str, shell: str):
            self._finished = False
            self._exit_code = None

            if sys.platform == "win32":
                self._pty = PtyProcess.spawn(f'{shell} /c {command}')
                self._fd = None
                self._proc = None
            else:
                master, slave = pty_mod.openpty()
                try:
                    self._proc = subprocess.Popen(
                        [shell, "-c", command],
                        stdin=slave, stdout=slave, stderr=slave,
                        preexec_fn=os.setsid,
                    )
                except Exception:
                    # Popen failed (bad shell path, ENOMEM, EPERM, ...).
                    # Without this both PTY fds would leak.
                    os.close(master)
                    os.close(slave)
                    raise
                os.close(slave)
                self._fd = master
                flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
                fcntl.fcntl(self._fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                self._pty = None

        async def read(self) -> bytes | None:
            if sys.platform == "win32":
                try:
                    data = await asyncio.to_thread(self._pty.read, 4096)
                    return data.encode() if isinstance(data, str) else data
                except EOFError:
                    self._finished = True
                    return b""
            else:
                try:
                    return os.read(self._fd, 4096)
                except BlockingIOError:
                    return None
                except OSError:
                    return b""

        async def write(self, data: bytes) -> None:
            if sys.platform == "win32":
                # bytes.decode() defaults to utf-8 regardless of locale, so
                # the normal path is fine. errors='replace' is defensive:
                # if upstream ever hands us non-utf-8 bytes, we'd rather
                # write a replacement char than crash the read loop.
                self._pty.write(data.decode('utf-8', errors='replace'))
            else:
                os.write(self._fd, data)

        async def is_finished(self) -> bool:
            if self._finished:
                return True
            if sys.platform == "win32":
                if not self._pty.isalive():
                    self._exit_code = self._pty.exitstatus
                    self._finished = True
            else:
                ret = self._proc.poll()
                if ret is not None:
                    self._exit_code = ret
                    self._finished = True
            return self._finished

        async def get_exit_code(self) -> int | None:
            return self._exit_code

        async def close(self) -> None:
            if sys.platform == "win32":
                if self._pty.isalive():
                    self._pty.close()
            else:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                if not self._finished:
                    self._proc.terminate()
                    try:
                        await asyncio.to_thread(self._proc.wait, 2.0)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        await asyncio.to_thread(self._proc.wait)

        async def send_signal(self, sig: bytes) -> None:
            await self.write(sig)
            if sys.platform == "win32" and sig == b'\x03':
                self._finished = True
