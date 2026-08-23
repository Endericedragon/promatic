# Promatic

本项目旨在实现一个基于Python的代理服务器，用于处理HTTP请求并根据规则进行重定向。当收到请求时，首先尝试直连，待其超时后，重定向到指定的代理（通过端口号指定）。

## 知识点

### HTTP和HTTPS代理的请求头

HTTP请求一般长这样，除了请求体外的其他部分（包括那个空行）被称为请求头：

```
GET http://domain:port/path HTTP/1.1
Host: domain:port
User-Agent: ...

(请求体)
```

代理需要解析并提取 Host/Path，将请求头原样或改写后转发给远端，并在中间转发 Response。

HTTPS一般会先请求一个CONNECT（注意请求头最后有个空行）：

```
CONNECT domain:port HTTP/1.1
Host: domain:port

```

待连接成功后，客户端期待接受到一个这个东西（注意它也有个空行）：

```
HTTP/1.1 200 Connection Established

```

此后，代理服务器退化为一个单纯的 TCP 字节管道，对后续传输的 TLS 握手及加密数据完全不感知、不解析。

### TCP的半关闭机制

TCP 连接是全双工的。当一端发送完数据后（例如客户端发完了整个 POST 请求体），可以发送一个 FIN 包（即`write_eof()`），表示“我写完了，但我还能继续读你的回复”。

`io_utils.py`中的`pipe`函数中就使用了半关闭机制。具体地， `can_write_eof()` 探测并发送 EOF，避免直接调用 close() 粗暴关闭整个连接导致远端未能发回响应。

### 假直连问题

很多被阻断的网站，**TCP 三次握手可能成功**（或被伪造 SYN-ACK），但在发送 HTTP 请求或 TLS Client Hello 之后，**连接会被 RST 打断或丢包挂起**。由此可见，TCP 连接成功不等于可以直连。代码中设计了回调函数 `on_recv_first_remote_data`，只有当收到来自远端的第一批数据字节时，才真正将域名标记为直连（DIRECT）。

此外在`io_utils.py`的`bidirectional_pipe`函数末尾可以看到，若客户端发送了请求数据（`client_sent_bytes > 0`），但直连远端超时没有返回任何数据（`remote_recvd_bytes == 0`）时（或者直连读取首包超时）时，应当也认定为假直连。