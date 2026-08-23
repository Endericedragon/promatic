import asyncio as aio
import signal
from sys import platform as current_platform
from urllib.parse import urlparse

from domain_trie import DomainTrie, NodeStatus, load_memo, write_memo
from log_utils import get_logger
from io_utils import (
    bidirectional_pipe,
    read_headers,
    safe_close,
    try_open_direct_connection,
)

from consts import (
    get_port,
    get_backend_port,
    MAX_PROXY_TIMEOUT,
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
            target_reader, target_writer = await try_open_direct_connection(host, port)
            # 先不急着标记为直连，等首包通信成功后再标记
        except (aio.TimeoutError, OSError) as e:
            # 直连失败，记录日志并切换为代理
            LOGGER.warning(f"[✅HErr] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.PROXY)
            use_proxy = True
    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", get_backend_port()),
                timeout=MAX_PROXY_TIMEOUT,
            )
        except Exception as e:
            LOGGER.error(f"[🚀HErr] {type(e).__name__} {host}:{port}")
            return
    # 3. 开始通信
    try:
        # 3.1 把后端传过来的头部直接丢给目标
        target_writer.write(header_bytes)
        await target_writer.drain()

        # 3.2 然后让用户和目标直接双向通信
        def mark_as():  # 当返回首包时，可以准确标记域名为直连还是代理了
            global LOGGER, TRIE
            nonlocal use_proxy
            if use_proxy:
                LOGGER.info("[🚀H] {}:{}".format(host, port))
            else:
                LOGGER.info("[✅H] {}:{}".format(host, port))
                # 首包通信成功，才能放心将其标记为直连
                TRIE.insert(host, NodeStatus.DIRECT)

        await bidirectional_pipe(
            reader,
            writer,
            target_reader,
            target_writer,
            read_server_through_proxy=True,
            on_recv_first_remote_data=mark_as,
        )
        # 3.3 通信成功
    except Exception as e:  # 只会被FakeDirectError触发
        LOGGER.warning(f"[{'🚀' if use_proxy else '✅'}HErr2] {e} {host}:{port}")
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
            target_reader, target_writer = await try_open_direct_connection(host, port)
            # 先不急着标记为直连，等首包通信成功后再标记
        except (aio.TimeoutError, OSError) as e:
            # 记录日志并切换为代理
            LOGGER.warning(f"[✅HSErr] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.PROXY)
            use_proxy = True

    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", get_backend_port()),
                timeout=MAX_PROXY_TIMEOUT,
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
            LOGGER.error(f"[🚀HSErr] {type(e).__name__} {host}:{port}")
            return
    # 3. 开始通信
    try:
        # 3.1 回头告诉用户，代理连接已建立
        writer.write(CONN_ESABLISHED.encode("latin1"))
        await writer.drain()

        # 3.2 然后让用户和目标直接双向通信
        def mark_as():  # 当返回首包时，可以准确标记域名为直连还是代理了
            global LOGGER, TRIE
            nonlocal use_proxy
            if use_proxy:
                LOGGER.info("[🚀HS] {}:{}".format(host, port))
                # 已经在前面将域名记录为代理
            else:
                LOGGER.info("[✅HS] {}:{}".format(host, port))
                # 首包通信成功，才能放心将其标记为直连
                TRIE.insert(host, NodeStatus.DIRECT)

        await bidirectional_pipe(
            reader,
            writer,
            target_reader,
            target_writer,
            read_server_through_proxy=True,
            on_recv_first_remote_data=mark_as,
        )
        # 3.3 通信成功
    except Exception as e:  # 只会被FakeDirectError触发
        LOGGER.warning(f"[{'🚀' if use_proxy else '✅'}HSErr2] {e} {host}:{port}")
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


async def set_signals_and_run(stop_event: aio.Event):
    """设置信号处理函数并运行异步循环。"""
    loop = aio.get_running_loop()

    def __handle_signal():
        nonlocal stop_event
        LOGGER.info("Signal received!")
        stop_event.set()

    if current_platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: __handle_signal())
        signal.signal(signal.SIGTERM, lambda *_: __handle_signal())
    else:
        loop.add_signal_handler(signal.SIGINT, __handle_signal)
        loop.add_signal_handler(signal.SIGTERM, __handle_signal)

    await main_task(stop_event)


async def main_task(stop_event: aio.Event):
    proxy_server = await aio.start_server(start_proxy_server, "127.0.0.1", get_port())
    LOGGER.info("Proxy server started on 127.0.0.1:{}".format(get_port()))
    async with proxy_server:
        await stop_event.wait()
        LOGGER.info("Stopping proxy server...")
        proxy_server.close()
        await proxy_server.wait_closed()


if __name__ == "__main__":
    stop_event = aio.Event()
    load_memo(TRIE)
    try:
        aio.run(set_signals_and_run(stop_event))
    except KeyboardInterrupt as e:
        pass
    except Exception as e:
        LOGGER.error("Unknown error:", type(e).__name__)
    finally:
        LOGGER.info("Shutting down proxy server...")
        write_memo(TRIE)
