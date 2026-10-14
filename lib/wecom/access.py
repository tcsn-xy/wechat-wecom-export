"""在企业微信的沙盒里拿到一个「命令行」——不附加调试器、不重启 App、不弹权限框。

原理
----
企业微信 5.x 会拉起两个 Node.js 子进程：

* ``.../Resources/WeDrive/v14.17.4/bin/node .../WeDrive/res/node.js``（微盘）
* ``.../Resources/wemail_resource/wemail_node/main.js``（邮件）

它们是主进程的子进程，**继承了企业微信的沙盒与容器授权**，所以能读
``~/Library/Containers/com.tencent.WeWorkMac/Data``——而外部进程（哪怕 sudo）
读它会被 TCC 的 ``kTCCServiceSystemPolicyAppDataDetailed`` 挡掉。

Node 自带的开发特性是：给进程发 ``SIGUSR1`` 就在 ``127.0.0.1:9229`` 起一个
inspector。于是：

    SIGUSR1  →  CDP  →  Runtime.evaluate  →  child_process.execFile('/bin/sh')

我们就得到了一个**跑在企业微信沙盒里的 shell**。写入位置限于企业微信自己有权
写的地方（``~/Downloads`` 最方便），因为沙盒是继承来的。

风险与边界
----------
* 只发 ``SIGUSR1`` 并在本机回环口说话，**不 attach 进程、不注入 dylib、不改 App**；
* 不读企业微信 UI、不点任何按钮、不发送任何消息；
* inspector 会一直开着直到进程重启。收工后可以调 :func:`close_inspector`
  把它关掉（进程重启也会自动关）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from . import cdp as _cdp

# 这两个子进程都是企业微信的孩子，都继承沙盒；WeDrive 那个更独立，优先用
NODE_HINTS = ("WeDrive/res/node.js", "wemail_node/main.js")
DEFAULT_PORTS = (9229, 9230, 9231, 9232)


# --------------------------------------------------------------------------- #
def node_processes() -> list:
    """找出企业微信里那两个 Node 子进程： [(pid, cmdline), ...]"""
    out = subprocess.run(["ps", "-axo", "pid=,command="],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        if not pid.isdigit():
            continue
        if "企业微信.app" not in cmd:
            continue
        if any(h in cmd for h in NODE_HINTS):
            found.append((int(pid), cmd.strip()))
    # WeDrive 的排前面
    found.sort(key=lambda pc: 0 if "WeDrive" in pc[1] else 1)
    return found


def listening_ports(pid: int) -> list:
    """这个进程正在 LISTEN 的本地端口。"""
    out = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True, text=True).stdout
    ports = []
    for m in re.finditer(r"127\.0\.0\.1:(\d+)\s*\(LISTEN\)", out):
        ports.append(int(m.group(1)))
    return sorted(set(ports))


def inspector_port(pid: int, wait: float = 0.0) -> int:
    """拿到该进程的 inspector 端口；没有就发 SIGUSR1 打开。"""
    ports = listening_ports(pid)
    if not ports and wait >= 0:
        try:
            os.kill(pid, 30)          # SIGUSR1：Node 的开箱调试开关
        except OSError as exc:
            raise RuntimeError("给 pid %d 发 SIGUSR1 失败: %s" % (pid, exc))
        deadline = time.time() + max(wait, 6.0)
        while time.time() < deadline and not ports:
            time.sleep(0.3)
            ports = listening_ports(pid)
    for p in ports:
        if _cdp.list_targets(p, timeout=2):
            return p
    raise RuntimeError("pid %d 上没有可用的 inspector 端口（端口: %s）" % (pid, ports))


def wecom_access():
    """返回 (pid, port)，必要时自动打开 inspector。"""
    procs = node_processes()
    if not procs:
        raise RuntimeError("没找到企业微信的 Node 子进程，企业微信在运行吗？")
    last = None
    for pid, _cmd in procs:
        try:
            return pid, inspector_port(pid, wait=6.0)
        except RuntimeError as exc:
            last = exc
    raise RuntimeError("无法在企业微信子进程上打开 inspector：%s" % last)


# --------------------------------------------------------------------------- #
_SH_JS = r"""
(function () {
  var cp = (process.mainModule && process.mainModule.require
            ? process.mainModule.require('child_process')
            : null);
  if (!cp) { return JSON.stringify({rc: -2, out: '', err: 'process.mainModule 不可用'}); }
  try {
    var out = cp.execFileSync('/bin/sh', ['-c', %s],
                              {encoding: 'utf8', maxBuffer: 1 << 28});
    return JSON.stringify({rc: 0, out: out, err: ''});
  } catch (e) {
    return JSON.stringify({
      rc: (typeof e.status === 'number' ? e.status : -1),
      out: String((e.stdout === undefined ? '' : e.stdout)),
      err: String((e.stderr === undefined ? '' : e.stderr)) + String(e.message || '')
    });
  }
})()
"""


def run_js(expression: str, port: int, timeout: float = 120.0):
    with _cdp.CDP(port=port, timeout=timeout) as client:
        return client.evaluate(expression, timeout=timeout)


def sh(command: str, port: int, timeout: float = 120.0) -> dict:
    """在企业微信沙盒里执行一条命令，返回 {'rc': int, 'out': str, 'err': str}。"""
    raw = run_js(_SH_JS % json.dumps(command), port, timeout=timeout)
    if raw is None:
        raise RuntimeError("沙盒命令没有返回结果: %s" % command)
    return json.loads(raw)


def sandbox_home() -> str:
    """企业微信沙盒里的 $HOME（就是它自己的容器 Data 目录）。"""
    return os.path.expanduser(
        "~/Library/Containers/com.tencent.WeWorkMac/Data")


def close_inspector(port: int) -> bool:
    """把 inspector 关掉，恢复原状。"""
    try:
        run_js("(function(){var i=process.mainModule.require('inspector');"
               "i.close();return 'closed';})()", port, timeout=15)
        return True
    except Exception:
        return False
