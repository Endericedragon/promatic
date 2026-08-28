import asyncio as aio

from consts import (
    CONN_ESABLISHED,
    CONN_PROXY_TEMPLATE,
    MAX_DIRECT_TIMEOUT,
    MAX_PROXY_TIMEOUT,
    TRIE,
    get_backend_port,
)
from domain_trie import NodeStatus
from io_utils import bidirectional_pipe, read_headers, safe_close
from log_utils import get_logger

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
    trie_search_result = TRIE.search(host)
    log_icon = repr(trie_search_result)  # 用于日志图标
    use_proxy = trie_search_result == NodeStatus.PROXY
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
            LOGGER.warning(f"[{log_icon}HErr0] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.PROXY)
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
            LOGGER.error(f"[{log_icon}HErr0] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
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
            msg = f"[{log_icon}H] {host}:{port}"
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
        LOGGER.warning(f"[{log_icon}HErr1] {e} {host}:{port}")
        if not use_proxy:
            # 3.3.1 如果命中直连规则但无法成功的，记为代理
            TRIE.insert(host, NodeStatus.PROXY)
        else:
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
    finally:
        await safe_close(target_writer)


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
    trie_search_result = TRIE.search(host)
    log_icon = repr(trie_search_result)  # 用于日志图标
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
            # 标记为需要代理，并切换到代理
            LOGGER.warning(f"[{log_icon}HSErr0] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.PROXY)
            use_proxy = True
            log_icon = repr(NodeStatus.PROXY)

    # 2. 若直连失败或命中代理规则
    if use_proxy:
        try:
            target_reader, target_writer = await aio.wait_for(
                aio.open_connection("127.0.0.1", get_backend_port()),
                timeout=MAX_PROXY_TIMEOUT,
            )
        except Exception as e:
            LOGGER.error(f"[{log_icon}HSErr0] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
            return
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
            LOGGER.error(f"[{log_icon}HSErr1] {type(e).__name__} {host}:{port}")
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
            await safe_close(target_writer)
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
            msg = f"[{log_icon}HS] {host}:{port}"
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
        LOGGER.warning(f"[{log_icon}HSErr2] {e} {host}:{port}")
        if not use_proxy:
            # 3.4 如果命中直连规则但无法成功的，记为代理
            TRIE.insert(host, NodeStatus.PROXY)
        else:
            TRIE.insert(host, NodeStatus.BRANCH)  # 走直连和代理都不行，标记为分支节点
    finally:
        await safe_close(target_writer)
