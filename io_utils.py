import asyncio as aio
from typing import Tuple

from consts import MAX_TIMEOUT, async_retry
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


@async_retry((aio.TimeoutError, OSError))
async def try_open_connection(
    host: str, port: int
) -> Tuple[aio.StreamReader, aio.StreamWriter]:
    """尝试打开连接。

    使用async_retry包装后，采用指数退避的重试策略。
    """
    return await aio.wait_for(aio.open_connection(host, port), timeout=MAX_TIMEOUT)


ACCEPTABLE_EXCS = (
    ConnectionResetError,
    BrokenPipeError,
    OSError,
    TimeoutError,
    aio.TimeoutError,
)


@async_retry(ACCEPTABLE_EXCS)
async def try_read(reader: aio.StreamReader):
    return await aio.wait_for(reader.read(8192), timeout=MAX_TIMEOUT)


async def pipe(
    p_in: aio.StreamReader,
    p_out: aio.StreamWriter,
    helper_msg: str,
    is_in_remote: bool,
    is_out_remote: bool,
):
    """将p_in中的数据写入p_out。当p_in读到EOF时，尝试向p_out发送EOF

    Args:
        p_in: 输入流
        p_out: 输出流
        helper_msg: 日志中显示的消息
        is_in_remote: 输入流是否为远端
        is_out_remote: 输出流是否为远端
    """

    while True:
        try:  # 单独处理读异常
            # data = await aio.wait_for(p_in.read(8192), MAX_TIMEOUT)
            data = await try_read(p_in)
            if not data:
                break
        except ACCEPTABLE_EXCS as e:
            if is_in_remote:
                raise FakeDirectError(f"Read failed with {type(e).__name__}")
            break
        try:  # 单独处理写异常
            p_out.write(data)
            await p_out.drain()
        except ACCEPTABLE_EXCS as e:
            if is_out_remote:
                raise FakeDirectError(f"Write failed with {type(e).__name__}")
            break
    try:
        if p_out.can_write_eof():
            p_out.write_eof()
            await p_out.drain()
    except Exception as e:
        LOGGER.error("[PIPING {}] {}".format(helper_msg, type(e).__name__))


async def bidirectional_pipe(
    cr: aio.StreamReader,
    cw: aio.StreamWriter,
    sr: aio.StreamReader,
    sw: aio.StreamWriter,
):
    """双向管道。将cr中的数据写入cw，将sr中的数据写入sw"""
    task1 = aio.create_task(pipe(cr, sw, "CR -> SW", False, True))
    task2 = aio.create_task(pipe(sr, cw, "SR -> CW", True, False))
    # ? 等待任意任务完成
    done, pending = await aio.wait([task1, task2], return_when=aio.FIRST_COMPLETED)
    # ? 然后取消剩余的任务
    for task in pending:
        task.cancel()
    await aio.gather(*pending, return_exceptions=True)
    # 最后关闭远端writer
    await safe_close(sw)

    # 只在已完成的任务里检查，取消的任务不会被检查
    for task in done:
        exec = task.exception()
        if isinstance(exec, FakeDirectError):
            raise exec
