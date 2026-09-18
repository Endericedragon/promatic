import asyncio as aio
from abc import abstractmethod, ABC
from dataclasses import dataclass
from typing import override

from consts import CONN_ESTABLISHED, CONN_PROXY_TEMPLATE
from io_utils import read_headers, safe_close


@dataclass(slots=True)
class ProxyRequest:
    proto: "Proto"
    host: str
    port: int
    header_bytes: bytearray


class Proto(ABC):
    """策略基类，规定在隧道建立、和通信开始前的一系列动作。"""

    port: int
    log_symbol: str

    @abstractmethod
    async def setup_proxy_tunnel(
        self,
        proxy_reader: aio.StreamReader,
        proxy_writer: aio.StreamWriter,
        req: ProxyRequest,
    ): ...

    @abstractmethod
    async def prepare_communication(
        self,
        remote_writer: aio.StreamWriter,
        client_writer: aio.StreamWriter,
        req: ProxyRequest,
    ): ...


class HttpProto(Proto):
    port = 80
    log_symbol = "H"

    @override
    async def setup_proxy_tunnel(
        self,
        proxy_reader: aio.StreamReader,
        proxy_writer: aio.StreamWriter,
        req: ProxyRequest,
    ):
        """HTTP请求，无需代理隧道"""
        return True

    @override
    async def prepare_communication(
        self,
        remote_writer: aio.StreamWriter,
        client_writer: aio.StreamWriter,
        req: ProxyRequest,
    ):
        """HTTP隧道建立，向远端转发HTTP请求头"""
        remote_writer.write(req.header_bytes)
        await remote_writer.drain()


class HttpsProto(Proto):
    port = 443
    log_symbol = "S"

    @override
    async def setup_proxy_tunnel(
        self,
        proxy_reader: aio.StreamReader,
        proxy_writer: aio.StreamWriter,
        req: ProxyRequest,
    ) -> bool:
        """HTTPS请求，需要和远端发送CONNECT请求"""
        try:
            # 2.1 构造代理请求
            PROXY_REQUEST = CONN_PROXY_TEMPLATE.format(req.host, req.port)
            proxy_writer.write(PROXY_REQUEST.encode("latin1"))
            await proxy_writer.drain()
            # 2.2 看看代理返回了啥，若包含200则成功
            result = await read_headers(proxy_reader)
            return bool(result) and (b"200" in result)
        except Exception:  # 走直连和代理都不行，标记为分支节点
            await safe_close(proxy_writer)
            return False

    @override
    async def prepare_communication(
        self,
        remote_writer: aio.StreamWriter,
        client_writer: aio.StreamWriter,
        req: ProxyRequest,
    ):
        """HTTPS隧道建立，向客户端回复CONN_ESTABLISHED"""
        client_writer.write(CONN_ESTABLISHED.encode("latin1"))
        await client_writer.drain()


HTTP_INSTANCE = HttpProto()
HTTPS_INSTANCE = HttpsProto()
