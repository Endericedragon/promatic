import asyncio as aio
import functools
import random
from typing import Callable, Tuple, Type, Awaitable, ParamSpec, TypeVar

PORT: int = 33333
BACKEND_PROXY_PORT: int = 32001

# HTTPS时使用。HTTPS中，只有当服务器返回200 Connection Established时，
# 客户端才认为连接成功，通信才能继续。
CONN_ESABLISHED: str = "HTTP/1.1 200 Connection Established\r\n\r\n"
# HTTPS时使用。用于构建CONNECT请求，
# 发往目标服务器（远端或本地代理服务器）请求构建连接。
CONN_PROXY_TEMPLATE: str = "CONNECT {0}:{1} HTTP/1.1\r\nHost: {0}:{1}\r\n\r\n"

MAX_TIMEOUT: float = 5.0  # 代理超时时间
MAX_RETRY: int = 4  # 最大重试次数

# 一些泛型常量
P = ParamSpec("P")
R = TypeVar("R")


def async_retry(
    acceptable_exceptions: Tuple[Type[BaseException], ...],
    init_delay: float = 1.0,
    factor: int = 2,
    max_retry: int = MAX_RETRY,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """
    异步重试装饰器，使用指数退避的等待策略

    Args:
        acceptable_exceptions: 可接受的异常类型，在重试时遇到这些异常不会直接抛出，除非重试次数用尽
        init_delay: 初始等待时间，单位秒
        factor: 筇避因子，每次重试等待时间乘以该因子
        max_retry: 最大重试次数
    """

    def decorator(func: Callable[P, Awaitable[R]]):
        @functools.wraps(func)  # 让func的元数据（__name__、__doc__等）保持不变
        async def wrapper(*args: P.args, **kwargs: P.kwargs):
            # 目前的等待时间
            current_delay = init_delay

            for i in range(1, max_retry + 1):
                try:
                    # 从func的返回值可以看出func是个异步函数，因此需要await其返回值
                    return await func(*args, **kwargs)
                except acceptable_exceptions as e:
                    if i == max_retry:
                        raise e
                    # 计算下个等待时间，同时加入随机抖动
                    sleep_time = current_delay * (1 + (random.random() * 0.2 - 0.1))
                    await aio.sleep(sleep_time)
                    current_delay *= factor
            raise RuntimeError("Max retry reached")

        return wrapper

    return decorator
