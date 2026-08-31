import asyncio as aio
from typing import Callable, Dict

from consts import MAX_DIRECT_TIMEOUT, MAX_PROXY_TIMEOUT, BUFFER_SIZE
from log_utils import get_logger

LOGGER = get_logger()


class FakeDirectError(Exception):
    pass


async def safe_close(writer: aio.StreamWriter):
    """安安静静地关掉StreamWriter"""
    try:
        writer.close()
        await writer.wait_closed()
    except:
        pass


def ignore_windows_socket_reset(
    loop: aio.AbstractEventLoop, context: Dict[str, BaseException]
):
    """过滤 Windows 下 ProactorEventLoop 常见的底层连接重置噪音"""
    if (exc := context.get("exception")) and (
        isinstance(exc, ConnectionResetError)
        or (isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10054)
    ):
        return
    loop.default_exception_handler(context)


async def read_headers(reader: aio.StreamReader) -> bytearray:
    """读取请求头。

    该函数将从缓冲区中取走截至空行的内容，然后返回空行及之前的内容（即请求头）。
    空行后的请求体仍然保留在缓冲区中。

    请求头的格式一般如下，
    ```
    GET http://domain:port/path HTTP/1.1\r\n
    Host: domain:port\r\n
    ...\r\n  (每行均以\r\n结尾)
    \r\n  (空行，请求头至此结束)
    Payload  (请求体)
    ```
    """
    data = bytearray()
    while line := await reader.readline():  # 不停读取新行，直至EOF
        data.extend(line)
        if line in {b"\r\n", b"\n"}:  # 空行
            break
    return data


ACCEPTABLE_EXCS = (
    ConnectionResetError,
    BrokenPipeError,
    OSError,
    TimeoutError,
    aio.TimeoutError,
)


async def pipe(
    p_in: aio.StreamReader,
    p_out: aio.StreamWriter,
    helper_msg: str,
    is_in_remote: bool,
    is_out_remote: bool,
    read_through_proxy: bool = False,
    on_recv_first_data: Callable[[], None] | None = None,
) -> int:
    """将p_in中的数据写入p_out，返回从p_in读取到的字节数。

    当p_in读到EOF时，尝试向p_out发送EOF（FIN标志位）从而半关闭连接。

    Args:
        p_in: 输入流
        p_out: 输出流
        helper_msg: 日志中显示的消息
        is_in_remote: 输入流是否为远端
        is_out_remote: 输出流是否为远端
        read_through_proxy: 是否通过代理读取数据
        on_recv_first_data: 当收到首包时调用的回调函数
    Returns:
        已接收的字节数
    """
    recv_byte: int = 0  # 已接收的字节数
    while True:
        try:  # 单独处理读异常
            if is_in_remote and recv_byte == 0:
                # 首包通信，设置超时
                data = await aio.wait_for(
                    p_in.read(BUFFER_SIZE),
                    timeout=(
                        MAX_PROXY_TIMEOUT if read_through_proxy else MAX_DIRECT_TIMEOUT
                    ),
                )
            else:
                # 说明首包通信已成功，不再设置超时
                data = await p_in.read(BUFFER_SIZE)
            if not data:
                break
            if on_recv_first_data and recv_byte == 0:
                on_recv_first_data()
            recv_byte += len(data)
        except ACCEPTABLE_EXCS as e:
            if is_in_remote and recv_byte == 0:
                # 远端读取失败，且从未收到任何数据
                raise FakeDirectError(f"{type(e).__name__} during reading")
            break
        try:  # 单独处理写异常
            p_out.write(data)
            await p_out.drain()
        except ACCEPTABLE_EXCS as e:
            if is_out_remote:
                raise FakeDirectError(f"{type(e).__name__} during writing")
            break
    # 传输完毕，准备发送EOF“半关闭”连接
    try:
        if p_out.can_write_eof():
            p_out.write_eof()
            await p_out.drain()
    except Exception as e:
        LOGGER.error("[PIPING {}] {}".format(helper_msg, type(e).__name__))

    return recv_byte


async def bidirectional_pipe(
    client_reader: aio.StreamReader,
    client_writer: aio.StreamWriter,
    server_reader: aio.StreamReader,
    server_writer: aio.StreamWriter,
    read_server_through_proxy: bool = False,
    on_recv_first_remote_data: Callable[[], None] | None = None,
):
    """双向管道。将client_reader中的数据写入server_writer，将server_reader中的数据写入server_writer。
    注意：该函数不会关闭server_writer，需要另行关闭。

    Args:
        client_reader: 客户端读取流
        client_writer: 客户端写入流
        server_reader: 远端读取流
        server_writer: 远端写入流
        on_recv_first_remote_data: 当收到首个远端包时调用的回调函数
    Returns:
        无
    """
    task1 = aio.create_task(
        pipe(
            client_reader,
            server_writer,
            "ClientReader -> ServerWriter",
            False,
            True,
            False,
        )
    )
    task2 = aio.create_task(
        pipe(
            server_reader,
            client_writer,
            "ServerReader -> ClientWriter",
            True,
            False,
            read_server_through_proxy,
            on_recv_first_remote_data,
        )
    )
    # ? 等待任意任务完成
    done, pending = await aio.wait([task1, task2], return_when=aio.FIRST_COMPLETED)
    # ? 为啥可以放心取消剩余的任务？因为任务完成，等价于reader读到EOF。
    # 1. ServerReader读到EOF，这时服务端不再发送数据，客户端Reader的相应任务也当然要取消掉
    # 2. ClientReader读到EOF，大概率是浏览器被关闭等情况，这时候不应让远端继续再等，也应取消
    for task in pending:
        task.cancel()
    await aio.gather(*pending, return_exceptions=True)

    # 只在已完成的任务里检查，取消的任务不会被检查
    for task in done:
        exec = task.exception()
        if isinstance(exec, FakeDirectError):
            raise exec
    # 处理：客户端发送了数据，但远端未回复，客户端主动关闭连接
    client_sent_bytes = task1.result() if not task1.exception() else 0
    remote_recvd_bytes = task2.result() if not task2.exception() else 0
    if client_sent_bytes > 0 and remote_recvd_bytes == 0:
        raise FakeDirectError("Remote sent nothing")
