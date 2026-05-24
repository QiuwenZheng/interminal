import asyncio
import codecs
import logging

from channel import Channel

logger = logging.getLogger("interminal.command")


class RunningCommand:
    """Manages one running command with background output buffering."""

    def __init__(self, channel: Channel, session_id: str):
        self.channel = channel
        self.session_id = session_id

        self.buffer = ""
        self.running = True
        self.exit_code = None
        self.new_data_event = asyncio.Event()
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        while self.running:
            try:
                raw = await self.channel.read()
                has_data = False

                if raw is not None and raw != b"":
                    self.buffer += self.decoder.decode(raw)
                    has_data = True
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
                break

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
        self.read_task.cancel()
        await self.channel.close()
