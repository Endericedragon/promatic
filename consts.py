# HTTPS时使用。HTTPS中，只有当服务器返回200 Connection Established时，
# 客户端才认为连接成功，通信才能继续。
CONN_ESABLISHED: str = "HTTP/1.1 200 Connection Established\r\n\r\n"
# HTTPS时使用。用于构建CONNECT请求，
# 发往目标服务器（远端或本地代理服务器）请求构建连接。
CONN_PROXY_TEMPLATE: str = "CONNECT {0}:{1} HTTP/1.1\r\nHost: {0}:{1}\r\n\r\n"

MAX_DIRECT_TIMEOUT: float = 2.0  # 直连超时时间
MAX_PROXY_TIMEOUT: float = 5.0  # 代理超时时间
MAX_RETRY: int = 3  # 最大重试次数


"""代理设置"""
PORT: int = 33333
BACKEND_PROXY_PORT: int = 32001


def get_port() -> int:
    return PORT


def set_port(port: int) -> int:
    global PORT
    PORT = port
    return PORT


def get_backend_port() -> int:
    return BACKEND_PROXY_PORT


def set_backend_port(port: int) -> int:
    global BACKEND_PROXY_PORT
    BACKEND_PROXY_PORT = port
    return BACKEND_PROXY_PORT
