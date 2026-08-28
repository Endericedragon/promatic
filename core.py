import asyncio as aio
from urllib.parse import urlparse

from consts import (
    CONN_ESABLISHED,
    CONN_PROXY_TEMPLATE,
    MAX_DIRECT_TIMEOUT,
    MAX_PROXY_TIMEOUT,
    TRIE,
    get_backend_port,
    get_port,
)
from domain_trie import NodeStatus
from io_utils import (
    bidirectional_pipe,
    read_headers,
    safe_close,
)
from log_utils import get_logger

LOGGER = get_logger()


async def handle_conn_unified(
    reader: aio.StreamReader,
    writer: aio.StreamWriter,
    host: str,
    port: int | None,
    header_bytes: bytearray | None,
):
    """统一处理HTTP和HTTPS连接。
    先尝试直连服务器，若超时则换成代理访问。
    若port为None，则自动使用80端口。"""
    is_https = header_bytes is None
    port = port or (443 if is_https else 80)
    trie_search_result = TRIE.search(host)
    log_icon = repr(trie_search_result) + "S" if is_https else "H"  # 用于日志图标
    use_proxy = trie_search_result in {NodeStatus.PROXY, NodeStatus.FORCE_PROXY}
    has_record = trie_search_result != NodeStatus.BRANCH

    # 1. 先尝试直连服务器
    if not use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection(host, port), timeout=MAX_DIRECT_TIMEOUT
            )
            # 先不急着标记为直连，等首包通信成功后再标记
        except (aio.TimeoutError, OSError) as e:
            # 直连失败，记录日志并切换为代理
            LOGGER.warning(f"[{log_icon}Err-TryDirect] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.PROXY)
            log_icon = repr(NodeStatus.PROXY) + "S" if is_https else "H"
            use_proxy = True
    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", get_backend_port()),
                timeout=MAX_PROXY_TIMEOUT,
            )
        except Exception as e:
            LOGGER.error(f"[{log_icon}Err-TryProxy] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
            return
        if is_https:
            # 2.1 若是HTTPS请求，则还需要和远端发送CONNECT请求
            try:
                # 2.1 构造代理请求
                PROXY_REQUEST = CONN_PROXY_TEMPLATE.format(host, port)
                target_writer.write(PROXY_REQUEST.encode("latin1"))
                await target_writer.drain()
                # 2.2 看看代理返回了啥，若包含200则成功
                result = await read_headers(target_reader)
                if not result or b"200" not in result:
                    raise Exception()  # 2.2.1 强制跳转到except
            except Exception as e:
                LOGGER.error(f"[{log_icon}Err-TryHTTPSConn] {type(e).__name__} {host}:{port}")
                TRIE.insert(
                    host, NodeStatus.BRANCH
                )  # 走直连和代理都不行，标记为分支节点
                await safe_close(target_writer)
                return
    # 3. 开始通信
    try:
        # 3.1 若是HTTPS，则告诉用户，代理连接已建立；否则转发请求头即可
        writer.write(
            CONN_ESABLISHED.encode("latin1") if header_bytes is None else header_bytes
        )
        await writer.drain()

        # 3.2 然后让用户和目标直接双向通信
        def mark_as():  # 当返回首包时，可以准确标记域名为直连还是代理了
            global LOGGER, TRIE
            nonlocal use_proxy
            msg = f"[{log_icon}] {host}:{port}"
            if has_record:
                LOGGER.debug(msg)
            else:
                LOGGER.info(msg)
            if not use_proxy:
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
        LOGGER.warning(f"[{log_icon}Err-TryTransfer] {e} {host}:{port}")
        if not use_proxy:
            # 3.4 如果命中直连规则但无法成功的，记为代理
            TRIE.insert(host, NodeStatus.PROXY)
        else:
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
    finally:
        await safe_close(target_writer)


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
                await handle_conn_unified(reader, writer, host, port, None)
            case _:
                # HTTP请求，如GET、POST等
                parsed = urlparse(path)
                assert parsed.hostname is not None
                await handle_conn_unified(
                    reader, writer, parsed.hostname, parsed.port, header_bytes
                )
    except Exception as e:
        LOGGER.error(f"[ServerErr] {type(e).__name__}: {e}")
    finally:
        await safe_close(writer)


async def autosave_trie(interval_sec: int = 60):
    while True:
        try:
            await aio.sleep(interval_sec)
            if TRIE.safely_save_memo():
                LOGGER.debug("[AutoSave] Rules saved successfully.")
        except aio.CancelledError:
            break
        except Exception as e:
            LOGGER.error(f"[AutoSaveErr] {type(e).__name__}: {e}")


async def main_logic(stop_event: aio.Event):
    """代理服务器的主逻辑。
    负责规则的加载和持久化，启动和停止代理服务器。
    """
    TRIE.load_memo()
    save_task = aio.create_task(autosave_trie())
    proxy_server = await aio.start_server(start_proxy_server, "127.0.0.1", get_port())
    LOGGER.info("Proxy server started on 127.0.0.1:{}".format(get_port()))
    async with proxy_server:
        await stop_event.wait()
        LOGGER.info("Stopping proxy server...")
        # async with会自动关闭服务器
        # proxy_server.close()
        # await proxy_server.wait_closed()
        # 1. 确定当前任务（主任务）
        current_task = aio.current_task()
        # 2. 取消所有其他任务
        save_task.cancel()
        active_tasks = filter(lambda t: t is not current_task, aio.all_tasks())
        for each in active_tasks:
            each.cancel()
        if active_tasks:
            await aio.gather(*active_tasks, return_exceptions=True)
        # 3. 存储规则
        TRIE.safely_save_memo()  # 持久化
