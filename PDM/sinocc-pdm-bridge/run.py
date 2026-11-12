"""Standalone entry point for the packaged executable.

环境变量（都可选，默认值 = 原行为）：
  PDM_PORT=9126        指定监听端口（默认 9123；被**别的**程序占用会自动顺延）
  PDM_HOST=0.0.0.0     指定监听地址（默认 127.0.0.1；公网/内网穿透接入时改 0.0.0.0）
  PDM_MCP_TOKEN=xxx    给 /mcp 端点加鉴权（默认空=免鉴权；暴露公网前务必设置）
  PDM_MCP_ALLOWED_HOSTS=mcp.example.com, mcp.example.com:*
                       公网接入的 Host 白名单（mcp SDK 默认只信本机，否则 421）
  PDM_PUBLIC_BASE=https://x.example.com  仅用于 /mcp-status 回显外部访问地址
  PDM_NO_BROWSER=1     启动后不开浏览器（自动化验收用；同时抑制弹框）
  PDM_NO_DIALOG=1      启动失败时不弹错误框（只写日志）
  PDM_LOG_TO_FILE=1    强制落盘（默认就已落盘）

日志：<exe 同级>/logs/bridge.log（5MB 轮转 ×3）；目录不可写时依次退到
      %LOCALAPPDATA%\\SinoCC-PDM-Bridge\\logs\\bridge.log、%TEMP%\\SinoCC-PDM-Bridge-logs\\bridge.log。

排障设计（2026-09-10，起因「拷到别的电脑双击直接闪退」）：
- 启动期任何异常 → 写 crash.log + 弹一个错误框（双击场景本来只会「闪一下没了」，
  什么线索都留不下；控制台版双击同理，窗口随进程一起消失）。
- 启动时把系统/架构/解释器/端口探测结果全部记进日志，方便异地机器回传。
- 端口被**非本程序**占用时不再直接退出（旧行为=静默闪退），改为自动顺延端口。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from pathlib import Path

BIND_HOST = os.environ.get("PDM_HOST", "127.0.0.1")
# HOST 只用于「本机自连 / 开浏览器 / 日志展示」；监听地址可能是 0.0.0.0，
# 拿 0.0.0.0 去 connect 或喂给 webbrowser 虽然 Windows 上多半能通，但不干净。
HOST = "127.0.0.1" if BIND_HOST in ("0.0.0.0", "::", "") else BIND_HOST
# 默认 9123；单文件版验收/并行调试可用 PDM_PORT 换端口（如 PDM_PORT=9126）。
PORT = int(os.environ.get("PDM_PORT", "9123"))

# 端口顺延上限（preferred+1 .. preferred+N）
PORT_SCAN = 50


def _say(msg: str) -> None:
    """无控制台（--windowed 打包）时 sys.stdout 为 None，print 会静默，这里显式兜底。"""
    if sys.stdout is not None:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _log(msg: str) -> None:
    try:
        import logging

        logging.getLogger("bridge.launcher").info(msg)
    except Exception:
        pass


def _clear_proxies() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


def _load_portable_env() -> None:
    """Load `.env` sitting next to the exe (portable / single-file deployment)."""
    try:
        from dotenv import load_dotenv

        exe_dir = Path(sys.executable).resolve().parent
        load_dotenv(exe_dir / ".env", override=False)
    except Exception:
        return


def _has_console() -> bool:
    """真的绑定了可用控制台吗？

    注意不能只判 `sys.stderr is None`：--windowed（GUI 子系统）下 PyInstaller
    给的 stdout/stderr 未必是 None，本机实测 stderr 非 None → 误判「有控制台」
    → 日志既不落盘也看不见（窗口版排查时零线索，2026-09-10 踩坑）。
    改为探测 fileno 是否可用（无控制台时抛异常 / 返回负值）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.fileno() >= 0:
                return True
        except Exception:
            continue
    return False


def _interactive() -> bool:
    """双击场景才弹窗；验收/自动化（PDM_NO_BROWSER=1）不弹，避免阻塞脚本。"""
    if os.environ.get("PDM_NO_DIALOG") == "1":
        return False
    return os.environ.get("PDM_NO_BROWSER") != "1"


def _log_dir() -> Path | None:
    """日志目录：exe 同级 logs/ → %LOCALAPPDATA% → %TEMP%（三级兜底，尽量一定写得进去）。"""
    candidates: list[Path] = []
    try:
        candidates.append(Path(sys.executable).resolve().parent / "logs")
    except Exception:
        pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "SinoCC-PDM-Bridge" / "logs")
    try:
        candidates.append(Path(tempfile.gettempdir()) / "SinoCC-PDM-Bridge-logs")
    except Exception:
        pass

    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            # 真正试写一次：目录存在但只读（共享盘 / Program Files）也算失败
            probe = d / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return d
        except Exception:
            continue
    return None


def _setup_logging() -> None:
    """日志**总是**落盘（轮转 5MB×3）；有控制台时额外打到屏幕。"""
    import logging
    from logging.handlers import RotatingFileHandler

    fmt = "%(asctime)s %(levelname)s %(name)s | %(message)s"
    handlers: list[logging.Handler] = []

    log_dir = _log_dir()
    if log_dir is not None:
        try:
            handlers.append(
                RotatingFileHandler(log_dir / "bridge.log", maxBytes=5 * 1024 * 1024,
                                    backupCount=3, encoding="utf-8")
            )
        except Exception:
            pass

    if _has_console():
        handlers.append(logging.StreamHandler())

    if handlers:
        logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _alert(msg: str, title: str = "SinoCC PDM Bridge", error: bool = False) -> None:
    """双击场景的兜底提示：控制台会随进程消失，只有弹框留得住信息。"""
    if not _interactive():
        return
    try:
        import ctypes

        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, msg, title, flags)
    except Exception:
        pass


def _environment_snapshot() -> str:
    """一行摘要 + 多行明细，用于异地机器排障（回传日志即可定位系统/架构问题）。"""
    import platform

    lines = []
    try:
        ver = sys.getwindowsversion()
        lines.append(f"windows={ver.major}.{ver.minor}.{ver.build} "
                     f"sp={getattr(ver, 'service_pack', '')!r} product_type={ver.product_type}")
    except Exception as exc:
        lines.append(f"windows=<unavailable: {exc}>")
    lines.append(f"arch={platform.machine()} pointer={platform.architecture()[0]} "
                 f"python={platform.python_version()}")
    lines.append(f"frozen={getattr(sys, 'frozen', False)} meipass={getattr(sys, '_MEIPASS', '')}")
    lines.append(f"exe={sys.executable}")
    lines.append(f"cwd={os.getcwd()}")
    lines.append(f"temp={tempfile.gettempdir()} logdir={_log_dir()}")
    return "\n".join(lines)


def _write_crash_log(title: str, detail: str) -> Path | None:
    log_dir = _log_dir()
    if log_dir is None:
        return None
    try:
        path = log_dir / "crash.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {title} =====\n")
            fh.write(_environment_snapshot() + "\n")
            fh.write(detail.rstrip() + "\n")
        return path
    except Exception:
        return None


_REPORTED = False


def _fatal(title: str, detail: str) -> None:
    """启动失败：落盘 + 屏幕 + 弹框，三处都留痕。绝不静默退出。

    幂等：同一个进程只报一次（main 里报过之后抛 SystemExit，外层处理器会再进来）。
    """
    global _REPORTED
    if _REPORTED:
        return
    _REPORTED = True
    path = _write_crash_log(title, detail)
    _say(f"[bridge] {title}")
    _say(detail.rstrip())
    if path is not None:
        _say(f"[bridge] 详细信息已写入: {path}")
    _alert(f"{title}\n\n{detail.strip()[:1500]}\n\n"
           f"日志：{path if path else '(无法写入日志目录)'}", title="SinoCC PDM Bridge — 启动失败",
           error=True)


def _port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection((HOST, port), timeout=0.4):
            return True
    except Exception:
        return False


def _is_our_bridge(port: int) -> bool:
    """端口上跑的是不是本程序？用 /ui-version 的 JSON 签名判定（而不是「端口开着=我」）。"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/ui-version", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return bool(data.get("ok")) and isinstance(data.get("build"), str)
    except Exception:
        return False


def _resolve_port(preferred: int) -> tuple[int, str]:
    """返回 (端口, 处置)：free=直接可用 / self=本程序的另一个实例 / shifted / none。"""
    if not _port_open(preferred):
        return preferred, "free"
    if _is_our_bridge(preferred):
        return preferred, "self"
    # 被别的程序占用：旧行为是打印一句就退出（用户看到的就是「闪退」），
    # 这里改为顺延到下一个空闲端口继续启动。
    for cand in range(preferred + 1, preferred + 1 + PORT_SCAN):
        if not _port_open(cand):
            return cand, "shifted"
    return 0, "none"


def _open_browser() -> None:
    if os.environ.get("PDM_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(f"http://{HOST}:{PORT}/")
    except Exception:
        pass


def _open_browser_later() -> None:
    # 验收/自动化场景（PDM_NO_BROWSER=1）不弹浏览器。
    if os.environ.get("PDM_NO_BROWSER") == "1":
        return

    def _open() -> None:
        # onefile 打包首启要先解包到临时目录（3-8s），固定 sleep 容易撞上
        # 「无法连接」→ 改为轮询端口，就绪后再开浏览器（最多等 60s）。
        import socket

        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                with socket.create_connection((HOST, PORT), timeout=0.5):
                    break
            except Exception:
                time.sleep(0.3)
        _open_browser()

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    global PORT

    _clear_proxies()
    _load_portable_env()
    _setup_logging()

    snapshot = _environment_snapshot()
    _log("launcher environment | " + snapshot.replace("\n", " | "))
    _say(f"[bridge] {snapshot.splitlines()[0]}")
    _say(f"[bridge] {snapshot.splitlines()[1]}")

    # 原生崩溃（如 DLL 加载失败后段错误）也留痕
    try:
        import faulthandler

        d = _log_dir()
        if d is not None:
            faulthandler.enable(open(d / "native-crash.log", "ab"))
    except Exception:
        pass

    port, action = _resolve_port(PORT)

    if action == "self":
        msg = (f"端口 {PORT} 已经是本程序在监听——不用再开一个。\n\n"
               f"即将为你打开 http://{HOST}:{PORT}/\n\n"
               f"若确实要再起一个实例，请先设置环境变量 PDM_PORT=<其它端口> 再运行。")
        _say("[bridge] " + msg.replace("\n\n", "\n[bridge] "))
        _alert(msg)
        _open_browser()
        time.sleep(2.5)  # 让双击启动的用户能看清提示
        return

    if action == "none":
        _fatal("端口全部被占用",
               f"{HOST}:{PORT}..{PORT + PORT_SCAN} 之间没有可用端口。\n"
               f"请用 netstat -ano | findstr :{PORT} 查看占用者，"
               f"或设置 PDM_PORT=<空闲端口> 再运行。")
        raise SystemExit(1)

    if action == "shifted":
        _log(f"port {PORT} occupied by another program -> fallback to {port}")
        _say(f"[bridge] 端口 {PORT} 被其它程序占用，已自动改用 {port}。"
             f"（想固定端口可设 PDM_PORT=<空闲端口>）")
        PORT = port

    # MCP 只读代理工具（app/mcp_server.py 模块级读 BRIDGE_BASE）必须指向本进程，
    # 否则换端口（PDM_PORT）后 MCP 会代理到别的实例。.env 显式配了则以 .env 为准。
    os.environ.setdefault("PDM_BRIDGE_URL", f"http://{HOST}:{PORT}")

    import uvicorn

    from app import app as fastapi_app

    _log(f"serving on http://{HOST}:{PORT}/ (frozen={getattr(sys, 'frozen', False)})")
    _say(f"[bridge] UI -> http://{HOST}:{PORT}/   (frozen={getattr(sys, 'frozen', False)})")
    _open_browser_later()
    uvicorn.run(fastapi_app, host=BIND_HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:  # uvicorn 绑定失败时是 sys.exit(1)——别让它变成「闪一下没了」
        code = exc.code
        if code not in (None, 0):
            _fatal("服务启动失败（SystemExit）",
                   f"退出码 {code!r}\n\n" + traceback.format_exc())
            raise SystemExit(code)
        raise
    except BaseException:
        _fatal("启动失败（未捕获异常）", traceback.format_exc())
        raise SystemExit(1)
