class PromaticError(Exception):
    pass


class FakeDirectError(PromaticError):
    """通过直连能成功TCP三次握手，但无法真正通信的“假直连”错误"""

    pass


class DirectHandshakeError(PromaticError):
    """无法直连完成TCP三次握手"""

    pass


class ProxyHandshakeError(PromaticError):
    """无法通过代理完成TCP三次握手"""

    pass
