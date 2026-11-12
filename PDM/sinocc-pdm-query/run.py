"""SinoCC PDM 查询工具 —— 启动器（单实例 + 整体式窗口）。

行为：
  1. 默认监听 127.0.0.1:9130（环境变量 PDMQ_PORT 可改）
  2. 端口上若已是本程序（/api/health 标记匹配）→ 直接用已有实例，只开窗口
  3. 否则后台线程起 uvicorn，健康检查通过后开窗口：
       · 装了 pywebview → 原生窗口（无浏览器地址栏，最像"软件"）
       · 否则用 Chrome/Edge 的 --app= 无边框窗口（专用 profile，不含标签页/地址栏）
       · 都没有 → 系统默认浏览器
  4. 窗口模式下不弹黑框；日志一律落 %LOCALAPPDATA%\\SinoCC-PDM-Query\\logs\\app.log
  5. 托盘/关窗口不退出（服务常驻，再次双击只会重开窗口）；UI 顶栏「退出程序」才真正结束

用法：python run.py [--port 9130] [--no-window] [--browser]
"""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_TITLE = "SinoCC PDM 查询工具"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

log = logging.getLogger("pdmq.run")


# ---------------------------------------------------------------- 环境
class _NullIO:
    """windowed 打包（console=False）下 sys.stdout/stderr 为 None，print 会炸 → 兜一个黑洞。

    ⚠ 2026-11-09 实测事故：uvicorn 的 ColourFormatter 在 use_colors=None 时会调
    sys.stdout.isatty()（uvicorn/logging.py），_NullIO 缺这个方法 → uvicorn 线程崩、
    服务起不来、健康检查超时（另一台电脑双击 exe 复现；本机冒烟因 DEVNULL 句柄掩盖）。
    所以必须补齐 isatty 等文件对象接口。
    """

    def write(self, *_a, **_k):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def close(self):
        pass

    @property
    def closed(self):
        return False

    @property
    def encoding(self):
        return "utf-8"

    @property
    def name(self):
        return "<null>"


def _fix_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = _NullIO()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullIO()  # type: ignore[assignment]


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "SinoCC-PDM-Query" / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "app.log"


def _setup_logging() -> None:
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s | %(message)s",
            handlers=[logging.FileHandler(_log_path(), encoding="utf-8")],
        )
    except Exception:
        pass


def _clear_proxies() -> None:
    """清环境变量代理：httpx 默认 read_env，公司代理不通时会让 PDM 请求全部失败。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)


# ---------------------------------------------------------------- 服务
def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_our_app(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.2) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("app") == "sinocc-pdm-query"
    except Exception:
        return False


def _wait_ready(port: int, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _is_our_app(port):
            return True
        time.sleep(0.25)
    return False


def _start_server(port: int) -> None:
    """在子线程跑 uvicorn（主线程留给窗口消息循环）。"""
    import uvicorn
    from pdmq import create_app

    app = create_app()

    def _run():
        try:
            # log_config=None：跳过 uvicorn 的 dictConfig（彩色 formatter 会调 sys.stdout.isatty()，
            # 无控制台句柄的环境下不可靠）。日志统一走根 logger → app.log。
            uvicorn.run(app, host="127.0.0.1", port=port,
                        log_config=None, access_log=False)
        except Exception as e:  # noqa: BLE001
            log.exception("uvicorn 退出：%s", e)

    th = threading.Thread(target=_run, name="uvicorn", daemon=True)
    th.start()


# ---------------------------------------------------------------- 窗口
class JsApi:
    """暴露给前端 JS 的文件落盘桥。

    ⚠ 2026-11-10 实测：pywebview（WebView2 原生窗口）**不处理 `<a download>` 的 blob 下载**，
    前端「导出 EXCEL」只弹 toast、文件不落盘。前端检测到 window.pywebview 时改走本方法，
    由 Python 把文件写到用户「下载」目录并返回完整路径。
    """

    def save_file(self, filename: str, data_b64: str) -> str:
        import base64

        safe = os.path.basename(filename or "export.xls").replace("..", "_")
        try:
            data = base64.b64decode(data_b64 or "")
        except Exception:  # noqa: BLE001
            return ""
        log.info("JS 请求落盘：%s（%d bytes）", safe, len(data))
        candidates = [
            Path.home() / "Downloads",
            Path.home(),
            Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "SinoCC-PDM-Query" / "exports",
        ]
        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                p = d / safe
                p.write_bytes(data)
                log.info("已保存：%s", p)
                return str(p)
            except Exception as e:  # noqa: BLE001
                log.warning("落盘目录 %s 失败：%s", d, e)
        return ""


def _user_data_dir() -> Path:
    """浏览器专用 profile 目录；LOCALAPPDATA 不可写时退回 %TEMP%。"""
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("TEMP"), None):
        try:
            p = Path(base or str(ROOT)) / "SinoCC-PDM-Query" / "uinow"
            p.mkdir(parents=True, exist_ok=True)
            # 验证真的可写（只建文件不删，避免被删除守护误伤）
            (p / ".probe").write_text("ok")
            return p
        except Exception:  # noqa: BLE001
            continue
    return Path(os.environ.get("TEMP", ".")) / "SinoCC-PDM-Query-uinow"


def _browser_exes() -> list:
    """按优先级找本机 Chrome/Edge：固定路径 → 用户级安装 → 注册表 App Paths。"""
    cands = list(CHROME_CANDIDATES)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands += [
            Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    try:
        import winreg

        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        v = winreg.QueryValueEx(k, None)[0]
                        if v:
                            cands.append(v)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass

    out, seen = [], set()
    for c in cands:
        try:
            p = Path(c)
        except Exception:  # noqa: BLE001
            continue
        if str(p).lower() in seen:
            continue
        seen.add(str(p).lower())
        if p.is_file():
            out.append(str(p))
    return out


def _msgbox(url: str, detail: str = "") -> None:
    """所有窗口方式都失败时的最后兜底：至少告诉用户服务还活着、地址是什么。"""
    log.error("所有窗口方式均失败：%s", detail)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            f"服务已在后台运行：\n\n{url}\n\n"
            f"未找到可用的浏览器窗口（{detail or '原因见日志'}）。\n"
            "请手动打开浏览器访问上面的地址；日志：%LOCALAPPDATA%\\SinoCC-PDM-Query\\logs\\app.log",
            APP_TITLE,
            0x40,
        )
    except Exception:  # noqa: BLE001
        pass


def _open_window(url: str, use_browser: bool = False) -> bool:
    """打开整体式窗口。返回 True 表示用了原生窗口（会阻塞到关闭）。

    降级链：pywebview → Chrome/Edge --app（路径/注册表探测）→ os.startfile
    （系统默认浏览器）→ webbrowser → MessageBox 提示。每一步都写日志。
    """
    if not use_browser:
        try:
            import webview  # type: ignore

            # WebView2 允许导航类下载（双保险；blob <a download> 仍靠 JsApi.save_file 落盘）
            try:
                webview.settings["ALLOW_DOWNLOADS"] = True
            except Exception:  # noqa: BLE001
                pass
            log.info("pywebview 可用（%s），创建原生窗口", getattr(webview, "__version__", "?"))
            webview.create_window(APP_TITLE, url, width=1360, height=860,
                                  min_size=(1080, 680), text_select=True, js_api=JsApi())
            webview.start()
            return True
        except ImportError:
            log.info("pywebview 不可用（未捆绑），走浏览器 --app 窗口")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "WebView2" in msg or "webview2" in msg:
                log.warning("pywebview 失败：目标机缺 WebView2 Runtime → 降级浏览器 --app（可装 "
                            "https://developer.microsoft.com/microsoft-edge/webview2/ 后恢复原生窗口）")
            else:
                log.warning("pywebview 窗口失败（%s: %s），降级浏览器 --app", type(e).__name__, e)

    forced = os.environ.get("PDMQ_BROWSER", "").strip()
    exes = ([forced] if forced else []) + _browser_exes()
    if forced:
        log.info("PDMQ_BROWSER 强制指定：%s", forced)
    log.info("候选浏览器：%s", exes or "（无）")
    profile = _user_data_dir()
    for exe in exes:
        try:
            log.info("尝试浏览器窗口：%s（profile=%s）", exe, profile)
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}",
                              "--no-first-run", "--no-default-browser-check",
                              "--window-size=1360,860", "--disable-features=Translate"])
            log.info("浏览器窗口已拉起：%s", exe)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("拉起 %s 失败：%s: %s", exe, type(e).__name__, e)

    for opener in ("os.startfile", "webbrowser"):
        try:
            if opener == "os.startfile":
                log.info("尝试系统默认浏览器（os.startfile）")
                os.startfile(url)  # noqa: S606
            else:
                import webbrowser

                log.info("尝试 webbrowser.open")
                webbrowser.open(url)
            log.info("默认浏览器打开成功（%s）", opener)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("%s 失败：%s: %s", opener, type(e).__name__, e)

    _msgbox(url, detail="无 Chrome/Edge、默认浏览器也无法拉起")
    return False


# ---------------------------------------------------------------- main
def main() -> int:
    _fix_stdio()
    multiprocessing.freeze_support()
    _setup_logging()

    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PDMQ_PORT", "9130")))
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口（调试/探针用）")
    ap.add_argument("--browser", action="store_true", help="强制用浏览器 --app 窗口（不用 pywebview）")
    ap.add_argument("--check-window", action="store_true",
                    help="只检查 pywebview/浏览器可用性并写日志后退出（排障用）")
    args = ap.parse_args()

    if args.check_window:
        try:
            import webview  # type: ignore

            log.info("check-window: pywebview %s 可用", getattr(webview, "__version__", "?"))
            print("pywebview OK", getattr(webview, "__version__", "?"))
        except Exception as e:  # noqa: BLE001
            log.warning("check-window: pywebview 不可用：%s: %s", type(e).__name__, e)
            print("pywebview MISSING:", type(e).__name__, e)
        exes = _browser_exes()
        log.info("check-window: 浏览器候选 %s", exes)
        print("browsers:", exes)
        return 0

    _clear_proxies()
    port = args.port
    url = f"http://127.0.0.1:{port}/"

    if _port_busy(port):
        if _is_our_app(port):
            log.info("已在运行，复用现有实例：%s", url)
            print(f"已在运行：{url}")
            if not args.no_window:
                _open_window(url, args.browser)
            return 0
        log.error("端口 %s 被其它程序占用", port)
        print(f"端口 {port} 被其它程序占用，换一个：--port 9131")
        return 2

    _start_server(port)
    if not _wait_ready(port):
        log.error("服务启动失败：健康检查超时")
        print("服务启动失败：健康检查超时")
        return 1
    log.info("服务就绪：%s", url)
    print(f"{APP_TITLE} 已启动：{url}")

    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    native = _open_window(url, args.browser)
    if native:
        return 0
    print("已打开应用窗口；窗口关闭后服务仍在（再次启动只会重开窗口）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
