import asyncio as aio
from urllib.parse import urlparse

from consts import (
    MAX_DIRECT_TIMEOUT,
    MAX_PROXY_TIMEOUT,
    FOREST,
    get_backend_port,
    get_port,
)
from trie import NodeStatus
from io_utils import (
    bidirectional_pipe,
    read_headers,
    safe_close,
)
from protos import HTTP_INSTANCE, HTTPS_INSTANCE, ProxyRequest
from log_utils import get_logger

LOGGER = get_logger()


async def handle_conn_unified(
    reader: aio.StreamReader, writer: aio.StreamWriter, req: ProxyRequest
):
    """统一处理HTTP和HTTPS连接。
    先尝试直连服务器，若超时则换成代理访问。
    """
    forest_search_result = FOREST.search(req.host)
    log_icon = repr(forest_search_result)  # 用于日志图标
    use_proxy = forest_search_result == NodeStatus.PROXY
    has_record = forest_search_result != NodeStatus.BRANCH

    # 1. 先尝试直连服务器
    if not use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection(req.host, req.port), timeout=MAX_DIRECT_TIMEOUT
            )
            # 先不急着标记为直连，等首包通信成功后再标记
        except (aio.TimeoutError, OSError) as e:
            # 直连失败，记录日志并切换为代理
            LOGGER.warning(
                f"[{log_icon}ErrDirect] {type(e).__name__} {req.host}:{req.port}"
            )
            FOREST.insert(req.host, NodeStatus.PROXY)
            log_icon = repr(NodeStatus.PROXY)
            use_proxy = True
    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", get_backend_port()),
                timeout=MAX_PROXY_TIMEOUT,
            )
        except Exception as e:
            # todo 后端代理没开吧？？
            LOGGER.error(
                f"[{log_icon}ErrProxy] {type(e).__name__} {req.host}:{req.port}"
            )
            FOREST.insert(
                req.host, NodeStatus.BRANCH
            )  # 走直连和代理都不行，标记为分支节点
            return
        if not await req.proto.setup_proxy_tunnel(target_reader, target_writer, req):
            LOGGER.error(f"[{log_icon}ErrHTTPSConn] {req.host}:{req.port}")
            FOREST.insert(
                req.host, NodeStatus.BRANCH
            )  # 走直连和代理都不行，标记为分支节点
            await safe_close(target_writer)
            return
    # 3. 开始通信
    try:
        await req.proto.prepare_communication(target_writer, writer, req)

        # 3.2 然后让用户和目标直接双向通信
        def mark_as():  # 当返回首包时，可以准确标记域名为直连还是代理了
            global LOGGER, FOREST
            nonlocal use_proxy
            msg = f"[{log_icon}] {req.host}:{req.port}"
            if has_record:
                LOGGER.debug(msg)
            else:
                LOGGER.info(msg)
            if not use_proxy:
                # 首包通信成功，才能放心将其标记为直连
                FOREST.insert(req.host, NodeStatus.DIRECT)

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
        LOGGER.warning(f"[{log_icon}Err-TryTransfer] {e} {req.host}:{req.port}")
        if not use_proxy:
            # 3.4 如果命中直连规则但无法成功的，记为代理
            FOREST.insert(req.host, NodeStatus.PROXY)
        else:
            FOREST.insert(
                req.host, NodeStatus.BRANCH
            )  # 走直连和代理都不行，标记为分支节点
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
                proxy_req = ProxyRequest(HTTPS_INSTANCE, host, port, header_bytes)
                await handle_conn_unified(reader, writer, proxy_req)
            case _:
                # HTTP请求，如GET、POST等
                parsed = urlparse(path)
                assert parsed.hostname is not None and parsed.port is not None
                proxy_req = ProxyRequest(
                    HTTP_INSTANCE, parsed.hostname, parsed.port, header_bytes
                )
                await handle_conn_unified(reader, writer, proxy_req)
    except Exception as e:
        LOGGER.error(f"[ServerErr] {type(e).__name__}: {e}")
    finally:
        await safe_close(writer)


async def autosave_trie(interval_sec: int = 60):
    while True:
        try:
            await aio.sleep(interval_sec)
            if FOREST.safely_save_memo():
                LOGGER.debug("[AutoSave] Rules saved successfully.")
        except aio.CancelledError:
            break
        except Exception as e:
            LOGGER.error(f"[AutoSaveErr] {type(e).__name__}: {e}")


async def main_logic(stop_event: aio.Event):
    """代理服务器的主逻辑。
    负责规则的加载和持久化，启动和停止代理服务器。
    """
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
        active_tasks = list(filter(lambda t: t is not current_task, aio.all_tasks()))
        for each in active_tasks:
            each.cancel()
        if active_tasks:
            await aio.gather(*active_tasks, return_exceptions=True)
        # 3. 存储规则
        FOREST.safely_save_memo()  # 持久化
