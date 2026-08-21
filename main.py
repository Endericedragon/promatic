import asyncio as aio
import signal
from sys import platform as current_platform
from urllib.parse import urlparse

from domain_trie import DomainTrie, NodeStatus, load_memo, write_memo
from log_utils import get_logger
from io_utils import bidirectional_pipe, read_headers, safe_close, try_open_connection

from consts import (
    PORT,
    BACKEND_PROXY_PORT,
    MAX_TIMEOUT,
    CONN_ESABLISHED,
    CONN_PROXY_TEMPLATE,
)

TRIE: DomainTrie = DomainTrie()
LOGGER = get_logger()


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
    use_proxy = TRIE.search(host) == NodeStatus.PROXY
    # 1. 先尝试直连服务器
    if not use_proxy:
        try:
            target_reader, target_writer = await try_open_connection(host, port)
        except (aio.TimeoutError, OSError) as e:
            # 最后一次重试失败，记录日志并切换为代理
            LOGGER.warning(f"[DHErr] {type(e).__name__} {host}:{port}")
            use_proxy = True
    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", BACKEND_PROXY_PORT),
                timeout=MAX_TIMEOUT,
            )
        except Exception as e:
            LOGGER.warning(f"[PHErr] {type(e).__name__} {host}:{port}")
            return
    # 3. 开始通信
    try:
        # 3.1 把后端传过来的头部直接丢给目标
        target_writer.write(header_bytes)
        await target_writer.drain()
        # 3.2 然后让用户和目标直接双向通信
        await bidirectional_pipe(reader, writer, target_reader, target_writer)
        # 3.3 通信成功
        if use_proxy:
            # 将域名记录为代理
            LOGGER.info("[PH] {}:{}".format(host, port))
            TRIE.insert(host, NodeStatus.PROXY)
        else:
            # 将域名记录为直连
            LOGGER.info("[DH] {}:{}".format(host, port))
            TRIE.insert(host, NodeStatus.DIRECT)
    except Exception as e:  # 只会被FakeDirectError触发
        LOGGER.warning(f"[{'P' if use_proxy else 'D'}HErr] {e} {host}:{port}")
        if not use_proxy:
            # 3.3 如果命中直连规则但无法成功的，记为代理
            TRIE.insert(host, NodeStatus.PROXY)


async def handle_https(
    reader: aio.StreamReader,
    writer: aio.StreamWriter,
    host: str,
    port: int | None,
):
    """处理HTTPS请求

    先尝试直连服务器，若超时则换成代理访问
    """
    port = port or 443
    use_proxy = TRIE.search(host) == NodeStatus.PROXY
    # 1. 先尝试直连服务器
    if not use_proxy:
        try:
            target_reader, target_writer = await try_open_connection(host, port)
            # 先不急着将域名记录为直连
        except (aio.TimeoutError, OSError) as e:
            # 最后一次重试失败，记录日志并切换为代理
            LOGGER.warning(f"[DHSErr] {type(e).__name__} {host}:{port}")
            use_proxy = True

    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.open_connection(
                "127.0.0.1", BACKEND_PROXY_PORT
            )
            # 2.1 构造代理请求
            PROXY_REQUEST = CONN_PROXY_TEMPLATE.format(host, port)
            target_writer.write(PROXY_REQUEST.encode("latin1"))
            await target_writer.drain()
            # 2.2 看看代理返回了啥，若包含200则成功
            result = await read_headers(target_reader)
            if not result or b"200" not in result:
                raise Exception()  # 2.2.1 强制跳转到except
        except Exception as e:
            LOGGER.warning(f"[PHSErr] {type(e).__name__} {host}:{port}")
            return
    # 3. 开始通信
    try:
        # 3.1 回头告诉用户，代理连接已建立
        writer.write(CONN_ESABLISHED.encode("latin1"))
        await writer.drain()
        # 3.2 然后让用户和目标直接双向通信
        await bidirectional_pipe(reader, writer, target_reader, target_writer)
        # 3.3 通信成功
        if use_proxy:
            LOGGER.info("[PHS] {}:{}".format(host, port))
            TRIE.insert(host, NodeStatus.PROXY)
        else:
            LOGGER.info("[DHS] {}:{}".format(host, port))
            TRIE.insert(host, NodeStatus.DIRECT)
    except Exception as e:  # 只会被FakeDirectError触发
        LOGGER.warning(f"[{'P' if use_proxy else 'D'}HSErr] {e} {host}:{port}")
        if not use_proxy:
            # 3.4 如果命中直连规则但无法成功的，记为代理
            TRIE.insert(host, NodeStatus.PROXY)


async def start_proxy_server(reader: aio.StreamReader, writer: aio.StreamWriter):
    """处理代理请求

    先获取其请求头，判断请求是HTTP请求还是HTTPS请求，然后交由对应函数处理。
    """
    try:
        header_bytes = await read_headers(reader)
        if not header_bytes:
            return
        header = header_bytes.decode("latin1")
        parts = header.splitlines()[0].split(" ", 2)
        if len(parts) != 3:
            LOGGER.error(
                "[Header Parse Failed] Invalid request line: {}".format(header)
            )
            return
        method, path, _ = parts
        match method:
            case "CONNECT":
                # HTTPS
                host, port_str = path.split(":", 1)
                port = int(port_str)
                await handle_https(reader, writer, host, port)
            case _:
                # HTTP请求，如GET、POST等
                parsed = urlparse(path)
                assert parsed.hostname is not None
                await handle_http(
                    reader, writer, parsed.hostname, parsed.port, header_bytes
                )
    except Exception as e:
        LOGGER.error(f"[ServerErr] {type(e).__name__}: {e}")
    finally:
        await safe_close(writer)


async def main():
    stop_event = aio.Event()
    loop = aio.get_running_loop()

    def __handle_signal():
        nonlocal stop_event
        LOGGER.info("Signal received, shutting down proxy server...")
        stop_event.set()

    if current_platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: __handle_signal())
        signal.signal(signal.SIGTERM, lambda *_: __handle_signal())
    else:
        loop.add_signal_handler(signal.SIGINT, __handle_signal)
        loop.add_signal_handler(signal.SIGTERM, __handle_signal)

    proxy_server = await aio.start_server(start_proxy_server, "127.0.0.1", PORT)
    LOGGER.info("Proxy server started on 127.0.0.1:{}".format(PORT))
    async with proxy_server:
        await stop_event.wait()
        LOGGER.info("Stopping proxy server...")
        proxy_server.close()
        await proxy_server.wait_closed()


if __name__ == "__main__":
    try:
        load_memo(TRIE)
        aio.run(main())
    except KeyboardInterrupt as e:
        pass
    except Exception as e:
        LOGGER.error("Unknown error:", type(e).__name__)
    finally:
        LOGGER.info("Shutting down proxy server...")
        write_memo(TRIE)
