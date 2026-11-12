"""Detect a live SinoCC PDM session (loginUID + username) from the local client.

2026-09-02 重写（两次登录态实证复现 2/2，跨 pid 跨 loginUID）：
  - loginUID  ：进程内存正则 ``CHIGOPDM.a.{GUID}...uacpc``        → 稳定
  - 用户名    ：进程内存会话串 ``|{ent};{name};{n};104|`` (GBK)   → 稳定、全内存唯一
  - 已废弃方法：GBK 中文词频猜人名（实测产出「理像/鬃颜/等线」全乱码）

服务端真伪验证器（仅读，不在本模块）：
  - ``vaultman.jsp cpcworkspace selectall``(wstype=4, modid=12, userid=candidate)
    → 本人返回 ``<wsname>{candidate}的工作区</wsname>``，他人/不存在只返回「回收站」；
    且与 loginUID 会话绑定（唐阳 token 查曾祥志也只有回收站），可当真伪判别。
"""
from __future__ import annotations

import ctypes
import os
import re
import sys

# ---- 模式 ----
# loginUID 形态（稳定命中，每次登录变化；同值在多处出现）
LOGINUID_RE = re.compile(
    rb"CHIGOPDM\.a\.\{[A-Za-z0-9_\-]+(?:\{[^}]+\}[A-Za-z0-9_\-]*)?\}(?:\.[A-Za-z0-9_]+)*"
)

# 会话身份串（GBK），2026-09-02 两次登录态复现实证：
#   |CHIGOCAC;唐阳;855020;104|   →   |CHIGOCAC;唐阳;6229454;104|
#   |{entcode};{用户名};{会话变量};{尾段}|
# - 用户名（group 2）：跨登录稳定 → 真实用户名
# - 第 3 段：每次登录都变 → 会话变量，**不要当 sysuserid**
# - 尾段：当前观测恒为 104（标记，跨登录稳定）
SESSION_TAG_RE = re.compile(
    rb"\|([A-Za-z0-9_]{2,20});([^\x00|;\r\n]{1,32});(\d{1,12});([^\x00|;\r\n]{1,16})\|"
)

# SinoCC 客户端进程名（小写，精确匹配优先）
TARGET_PROCS = {
    "sinocc.exe",
    "sinocc_cef_shell.exe",
    "cpcshell.exe",
    "cpcshell2.exe",
    "sinoccshell.exe",
    "sinocepdm.exe",
}

# ★ 自身进程排除（2026-09-07 修复扫描超时根因）：
# 打包后的程序叫 ``sinocc-pdm-bridge.exe`` / ``SinoCC-PDM-Query.exe``，会命中下面的
# ``startswith("sinocc")`` 前缀规则 → 被当成客户端扫（实测白花 11.9s），且自身内存里
# 残留的 loginUID 会污染候选。必须同时排除：当前 pid、已知本程序 exe 名、名字带标记的进程。
# 2026-11-09：独立查询工具 SinoCC-PDM-Query 复用本模块 → 加 "query" 标记与自身 exe 名。
SELF_EXE_NAMES = {"sinocc-pdm-bridge.exe", "sinocepdmbridge.exe", "pdm-bridge.exe",
                  "sinocc-pdm-query.exe", "sinocepdmquery.exe", "pdm-query.exe"}
SELF_NAME_MARKERS = ("bridge", "query")

# Windows API
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
# 可读位：PAGE_READONLY(0x02) | PAGE_READWRITE(0x04) | PAGE_EXECUTE_READ(0x20) | PAGE_EXECUTE_READWRITE(0x40)
PROTECT_R_MASK = 0xE6


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_size_t),
        ("AllocationBase", ctypes.c_size_t),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def _is_self_process(pid: int, name: str) -> bool:
    """桥接自身进程判定（含打包 exe / 源码启动两种形态）。"""
    if pid == os.getpid():
        return True
    if name in SELF_EXE_NAMES:
        return True
    return any(mk in name for mk in SELF_NAME_MARKERS)


def _enum_clients():
    """Yield (pid, exe_name_lower) for likely PDM client processes.

    排序：精确命中 TARGET_PROCS 在前（真客户端优先扫），前缀命中在后（CEF 之类的
    子进程内存大、命中率低），配合调用方的早停可把实测耗时从 ~17s 压到 ~1s。
    """
    try:
        import psutil
    except ImportError:
        return
    exact: list[tuple[int, str]] = []
    prefixed: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = (p.info["name"] or "").lower()
            pid = p.info["pid"]
        except Exception:
            continue
        if not n or not pid:
            continue
        if _is_self_process(pid, n):
            continue
        if n in TARGET_PROCS:
            exact.append((pid, n))
        elif n.startswith("sinocc") or n.startswith("cpcshell"):
            prefixed.append((pid, n))
    exact.sort()
    prefixed.sort()
    for item in exact + prefixed:
        yield item


def _scan_process(pid: int, uids_out: set, tags_out: list, *,
                  max_regions: int = 600, max_region_size: int = 12 * 1024 * 1024,
                  chunk_size: int = 4 * 1024 * 1024, stop=None) -> int:
    """单次遍历进程可读已提交内存区域，同时收集 loginUID 与会话身份串。

    2026-09-07 性能修复（实测 sinocc.exe 4.83s / 桥接自身 11.91s → 优化后秒级）：
      - 大 region 按 ``chunk_size`` 分块读（保留 256B 重叠，避免锚点跨块被切断）；
      - 每块先做 ``bytes in`` 粗筛（C 级），绝大多数块直接跳过正则；
      - ``stop()`` 命中即早停，不再扫完 600 个 region。

    Returns 该进程扫描的 region 数。
    """
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return 0
    k.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.ReadProcessMemory.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k.ReadProcessMemory.restype = ctypes.c_bool

    addr = 0
    regions = 0
    try:
        for _ in range(max_regions * 4):
            mbi = _MBI()
            qs = k.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
            if qs == 0:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            next_addr = base + size if size else addr + 0x1000
            if next_addr <= addr:
                next_addr = addr + 0x1000
            addr = next_addr
            if addr > 0x7FFF_FFFF_FFFF:
                break
            if regions >= max_regions:
                break
            if not size or size < 256 or size > max_region_size:
                continue
            if mbi.State != MEM_COMMIT or not (mbi.Protect & PROTECT_R_MASK):
                continue
            regions += 1
            try:
                prev_tail = b""
                off = 0
                while off < size:
                    want = min(chunk_size, size - off)
                    buf = (ctypes.c_ubyte * want)()
                    got = ctypes.c_size_t(0)
                    ok_read = k.ReadProcessMemory(
                        h, base + off, ctypes.byref(buf), want, ctypes.byref(got))
                    if not (ok_read and got.value):
                        break
                    chunk = prev_tail + bytes(buf[: int(got.value)])
                    prev_tail = chunk[-256:]
                    off += want
                    # 粗筛：C 级 bytes 扫描，绝大多数块直接跳过正则（主要提速点）
                    if b"CHIGOPDM" in chunk:
                        for m in LOGINUID_RE.finditer(chunk):
                            u = m.group().decode("ascii", errors="ignore")
                            uids_out.add(u)
                    if b"|" in chunk:
                        for m in SESSION_TAG_RE.finditer(chunk):
                            raw = m.group()
                            try:
                                decoded = raw.decode("gbk")
                            except Exception:
                                continue
                            if decoded not in tags_out:
                                tags_out.append(decoded)
                    if stop is not None and stop():
                        return regions
            except Exception:
                pass
    finally:
        k.CloseHandle(h)
    return regions


def _make_stop(uids: set, tags: list, entcode: str, need_tag: bool):
    """早停判定：拿到 loginUID（且需要时再拿到匹配 entcode 的会话串）就停。

    ``need_tag=False`` 时只要命中 loginUID 即可停（detect_login_uid 向后兼容路径）。
    """
    seen: set = set()

    def _stop() -> bool:
        if not uids:
            return False
        if not need_tag:
            return True
        for raw in tags:
            if raw in seen:
                continue
            seen.add(raw)
            t = _extract_tag(raw)
            if t and t["entcode"] == entcode:
                return True
        return False

    return _stop


# ---- 公共入口 ----

def detect_login_uid() -> dict:
    """扫所有目标进程，返回 loginUID 候选列表（向后兼容旧调用方）。

    Returns: ``{ok, login_uid, sources:[{login_uid,source},...], scanned, tags, reason?}``
    """
    if sys.platform != "win32":
        return {"ok": False, "reason": "auto-detect only supported on Windows"}
    uids: set = set()
    tags: list = []
    scanned: list[tuple[int, str]] = []
    try:
        for pid, name in _enum_clients():
            scanned.append((pid, name))
            try:
                _scan_process(pid, uids, tags, stop=_make_stop(uids, tags, "", False))
            except OSError:
                return {"ok": False, "reason": "permission denied (run bridge as Administrator)",
                        "candidates_so_far": sorted(uids)}
            if uids:
                break
    except Exception as e:
        return {"ok": False, "reason": f"enum failed: {type(e).__name__}: {e}"}
    if not uids:
        return {"ok": False,
                "reason": "未检测到 SinoCC 客户端会话（请确认客户端已登录且进程仍在运行）",
                "scanned": scanned, "tags": tags}
    first_pid = scanned[0][0] if scanned else 0
    sources = [{"login_uid": u, "source": f"pid={first_pid}"} for u in sorted(uids)]
    return {
        "ok": True,
        "login_uid": sources[0]["login_uid"],
        "sources": sources,
        "scanned": scanned,
        "tags": tags,
    }


def _extract_tag(decoded: str) -> dict | None:
    """将已 GBK 解码的 ``|ent;name;var;marker|`` 解析为 dict。"""
    if not (decoded.startswith("|") and decoded.endswith("|")):
        return None
    parts = decoded[1:-1].split(";")
    if len(parts) != 4:
        return None
    ent, name, var, marker = (p.strip() for p in parts)
    if not (ent and name and var and marker):
        return None
    if not (var.isdigit() and marker.isdigit()):
        return None
    if not (1 <= len(name) <= 32):
        return None
    if any(c in name for c in ("\\", "/", ":", "*", "?", '"', "<", ">", "|", ",", ";", " ", "\t", "\n", "\r")):
        return None
    return {"entcode": ent, "userid": name, "session_var": var, "marker": marker}


def detect_session_tag(entcode: str = "CHIGOCAC") -> dict | None:
    """扫所有进程，返回第一个匹配 entcode 的会话身份串解析结果。"""
    if sys.platform != "win32":
        return None
    uids: set = set()
    tags: list = []
    try:
        for pid, name in _enum_clients():
            try:
                _scan_process(pid, uids, tags,
                              stop=_make_stop(uids, tags, entcode, True))
            except OSError:
                continue
            # 命中目标 entcode 的会话串即停
            for raw in tags:
                t = _extract_tag(raw)
                if t and t["entcode"] == entcode:
                    return t
    except Exception:
        return None
    for raw in tags:
        t = _extract_tag(raw)
        if t:
            return t
    return None


def detect_identity(entcode: str = "CHIGOCAC") -> dict:
    """一次扫描同时拿 loginUID + 用户名（2026-09-02 实证 2/2）。

    Returns::

        {
          "ok": bool,
          "login_uid": str,
          "userid": str,        # 用户名（已 _extract_tag 解析）
          "entcode": str,
          "session_var": str,   # 第 3 段（**不要当 sysuserid**，仅供调试）
          "marker": str,        # 尾段（当前恒 104）
          "scanned": [(pid, name), ...],
          "tags": [...],        # 原始会话身份串
          "reason": str?,
        }
    """
    if sys.platform != "win32":
        return {"ok": False, "reason": "auto-detect only supported on Windows"}
    uids: set = set()
    tags: list = []
    scanned: list = []
    try:
        for pid, name in _enum_clients():
            scanned.append((pid, name))
            try:
                _scan_process(pid, uids, tags,
                              stop=_make_stop(uids, tags, entcode, True))
            except OSError:
                return {"ok": False, "reason": "permission denied (run bridge as Administrator)",
                        "scanned": scanned}
            # 已同时拿到 loginUID + 目标 entcode 会话串 → 停
            if uids and any((_extract_tag(r) or {}).get("entcode") == entcode for r in tags):
                break
    except Exception as e:
        return {"ok": False, "reason": f"enum failed: {type(e).__name__}: {e}",
                "scanned": scanned}

    parsed = None
    for raw in tags:
        t = _extract_tag(raw)
        if t and t["entcode"] == entcode:
            parsed = t
            break
    if parsed is None:
        for raw in tags:
            t = _extract_tag(raw)
            if t:
                parsed = t
                break

    out = {
        "ok": bool(uids or parsed),
        "login_uid": next(iter(sorted(uids)), "") if uids else "",
        "scanned": scanned,
        "tags": tags,
    }
    if parsed:
        out.update({
            "userid": parsed["userid"],
            "entcode": parsed["entcode"],
            "session_var": parsed["session_var"],
            "marker": parsed["marker"],
        })
    if not (uids or parsed):
        out["reason"] = "未检测到 SinoCC 客户端会话（请确认客户端已登录且进程仍在运行）"
    return out


def detect_username_from_files(entcode: str = "CHIGOCAC") -> list[dict]:
    """【弱信号·仅辅助】扫 ``C:\\sinocchome`` 下用户名目录。

    2026-09-02 实证：本地 vault 目录会被重新登录清空 → **不能**作为主判据。
    返回按 mtime 降序的候选列表。空表示「客户端未运行 / 刚清空」。
    """
    import os
    roots = [r"C:\sinocchome"]
    skip = {"回收站", "temp", "tmp", "cache", "logs", "log", "backup", "bak", "system",
            "public", "shared", "share", "trash", "recycle", "data", "files"}
    out: list[dict] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            d1_list = os.listdir(root)
        except OSError:
            continue
        for d1 in d1_list:
            p1 = os.path.join(root, d1)
            if not os.path.isdir(p1):
                continue
            try:
                d2_list = os.listdir(p1)
            except OSError:
                continue
            for d2 in d2_list:
                p2 = os.path.join(p1, d2)
                if not os.path.isdir(p2):
                    continue
                name = d2.strip()
                if not name or name.startswith(".") or name.lower() in skip:
                    continue
                if any(c in name for c in ("\\", "/", ":", "*", "?", '"', "<", ">", "|")):
                    continue
                try:
                    mt = os.path.getmtime(p2)
                except OSError:
                    continue
                out.append({"name": name, "path": p2, "mtime": mt, "root": d1})
    out.sort(key=lambda x: -x["mtime"])
    return out
