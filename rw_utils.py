import asyncio as aio
from log_utils import get_logger

LOGGER = get_logger()


async def pipe(p_in: aio.StreamReader, p_out: aio.StreamWriter, helper_msg: str = ""):
    """将p_in中的数据写入p_out。当p_in读到EOF时，关掉p_out"""
    try:
        while data := await p_in.read(8192):
            p_out.write(data)
            await p_out.drain()
        # 传输结束，准备关闭p_out
        if p_out.can_write_eof():
            p_out.write_eof()
            await p_out.drain()
    except ConnectionResetError:
        raise ConnectionResetError("Connection reset by peer while piping")
    except Exception as e:
        LOGGER.error("[PIPING {}] {}".format(helper_msg, type(e).__name__))
    finally:
        # 关闭p_out，忽略关闭期间出问题，防止被误判为需要代理
        try:
            p_out.close()
            await p_out.wait_closed()
        except Exception as e:
            LOGGER.error("[PIPED {}] {}".format(helper_msg, type(e).__name__))


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
