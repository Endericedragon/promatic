import asyncio as aio
import signal
from sys import platform as current_platform

import pystray
from PIL import Image, ImageDraw
from pystray._base import Icon as PIcon

from consts import get_port
from core import main_logic
from io_utils import ignore_windows_socket_reset
from log_utils import get_logger

LOGGER = get_logger()


# ==== 任务栏相关 ====
def default_tray_icon() -> Image.Image:
    """创建托盘上的图标。"""

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    elli_radius: int = 24
    draw.ellipse(
        (32 - elli_radius, 32 - elli_radius, 32 + elli_radius, 32 + elli_radius),
        fill=None,
        outline="orange",
        width=10,
    )
    rect_size: int = 24
    draw.rectangle((64 - rect_size, 0, 64, rect_size), fill="white")
    return image


def create_tray_utils(loop: aio.AbstractEventLoop, stop_event: aio.Event) -> PIcon:
    """设置托盘图标的右键菜单内容。
    Args:
        loop: 异步事件循环。用于发送停机信号。
        stop_event: 停机信号。
    Returns:
        PIcon: 托盘图标对象。
    """

    def on_exit(icon: PIcon, _: pystray.MenuItem):
        LOGGER.info("Exit in tray")
        loop.call_soon_threadsafe(stop_event.set)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(f"Port: {get_port()}", lambda *_: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )

    icon: PIcon = pystray.Icon(name="Promatic", icon=default_tray_icon(), menu=menu)
    return icon


# =====================


async def set_signals_and_run(stop_event: aio.Event):
    """设置信号处理函数并运行异步循环。"""
    loop = aio.get_running_loop()
    loop.set_exception_handler(ignore_windows_socket_reset)

    # 托盘
    tray_icon = create_tray_utils(loop, stop_event)
    tray_icon.run_detached()

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

    try:
        await main_logic(stop_event)
    finally:
        tray_icon.stop()


if __name__ == "__main__":
    stop_event = aio.Event()
    try:
        aio.run(set_signals_and_run(stop_event))
    except KeyboardInterrupt as e:
        pass
    except Exception as e:
        LOGGER.error("Unknown error:", type(e).__name__)
    finally:
        LOGGER.info("Shutting down proxy server...")
