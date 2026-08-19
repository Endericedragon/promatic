import asyncio as aio
import logging
from urllib.parse import urlparse

from domain_trie import DomainTrie, NodeStatus, load_memo, write_memo
from rw_utils import pipe, read_headers

WRITE_LOCK: aio.Lock = aio.Lock()
PORT: int = 33333
BACKEND_PROXY_PORT: int = 32001
TIMEOUT: float = 6.0
CONN_ESABLISHED: str = "HTTP/1.1 200 Connection Established\r\n\r\n"
CONN_PROXY_TEMPLATE: str = "CONNECT {0}:{1} HTTP/1.1\r\nHost: {0}:{1}\r\n\r\n"
TRIE: DomainTrie = DomainTrie()
LOGGER = logging.getLogger(__name__)


async def handle_http(
    reader: aio.StreamReader,
    writer: aio.StreamWriter,
    host: str,
    port: int | None,
    header_bytes: bytearray,
):
    """处理HTTP请求

    先尝试直连服务器，若超时则换成代理访问。
    若port为None，则自动使用80端口。
    """
    port = port or 80
    if TRIE.search(host) != NodeStatus.PROXY:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection(host, port), timeout=TIMEOUT
            )
            LOGGER.info("[Direct] {}:{}".format(host, port))
            assert host is not None
            TRIE.insert(host, NodeStatus.DIRECT)
            # 把后端传过来的头部直接丢给目标
            target_writer.write(header_bytes)
            await target_writer.drain()
            # 然后让用户和目标直接双向通信
            await aio.gather(pipe(target_reader, writer), pipe(reader, target_writer))
            return
        except aio.TimeoutError:
            LOGGER.error("[Timeout] {}:{}".format(host, port))
        except Exception as e:
            LOGGER.error("[HTTP Direct {}:{}] {}: {}".format(host, port, type(e), e))
    # 超时后，换成代理访问
    try:
        proxy_reader, proxy_writer = await aio.open_connection(
            "127.0.0.1", BACKEND_PROXY_PORT
        )
        LOGGER.info("[Proxy] {}:{}".format(host, port))
        assert host is not None
        TRIE.insert(host, NodeStatus.PROXY)
        proxy_writer.write(header_bytes)
        await proxy_writer.drain()
        # 然后让用户和目标直接双向通信
        await aio.gather(pipe(proxy_reader, writer), pipe(reader, proxy_writer))
    except Exception as e:
        LOGGER.error("[HTTP Proxy {}:{}] {}: {}".format(host, port, type(e), e))
    finally:
        writer.close()


async def handle_https(
    reader: aio.StreamReader,
    writer: aio.StreamWriter,
    host: str,
    port: int | None,
):
    """处理HTTPS请求

    先尝试直连服务器，若超时则换成代理访问
    """
    if TRIE.search(host) != NodeStatus.PROXY:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection(host, port), timeout=TIMEOUT
            )
            LOGGER.info("[Direct] {}:{}".format(host, port))
            assert host is not None
            TRIE.insert(host, NodeStatus.DIRECT)
            # 回头告诉用户，代理连接已建立
            writer.write(CONN_ESABLISHED.encode("latin1"))
            await writer.drain()
            # 然后让用户和目标双向传输去
            await aio.gather(pipe(target_reader, writer), pipe(reader, target_writer))
            return
        except aio.TimeoutError:
            LOGGER.error("[Timeout] {}:{}".format(host, port))
        except Exception as e:
            LOGGER.error("[HTTPS Direct {}:{}] {}: {}".format(host, port, type(e), e))
    try:
        proxy_reader, proxy_writer = await aio.open_connection(
            "127.0.0.1", BACKEND_PROXY_PORT
        )
        LOGGER.info("[Proxy] {}:{}".format(host, port))
        assert host is not None
        TRIE.insert(host, NodeStatus.PROXY)
        # 构造代理请求
        PROXY_REQUEST = CONN_PROXY_TEMPLATE.format(host, port)
        proxy_writer.write(PROXY_REQUEST.encode("latin1"))
        await proxy_writer.drain()
        # 看看代理返回了啥
        result = await read_headers(proxy_reader)
        if result and b"200" in result:
            # 回头告诉用户，代理连接已建立
            writer.write(CONN_ESABLISHED.encode("latin1"))
            await writer.drain()
            # 然后让用户和目标双向传输去
            await aio.gather(pipe(proxy_reader, writer), pipe(reader, proxy_writer))
        else:
            writer.write(result)
            await writer.drain()
    except Exception as e:
        LOGGER.error("[HTTPS Proxy {}:{}] {}: {}".format(host, port, type(e), e))
    finally:
        writer.close()


async def start_proxy_server(reader: aio.StreamReader, writer: aio.StreamWriter):
    """处理代理请求

    先获取其请求头，判断请求是HTTP请求还是HTTPS请求，然后交由对应函数处理。
    """
    header_bytes = await read_headers(reader)
    if not header_bytes:
        writer.close()
        return
    header = header_bytes.decode("latin1")
    parts = header.splitlines()[0].split(" ", 2)
    if len(parts) != 3:
        LOGGER.error("[Header Parse] Invalid request line: {}".format(header))
        writer.close()
        return
    method, path, _ = parts
    match method:
        case "GET":
            # HTTP
            parsed = urlparse(path)
            assert parsed.hostname is not None
            await handle_http(
                reader, writer, parsed.hostname, parsed.port, header_bytes
            )
        case "CONNECT":
            # HTTPS
            host, port_str = path.split(":", 1)
            port = int(port_str)
            await handle_https(reader, writer, host, port)
        case _:
            assert False, "Unreachable!"


async def main():
    load_memo(TRIE)
    task = await aio.start_server(start_proxy_server, "127.0.0.1", PORT)
    LOGGER.info("Proxy server started on 127.0.0.1:{}".format(PORT))
    async with task:
        await task.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        aio.run(main())
    except (KeyboardInterrupt, Exception) as e:
        LOGGER.error(type(e))
    finally:
        LOGGER.info("Shutting down proxy server...")
        write_memo(TRIE)
