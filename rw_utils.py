import asyncio as aio

from log_utils import get_logger
from consts import MAX_TIMEOUT

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

    try:
        while data := await aio.wait_for(p_in.read(8192), MAX_TIMEOUT):
            p_out.write(data)
            await p_out.drain()
        # 传输结束，准备关闭p_out
        if p_out.can_write_eof():
            p_out.write_eof()
            await p_out.drain()
    except aio.CancelledError:
        pass
    except (ConnectionResetError, BrokenPipeError, aio.TimeoutError, OSError):
        if is_in_remote or is_out_remote:
            raise FakeDirectError("We are fooled!")
        # 如果是客户端断开（浏览器关掉页面等），属于正常现象，静默处理
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
