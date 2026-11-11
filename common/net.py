import socket
import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

Addr = tuple[str, int]

class UdpEndpoint(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, Addr], None], *, loss: float = 0.0, delay_ms: float = 0.0, reorder: float = 0.0, seed: Optional[int] = None):
        self.on_datagram = on_datagram
        self.transport = None
        self.address = None
        self.closed = False

    async def open(self, host: str = "0.0.0.0", port: int = 0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, port))
        sock.setblocking(False)

        try:
            loop = asyncio.get_running_loop()
            self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
            self.address = self.transport.get_extra_info('sockname')[:2]
        except Exception:
            sock.close()
            raise

    def send(self, data: bytes, addr: Addr) -> None:
        if self.transport is None:
            raise RuntimeError("Cannot send on a closed endpoint.")
        try:
            self.transport.sendto(data, addr)
        except OSError as e:
            logger.debug(f"Error sending data to {addr}: {e}")

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        if self.closed:
            return
        try:
            self.on_datagram(data, addr)
        except Exception as e:
            logger.exception(f"Error in on_datagram callback for data from {addr}: {e}")

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc:
            logger.debug(f"Connection lost with error: {exc}")
        self.transport = None
        self.closed = True

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.closed = True