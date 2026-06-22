import asyncio
import codecs
import logging

from channel import Channel

logger = logging.getLogger("interminal.command")


class RunningCommand:
    """Manages one running command with background output buffering."""

    def __init__(self, channel: Channel, command: str, session_id: str | None = None):
        self.channel = channel
        self.command = command
        self.session_id = session_id

        self.buffer = ""
        self.running = True
        self.exit_code = None
        self.new_data_event = asyncio.Event()
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        # Wall-clock of the last byte arrival. Lets _wait_for_result compute
        # "already silent for X seconds" across calls so a poll on a quiet
        # stream returns without re-waiting the full pause_timeout.
        # Initialized to now so the first Phase 2 entry without prior data
        # doesn't short-circuit.
        self.last_data_time = asyncio.get_running_loop().time()
        self.read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        # The finally block matters: CancelledError isn't an Exception subclass
        # (it's BaseException), so a cancel on this task skips the except below.
        # Without finally, running=False + event.set() would never run on
        # cancel, leaving any waiter on new_data_event stuck until its own
        # pause_timeout fires.
        try:
            while self.running:
                try:
                    raw = await self.channel.read()
                    has_data = False

                    if raw is not None and raw != b"":
                        self.buffer += self.decoder.decode(raw)
                        has_data = True
                        self.last_data_time = asyncio.get_running_loop().time()
                        self.new_data_event.set()

                    is_eof = (raw == b"")

                    if is_eof or await self.channel.is_finished():
                        self.exit_code = await self.channel.get_exit_code()
                        # 排空 read() 和 is_finished() 之间可能积累的数据（SSH 必需）
                        while True:
                            remaining = await self.channel.read()
                            if remaining is None or remaining == b"":
                                break
                            self.buffer += self.decoder.decode(remaining)
                        tail = self.decoder.decode(b'', final=True)
                        if tail:
                            self.buffer += tail
                        break
                    elif not has_data:
                        await asyncio.sleep(0.05)
                except Exception as e:
                    logger.warning("_read_loop error: %s", e)
                    # Best-effort exit code so the caller doesn't see
                    # status="completed" + exit_code=None for an abnormal exit.
                    try:
                        self.exit_code = await self.channel.get_exit_code()
                    except Exception:
                        pass
                    break
        finally:
            self.running = False
            self.new_data_event.set()

    def read_output(self) -> str:
        data = self.buffer
        self.buffer = ""
        return data

    async def write_input(self, text: str) -> None:
        await self.channel.write(text.encode('utf-8'))

    def check_status(self) -> tuple[bool, int | None]:
        return not self.running, self.exit_code

    async def close(self):
        self.running = False
        # Wake any waiter on new_data_event immediately — the read_task's
        # finally will set it too, but doing it here means waiters see
        # running=False without having to wait for the task to actually
        # tear down.
        self.new_data_event.set()
        # Close the channel first so any blocking read (notably Windows PTY,
        # which sits in asyncio.to_thread(pty.read) and can't be cancelled)
        # unblocks. Only then await the read task — otherwise the to_thread
        # future will hold us until the thread itself exits, which it can't
        # do while the PTY is still open.
        await self.channel.close()
        self.read_task.cancel()
        try:
            await self.read_task
        except (asyncio.CancelledError, Exception):
            pass
