import asyncio as aio


async def read_http_headers(reader: aio.StreamReader) -> bytes:
    while line := await reader.readline():  # 不停读取新行，直至EOF

        pass
    return b""


async def start_proxy_server(reader: aio.StreamReader, writer: aio.StreamWriter):
    pass


if __name__ == "__main__":
    aio.run(aio.start_server(start_proxy_server, "127.0.0.1", 33333))
