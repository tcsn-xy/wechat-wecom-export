"""极简 Chrome DevTools Protocol 客户端（只用标准库）。

企业微信 5.x 的 macOS 客户端里带两个 **Node.js 子进程**（WeDrive 的 node 与
wemail 的 node），它们是主进程的孩子，因此**继承了企业微信的沙盒**——也就是说
它们能读企业微信自己的容器。

Node 有一个官方开发特性：给进程发 ``SIGUSR1`` 就会在 ``127.0.0.1:9229`` 上
启动 inspector。于是我们不需要附加调试器（Hardened Runtime 会拒绝）、不需要
改动或重启企业微信、更不需要任何系统授权弹窗，就能在那个沙盒里执行代码。

本模块只做一件事：用 WebSocket 说 CDP，把一段 JS 表达式送进去并取回结果。
WebSocket 的握手和帧解析都是手写的，不引入任何第三方依赖。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.request


class CDPError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 目标发现
# --------------------------------------------------------------------------- #
def list_targets(port: int = 9229, timeout: float = 3.0) -> list:
    """问 ``/json/list`` 有哪些可调试目标。"""
    url = "http://127.0.0.1:%d/json/list" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# WebSocket（客户端帧必须掩码，服务端帧不带掩码）
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手时连接被关闭")
            head += chunk
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise CDPError("WebSocket 握手失败: %s" % status.decode("latin-1"))
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    # -- 收 ---------------------------------------------------------------- #
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise CDPError("连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_frame(self):
        """返回 (fin, opcode, payload)。"""
        b0, b1 = self._read(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        masked = bool(b1 & 0x80)
        mask = self._read(4) if masked else b""
        payload = self._read(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self) -> str:
        """收一条完整文本消息（自己处理分片与 ping）。"""
        parts = []
        started = False
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == 0x9:                      # ping -> pong
                self.send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:                      # pong：忽略
                continue
            if opcode == 0x8:
                raise CDPError("对端关闭 WebSocket")
            if opcode in (0x1, 0x2):
                if started:
                    raise CDPError("收到嵌套的 WebSocket 分片")
                started = True
                parts.append(payload)
            elif opcode == 0x0:
                if not started:
                    raise CDPError("收到没有起始帧的 WebSocket 续帧")
                parts.append(payload)
            if fin and started:
                break
        return b"".join(parts).decode("utf-8", "replace")

    # -- 发 ---------------------------------------------------------------- #
    def send_frame(self, payload: bytes, opcode: int = 0x1):
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class CDP:
    """一次性的 CDP 连接。用法::

        with CDP(port=9229) as c:
            print(c.evaluate("1+1"))
    """

    def __init__(self, port: int = 9229, host: str = "127.0.0.1",
                 ws_path: str | None = None, timeout: float = 10.0):
        targets = list_targets(port, timeout=timeout)
        if not targets and ws_path is None:
            raise CDPError("127.0.0.1:%d 上没有可调试目标" % port)
        if ws_path is None:
            ws_path = targets[0]["webSocketDebuggerUrl"].split("127.0.0.1", 1)[1]
            ws_path = ws_path[ws_path.index("/"):]
        self.ws = _WS(host, port, ws_path, timeout=timeout)
        self._id = 0
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.ws.close()

    def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        self._id += 1
        mid = self._id
        self.ws.send_frame(json.dumps(
            {"id": mid, "method": method, "params": params or {}}).encode())
        deadline = (timeout or self.timeout) * 20
        for _ in range(int(deadline)):
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise CDPError("%s 超时" % method)

    # ------------------------------------------------------------------ #
    def evaluate(self, expression: str, timeout: float = 60.0,
                 await_promise: bool = True):
        """在目标进程里执行 JS 表达式，取回（可 JSON 化的）值。"""
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": False,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text")
            raise CDPError("JS 抛错: %s" % desc)
        return (res.get("result") or {}).get("value")
