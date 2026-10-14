"""微信 4.x 密钥捕获与沙盒数据搬运（lldb 命令模块）。

背景
----
微信的容器 ``~/Library/Containers/com.tencent.xinWeChat`` 受 macOS TCC 保护，
**任何外部进程（包括 root）都读不了**。而微信进程自己可以，并且它还持有
``com.apple.security.files.downloads.read-write`` 权限。

于是思路是：用 lldb 挂到微信进程上，把一小段程序注入进去执行——
*抓密钥* 用 ``CCKeyDerivationPBKDF`` / ``CCCryptorCreate`` 断点；
*搬数据* 用微信自己的 ``clonefile(2)`` 把容器里的目录写一份到 ``~/Downloads``。

前置条件：目标进程必须允许调试器附加。腾讯官方签名带 Hardened Runtime，
会拒绝附加，所以需要先用 ``bin/_sign_app.sh`` 给一份**副本**重新签名，
再用这份副本启动微信；原版 App 全程不动。

用法（在 lldb 里）::

    (lldb) command script import lldb/wxcap.py
    (lldb) wxattach  WeChat 60
    (lldb) wxcapture 150 /path/capture.jsonl
    (lldb) wxclone   <容器里的目录> <目标目录>
    (lldb) wxwalk    <目录> <列表输出文件>
    (lldb) wxprobe   <目录> <诊断输出文件>

一般不用手敲，直接跑 ``scripts/capture_keys.py`` / ``scripts/clone_data.py``。
"""
import json
import os
import shlex
import time

import lldb


def _say(msg):
    """Unbuffered output — lldb's Python stdout is block-buffered under --batch."""
    try:
        os.write(1, (str(msg) + "\n").encode("utf-8", "replace"))
    except Exception:
        pass


_state = {"out": None, "keys": set(), "pbkdf": set(), "saltkeys": set(),
          "n": 0, "proc": None, "target": None}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _log(rec):
    _state["n"] += 1
    with open(_state["out"], "a") as f:
        f.write(json.dumps(rec) + "\n")


def _mem(process, addr, size):
    if not addr or not size:
        return None
    err = lldb.SBError()
    data = process.ReadMemory(addr, size, err)
    if err.Success() and data is not None and len(data) == size:
        return bytes(data)
    return None


def _reg(frame, name):
    r = frame.FindRegister(name)
    if not r or not r.IsValid():
        return 0
    return r.GetValueAsUnsigned()


def _wait_for_pid(name, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = os.popen("/usr/bin/pgrep -x %s | head -1" % name).read().strip()
        if out.isdigit():
            return int(out)
        time.sleep(0.3)
    return 0


# --------------------------------------------------------------------------- #
# breakpoint callbacks
# --------------------------------------------------------------------------- #
def _crypt_cb(frame, bp_loc, internal):
    try:
        if _reg(frame, "x4") != 32:
            return False
        ptr = _reg(frame, "x3")
        data = _mem(frame.GetThread().GetProcess(), ptr, 32)
        if data:
            h = data.hex()
            if h not in _state["keys"]:
                _state["keys"].add(h)
                _log({"t": "crypt", "key": h})
    except Exception:
        pass
    return False


def _md5_cb(frame, bp_loc, internal):
    """企业微信专用：抓 wxSQLite3 页密钥派生那一步的 MD5 输入。

    企业微信每读一页都会算 ``MD5(raw_key(16) || LE32(page_no) || b"sAlT")``。
    坑在于**它用的不是 CommonCrypto 的 MD5**：二进制里静态链了 OpenSSL
    （``MD5_Init/MD5_Update/MD5_Final``、``md5_block_data_order``），所以几个
    MD5 入口都要断上（实测：只断 CC_MD5 一条都抓不到）。

    各实现参数位置不同，两种约定都试：
      * ``CC_MD5(data, len, md)`` / ``MD5Sum(data, len, digest)`` → x0=data, x1=len
      * ``CC_MD5_Update/MD5_Update(ctx, data, len)``             → x1=data, x2=len
      * ``base::MD5Update(ctx, piece)``（piece 在 x1/x2）        → 同上
    """
    try:
        proc = frame.GetThread().GetProcess()
        for ptr_reg, len_reg in (("x0", "x1"), ("x1", "x2")):
            ptr, length = _reg(frame, ptr_reg), _reg(frame, len_reg)
            if length != 24 or not ptr:
                continue
            data = _mem(proc, ptr, 24)
            if not data or data[20:24] != b"sAlT":
                continue
            h = data[:16].hex()
            if h in _state["saltkeys"]:
                continue
            _state["saltkeys"].add(h)
            _log({"t": "saltkey", "key": h,
                  "page": int.from_bytes(data[16:20], "little"),
                  "via": frame.GetFunctionName() or "?"})
    except Exception:
        pass
    return False


def _pbkdf_cb(frame, bp_loc, internal):
    try:
        proc = frame.GetThread().GetProcess()
        pw_ptr, pw_len = _reg(frame, "x1"), _reg(frame, "x2")
        salt_ptr, salt_len = _reg(frame, "x3"), _reg(frame, "x4")
        rounds, dk_ptr = _reg(frame, "x6"), _reg(frame, "x7")
        pw = _mem(proc, pw_ptr, min(pw_len, 512)) if pw_ptr and pw_len else None
        salt = _mem(proc, salt_ptr, min(salt_len, 64)) if salt_ptr and salt_len else None
        sig = "%s|%s|%s" % (pw.hex() if pw else "-",
                            salt.hex() if salt else "-", rounds)
        if sig in _state["pbkdf"]:
            return False
        _state["pbkdf"].add(sig)
        _log({"t": "pbkdf", "rounds": rounds, "pwlen": pw_len, "saltlen": salt_len,
              "pw": pw.hex() if pw else None,
              "salt": salt.hex() if salt else None})
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def wxattach(debugger, command, result, internal_dict):
    args = shlex.split(command)
    name = args[0] if args else "WeChat"
    wait = int(args[1]) if len(args) > 1 else 60
    debugger.SetAsync(True)
    pid = _wait_for_pid(name, wait)
    if not pid:
        _say("NO_PROCESS %s" % name)
        return
    target = debugger.CreateTarget("")
    err = lldb.SBError()
    process = target.Attach(lldb.SBAttachInfo(pid), err)
    if not err.Success() or not process or not process.IsValid():
        _say("ATTACH_FAIL %s" % err.GetCString())
        return
    _state.update(proc=process, target=target)
    _say("ATTACHED pid=%d" % process.GetProcessID())


def wxcapture(debugger, command, result, internal_dict):
    """wxcapture <seconds> <out.jsonl> [both|pbkdf|crypt]"""
    args = shlex.split(command)
    dur, out = int(args[0]), os.path.abspath(os.path.expanduser(args[1]))
    mode = args[2] if len(args) > 2 else "both"
    _state["out"] = out
    open(out, "a").close()
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return

    b1 = b2 = None
    if mode in ("both", "crypt"):
        b1 = target.BreakpointCreateByName("CCCryptorCreate")
        b1.SetScriptCallbackFunction("wxcap._crypt_cb")
    if mode in ("both", "pbkdf"):
        b2 = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
        b2.SetScriptCallbackFunction("wxcap._pbkdf_cb")
    _say("BP mode=%s crypt=%s pbkdf=%s"
         % (mode, b1.GetNumLocations() if b1 else "-",
            b2.GetNumLocations() if b2 else "-"))

    listener = debugger.GetListener()
    process.Continue()
    deadline = time.time() + dur
    last = -1
    while time.time() < deadline:
        ev = lldb.SBEvent()
        if listener.WaitForEvent(1, ev) and lldb.SBProcess.EventIsProcessEvent(ev):
            st = lldb.SBProcess.GetStateFromEvent(ev)
            if st == lldb.eStateExited:
                _say("PROC_EXITED")
                break
            if st == lldb.eStateStopped:
                process.Continue()
        if _state["n"] != last:
            last = _state["n"]
            _say("CAPTURED records=%d keys=%d pbkdf=%d"
                 % (last, len(_state["keys"]), len(_state["pbkdf"])))
    process.Stop()
    time.sleep(0.3)
    if b1:
        target.BreakpointDelete(b1.GetID())
    if b2:
        target.BreakpointDelete(b2.GetID())
    process.Continue()
    _say("CAPTURE_DONE keys=%d pbkdf=%d"
         % (len(_state["keys"]), len(_state["pbkdf"])))


def wxsalt(debugger, command, result, internal_dict):
    """wxsalt <seconds> <out.jsonl> — 抓企业微信数据库的 16 字节 raw key。

    只在企业微信上有效：断 ``CC_MD5``/``CC_MD5_Update``，命中长度 24、
    末尾 ``sAlT`` 的那次调用，前 16 字节就是 raw key（详见 ``_md5_cb``）。
    """
    args = shlex.split(command)
    dur, out = int(args[0]), os.path.abspath(os.path.expanduser(args[1]))
    _state["out"] = out
    open(out, "a").close()
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return

    # OpenSSL 的 MD5_Update 才是企业微信真正用的那条（实测只断 CC_MD5 抓不到）
    bps = []
    for sym in ("CC_MD5", "CC_MD5_Update", "MD5_Update", "MD5_Final",
                "MD5_Init", "base::MD5Sum", "base::MD5String", "base::MD5Update"):
        bp = target.BreakpointCreateByName(sym)
        bp.SetScriptCallbackFunction("wxcap._md5_cb")
        bps.append((sym, bp))
    try:
        rx = target.BreakpointCreateByRegex("MD5_Update$|MD5Sum$|MD5String$|CC_MD5$")
        rx.SetScriptCallbackFunction("wxcap._md5_cb")
        bps.append(("regex", rx))
    except Exception:
        pass
    _say("SALT_BP %s" % [(n, b.GetNumLocations()) for n, b in bps])

    listener = debugger.GetListener()
    process.Continue()
    deadline = time.time() + dur
    last = -1
    while time.time() < deadline:
        ev = lldb.SBEvent()
        if listener.WaitForEvent(1, ev) and lldb.SBProcess.EventIsProcessEvent(ev):
            st = lldb.SBProcess.GetStateFromEvent(ev)
            if st == lldb.eStateExited:
                _say("PROC_EXITED")
                break
            if st == lldb.eStateStopped:
                process.Continue()
        if len(_state["saltkeys"]) != last:
            last = len(_state["saltkeys"])
            _say("SALT keys=%d" % last)
    process.Stop()
    time.sleep(0.3)
    for _n, b in bps:
        target.BreakpointDelete(b.GetID())
    process.Continue()
    _say("SALT_DONE keys=%d" % len(_state["saltkeys"]))


def wxdump(debugger, command, result, internal_dict):
    """wxdump <outdir> [max_mb] — 导出目标进程所有可写内存，离线找 key。

    为什么比等断点稳：key 一定躺在内存里，dump 出来慢慢扫就行，不用赌
    「某一刻刚好发生一次页解密」。只导可写的、非可执行段（堆/栈/数据），
    跳过 dyld 共享缓存那种几百 MB 的只读映射。
    """
    args = shlex.split(command)
    if not args:
        _say("usage: wxdump <outdir> [max_mb]")
        return
    outdir = os.path.abspath(os.path.expanduser(args[0]))
    max_bytes = (int(args[1]) if len(args) > 1 else 2048) * 1024 * 1024
    os.makedirs(outdir, exist_ok=True)

    process = _state["proc"]
    if process is None:
        _say("NOT_ATTACHED")
        return

    process.Stop()
    time.sleep(0.2)
    info = lldb.SBMemoryRegionInfo()
    addr, idx, total = 0, 0, 0
    while total < max_bytes:
        err = process.GetMemoryRegionInfo(addr, info)
        if err.Fail():
            break
        base, end = info.GetRegionBase(), info.GetRegionEnd()
        if end <= base:
            break
        size = end - base
        if (info.IsWritable() and not info.IsExecutable()
                and 0 < size <= 512 * 1024 * 1024):
            rerr = lldb.SBError()
            data = process.ReadMemory(base, size, rerr)
            if rerr.Success() and data:
                path = os.path.join(outdir, "rw_%03d_%x.bin" % (idx, base))
                with open(path, "wb") as fh:
                    fh.write(data)
                total += len(data)
                idx += 1
                _say("DUMP %s %.1f MB" % (os.path.basename(path), len(data) / 1e6))
        addr = end
    process.Continue()
    _say("DUMP_DONE regions=%d bytes=%.1f MB" % (idx, total / 1e6))


def wxcopy(debugger, command, result, internal_dict):
    """wxcopy <src> <dst> — 让目标进程起一个**后台**子进程把 src 拷到 dst。

    为什么不直接用 ``wxclone``：目录级 ``clonefile`` 是在目标进程的线程上跑的，
    跑完之前整个 App 都是停住的。实测拷 ``db_storage`` 会卡好几分钟 → 微信假死，
    这是不能接受的。改成 ``system("... &")``：表达式立刻返回、进程马上恢复，
    真正的拷贝发生在一个**继承了它沙盒与 Downloads 权限**的子进程里。

    完成后会在 ``<dst>.rc`` 写一行 ``OK`` / ``FAIL``，外面轮询这个文件即可。

    ⚠️ **实测不可用（2026-09-20）**：在 lldb 停住的目标进程里 ``system()`` 会 fork，
    子进程会继承停止状态，表达式永远不返回 —— 微信会假死。别用这个命令，
    抓快照请走 ``scripts/clone_data.py``（内部用 ``wxclone``），
    并且**目标目录要用 ~/Downloads 中转**（见那边的注释）。
    """
    args = shlex.split(command)
    if len(args) < 2:
        _say("usage: wxcopy <src> <dst>")
        return
    src, dst = args[0], args[1]
    marker = dst + ".rc"
    parent = os.path.dirname(dst)
    inner = ("mkdir -p '%s' && /bin/cp -Rc '%s' '%s' && /bin/echo OK > '%s' "
             "|| /bin/echo FAIL > '%s'") % (parent, src, dst, marker, marker)
    cmdline = "/bin/sh -c " + json.dumps(inner) + " &"
    code = ('extern "C" int system(const char *);\n'
            '(int)system(%s)' % json.dumps(cmdline))
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return
    rc, detail = _eval(process, target, code)
    _say("COPY rc=%s err=%s %s -> %s" % (rc, detail, src, dst))


def _eval(process, target, code, top_level=False):
    """在目标进程里求值。

    ⚠️ **不要打开 top_level**（2026-09-20 踩过）：`SetTopLevel(True)` 会让这个
    微信版本上的表达式**永远不返回**，表现是 lldb 卡死、微信假死（实测目录级
    `clonefile` 挂 5 分钟以上）。改成普通表达式后，文件与目录 `clonefile` 都
    立刻返回 rc=0。普通表达式里不需要 `extern "C"` 声明，lldb 自己能从已加载的
    库里解析出 `clonefile`。
    """
    process.Stop()
    opts = lldb.SBExpressionOptions()
    opts.SetTimeoutInMicroSeconds(900 * 1000 * 1000)
    opts.SetIgnoreBreakpoints(True)
    if top_level:
        opts.SetTopLevel(True)
    opts.SetLanguage(lldb.eLanguageTypeC_plus_plus)
    val = target.EvaluateExpression(code, opts)
    err = val.GetError()
    rc = val.GetValueAsSigned() if not err.Fail() else None
    detail = "-" if not err.Fail() else err.GetCString()
    process.Continue()
    return rc, detail


def wxclone(debugger, command, result, internal_dict):
    """wxclone <src> <dst> — clonefile() inside WeChat's own process."""
    args = shlex.split(command)
    if len(args) < 2:
        _say("usage: wxclone <src> <dst>")
        return
    src, dst = args[0], args[1]
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return
    # 注意 1：表达式在同一个 lldb 会话里会被反复编译，用 static 变量会撞上
    #         "redefinition" 错误，所以直接返回调用结果，不引入具名变量。
    # 注意 2：不要加 extern "C" 声明、也不要开 SetTopLevel —— 见 _eval 的注释。
    code = '(int)clonefile(("%s"), ("%s"), 0)' % (src, dst)
    rc, detail = _eval(process, target, code)
    _say("CLONE rc=%s err=%s %s -> %s" % (rc, detail, src, dst))


def wxwalk(debugger, command, result, internal_dict):
    """wxwalk <dir> <out-file> — recursive listing written by WeChat itself."""
    args = shlex.split(command)
    if len(args) < 2:
        _say("usage: wxwalk <dir> <out_file>")
        return
    src, out = args[0], args[1]
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return

    code = r'''
    void *opendir(const char *);
    void *readdir(void *);
    int closedir(void *);
    void *fopen(const char *, const char *);
    int fprintf(void *, const char *, ...);
    int fclose(void *);
    struct DE { unsigned long long ino; unsigned long long off;
                unsigned short reclen; unsigned short namlen;
                unsigned char type; char name[1024]; };
    char gbuf[1024];
    char gstk[32][1024];
    int gsp = 0, gi, gj, gn = 0, gnl;
    void *gf;
    gj = 0;
    while (SRCPATH[gj] && gj < 1000) { gstk[0][gj] = SRCPATH[gj]; gj++; }
    gstk[0][gj] = 0;
    gsp = 1;
    gf = fopen(OUTPATH, "w");
    if (gf) fprintf(gf, "#SRC=[%s]\n#STK=[%s]\n", SRCPATH, gstk[0]);
    while (gsp > 0 && gf) {
      gsp--;
      void *gd = opendir(gstk[gsp]);
      if (!gd) continue;
      struct DE *ge;
      while ((ge = (struct DE *)readdir(gd)) != 0) {
        if (ge->name[0] == '.' && (ge->name[1] == 0 ||
            (ge->name[1] == '.' && ge->name[2] == 0))) continue;
        if (ge->namlen == 0 || ge->namlen > 255) continue;
        gnl = 0;
        for (gi = 0; gstk[gsp][gi] && gnl < 950; gi++) gbuf[gnl++] = gstk[gsp][gi];
        gbuf[gnl++] = '/';
        for (gi = 0; gi < (int)ge->namlen && gnl < 1000; gi++) gbuf[gnl++] = ge->name[gi];
        gbuf[gnl] = 0;
        if (ge->type == 4) {
          if (gsp < 31) { for (gi = 0; gi <= gnl; gi++) gstk[gsp][gi] = gbuf[gi]; gsp++; }
        } else {
          int isdir = 0;
          if (ge->type == 0) { void *gt = opendir(gbuf); if (gt) { closedir(gt); isdir = 1; } }
          if (isdir) {
            if (gsp < 31) { for (gi = 0; gi <= gnl; gi++) gstk[gsp][gi] = gbuf[gi]; gsp++; }
          } else {
            gn++;
            fprintf(gf, "%s\n", gbuf);
          }
        }
      }
      closedir(gd);
    }
    if (gf) fprintf(gf, "#TOTAL\t%d\n", gn);
    if (gf) fclose(gf);
    gn;
    '''
    code = code.replace("SRCPATH", '("%s")' % src).replace("OUTPATH", '("%s")' % out)
    opts = lldb.SBExpressionOptions()
    opts.SetTimeoutInMicroSeconds(900 * 1000 * 1000)
    opts.SetIgnoreBreakpoints(True)
    opts.SetLanguage(lldb.eLanguageTypeC)
    val = target.EvaluateExpression(code, opts)
    err = val.GetError()
    _say("WALK rc=%s err=%s -> %s"
         % (val.GetValueAsSigned() if not err.Fail() else "-",
            err.GetCString() if err.Fail() else "-", out))
    process.Continue()


def wxprobe(debugger, command, result, internal_dict):
    """wxprobe <dir> <out-file> — diagnose opendir/readdir from inside WeChat."""
    args = shlex.split(command)
    src, out = args[0], args[1]
    process, target = _state["proc"], _state["target"]
    if process is None:
        _say("NOT_ATTACHED")
        return
    process.Stop()
    code = r'''
    void *opendir(const char *);
    void *readdir(void *);
    int closedir(void *);
    void *fopen(const char *, const char *);
    int fprintf(void *, const char *, ...);
    int fclose(void *);
    int *__error(void);
    struct DE { unsigned long long ino; unsigned long long off;
                unsigned short reclen; unsigned short namlen;
                unsigned char type; char name[1024]; };
    void *gpf = fopen(OUTPATH, "w");
    int grc = 0;
    if (gpf) {
      fprintf(gpf, "errno_before=%d\n", *__error());
      void *gd = opendir(SRCPATH);
      fprintf(gpf, "dir=%p errno=%d\n", gd, *__error());
      if (gd) {
        struct DE *ge;
        int gn2 = 0;
        while ((ge = (struct DE *)readdir(gd)) != 0 && gn2 < 12) {
          fprintf(gpf, "type=%d namlen=%d reclen=%d name=[%s]\n",
                  (int)ge->type, (int)ge->namlen, (int)ge->reclen, ge->name);
          gn2++;
        }
        fprintf(gpf, "entries=%d errno_after=%d\n", gn2, *__error());
        closedir(gd);
        grc = gn2;
      }
      fclose(gpf);
    }
    grc;
    '''
    code = code.replace("SRCPATH", '("%s")' % src).replace("OUTPATH", '("%s")' % out)
    opts = lldb.SBExpressionOptions()
    opts.SetTimeoutInMicroSeconds(120 * 1000 * 1000)
    opts.SetIgnoreBreakpoints(True)
    opts.SetLanguage(lldb.eLanguageTypeC)
    val = target.EvaluateExpression(code, opts)
    err = val.GetError()
    _say("PROBE rc=%s err=%s" % (val.GetValueAsSigned() if not err.Fail() else "-",
                                 err.GetCString() if err.Fail() else "-"))
    process.Continue()


def __lldb_init_module(debugger, internal_dict):
    for fn in (wxattach, wxcapture, wxsalt, wxdump, wxclone, wxcopy,
               wxwalk, wxprobe):
        debugger.HandleCommand("command script add -f wxcap.%s %s"
                               % (fn.__name__, fn.__name__))
