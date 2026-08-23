import asyncio as aio
import tkinter.ttk as ttk
from threading import Thread
from tkinter import Tk

from consts import TRIE, get_backend_port, get_port, set_backend_port, set_port
from core import main_task
from domain_trie import load_memo, write_memo
from io_utils import ignore_windows_socket_reset
from log_utils import get_logger

LOGGER = get_logger()


class GUI:
    def __init__(self) -> None:
        # 协程准备
        self.loop: aio.AbstractEventLoop | None = None
        self.stop_signal: aio.Event | None = None
        self.stopped: bool = True
        self.proxy_thread: Thread | None = None
        # GUI准备
        self.root = Tk()
        self.root.title("Promatic")
        self.root.columnconfigure(1, weight=1)
        # Row 1
        self.label_backend_port = ttk.Label(self.root, text="后端端口：")
        self.input_backend_port = ttk.Entry(self.root)
        self.input_backend_port.insert(0, str(get_backend_port()))
        self.label_backend_port.grid(column=0, row=0)
        self.input_backend_port.grid(column=1, row=0, sticky="we")
        # Row 2
        self.label_port = ttk.Label(self.root, text="代理端口：")
        self.label_port.grid(column=0, row=1)
        self.input_port = ttk.Entry(self.root)
        self.input_port.insert(0, str(get_port()))
        self.input_port.grid(column=1, row=1, sticky="we")
        # Row 3
        self.start_button = ttk.Button(
            self.root, text="启动代理", command=self.handle_click
        )
        self.start_button.grid(column=0, columnspan=2, row=2, sticky="we")

    def __run_async_loop(self):
        """创建并运行异步循环。

        是新建线程的工作负载。副作用包括：
        - 为`self.stop_signal`赋值
        - 在运行结束后调用`self.__on_stopped`
        """
        self.loop = aio.new_event_loop()
        self.loop.set_exception_handler(ignore_windows_socket_reset)
        aio.set_event_loop(self.loop)
        self.stop_signal = aio.Event()
        try:
            self.loop.run_until_complete(main_task(self.stop_signal))
        except Exception as e:
            LOGGER.error(e)
        finally:
            self.root.after(0, self.__on_stopped)

    def __on_stopped(self):
        """异步循环停止时调用。副作用包括：
        - 重置按钮文本
        - 重置`self.loop`和`self.stop_signal`
        - 设置 `self.stopped` 为True
        """
        self.stopped = True
        LOGGER.info("Proxy server stopped.")
        self.start_button.config(text="启动代理")
        self.loop = None
        self.stop_signal = None
        self.input_port.config(state="normal")
        self.input_backend_port.config(state="normal")

    def handle_click(self):
        global TRIE
        if self.stopped:
            # 未启动，准备启动
            try:
                port = int(self.input_port.get())
                target_port = int(self.input_backend_port.get())
                set_port(port)
                set_backend_port(target_port)
            except ValueError:
                LOGGER.error("Invalid port number")
                self.start_button.config(text="端口号错误")
                return
            load_memo(TRIE)
            self.stopped = False
            self.start_button.config(text="停止代理")
            self.input_port.config(state="disabled")
            self.input_backend_port.config(state="disabled")
            self.proxy_thread = Thread(target=self.__run_async_loop, daemon=True)
            self.proxy_thread.start()
        else:
            # 已经启动，准备停止
            if self.loop and self.loop.is_running() and self.stop_signal:
                self.loop.call_soon_threadsafe(self.stop_signal.set)
            self.start_button.config(text="停止中……")
            write_memo(TRIE)

    def mainloop(self):
        self.root.update_idletasks()
        current_height = self.root.winfo_reqheight()
        self.root.geometry(f"225x{current_height}")
        self.root.mainloop()


if __name__ == "__main__":
    gui = GUI()
    gui.mainloop()
