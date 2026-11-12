# -*- coding: utf-8 -*-
r"""CADCraft 集成：DWG/DXF 图纸查看（渲染出图）。

调用方式：subprocess 驱动 `cadcraft-cli`（storytold/cadcraft v0.6.2+，纯 Rust，
MIT OR Apache-2.0，DWG R13–2018 读取经开源 acadrust 库）。

CLI 探测顺序（找到第一个存在的为准）：
  1. 环境变量 CADCRAFT_CLI（显式指定 cadcraft-cli.exe 全路径）
  2. <项目根>/vendor/cadcraft/cadcraft-*-portable/cadcraft-cli.exe
     （frozen 部署时：exe 同级 vendor/ 可热替换新版，优先于内嵌）
  3. spec 内嵌（frozen）：sys._MEIPASS/vendor/... → 首次探测时拷到
     %LOCALAPPDATA%\SinoCC-PDM-Bridge\cadcraft\<portable 目录名>\ 稳定目录再用
     （_MEI 解包目录会被企业终端 agent 批量清理，不能直接从那里跑）
  4. PATH 上的 cadcraft-cli

边界：MCP 是 JSON 文本通道，图片一律落盘返回 path（与 pdm_obj_save 同模式），
不在 MCP 响应里回传二进制。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("sinocc.bridge.cadcraft")

# CLI 探测结果缓存（None=尚未探测；False=确认不存在）
_cli_cache: Optional[str] = None

# CLI 版本元组缓存（None=尚未探测）；(0,0,0)=探测失败按最老版对待
_cli_version: Optional[tuple] = None

# 单次 CLI 调用上限（大图纸渲染可能要几十秒）
CLI_TIMEOUT_S = 180


def _version_tuple_of(path: str) -> tuple:
    """从路径（portable 目录名 cadcraft-X.Y.Z-*）提取版本元组；解析不出 → (0,0,0)。

    不能用字典序排序：'0.17.0' < '0.6.2' 按字符串比较（'1'<'6'），
    hits[-1] 会误选旧版（2026-11-04 实测踩坑）。
    """
    import re
    m = re.search(r"cadcraft-(\d+)\.(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def cli_version() -> tuple:
    """查询 CLI 版本元组（`cadcraft-cli --version` → "cadcraft-cli 0.17.0 (sha)"）。

    缓存一次；0.17.0 起 CLI 换 clap 解析，`--window` 负值必须用等号写法，
    旧版只认分开写 → 传参形态按此版本分流。
    """
    global _cli_version
    if _cli_version is not None:
        return _cli_version
    import re
    try:
        exe = find_cli()
        p = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (p.stdout or "") + (p.stderr or ""))
        _cli_version = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    except Exception:  # noqa: BLE001
        _cli_version = (0, 0, 0)
    return _cli_version


def _vendor_globs() -> list[str]:
    """vendor 目录候选（源码态 = 项目根；frozen = exe 同级 + _MEIPASS 内嵌）。"""
    pats: list[str] = []
    here = Path(__file__).resolve().parent.parent  # 项目根（源码态）
    pats.append(str(here / "vendor" / "cadcraft" / "cadcraft-*-portable" / "cadcraft-cli.exe"))
    if getattr(sys, "frozen", False):  # 打包 exe：exe 同级 vendor/（热替换优先）+ spec 内嵌
        base = Path(sys.executable).resolve().parent
        pats.append(str(base / "vendor" / "cadcraft" / "cadcraft-*-portable" / "cadcraft-cli.exe"))
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            pats.append(str(Path(meipass) / "vendor" / "cadcraft" / "cadcraft-*-portable" / "cadcraft-cli.exe"))
    return pats


def _stabilize_mei(src: Path) -> str:
    """从 _MEI 解包目录拷到稳定目录再返回（企业终端 agent 会批量清 %TEMP%\\_MEI*）。

    注意不能用字符串前缀比对：sys._MEIPASS 可能是 8.3 短路径（TANGYA~1），
    而 Path(__file__).resolve() 出来是长路径（tangyang1）→ 改用
    resolve 后 is_relative_to + 任意路径段 _MEI 前缀双判据。
    """
    src = Path(src)
    meipass = getattr(sys, "_MEIPASS", "")
    in_mei = any(p.lower().startswith("_mei") for p in src.parts)
    if getattr(sys, "frozen", False) and meipass and not in_mei:
        try:
            in_mei = src.resolve().is_relative_to(Path(meipass).resolve())
        except Exception:  # noqa: BLE001
            pass
    if not (getattr(sys, "frozen", False) and in_mei):
        return str(src)
    dst_dir = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
               / "SinoCC-PDM-Bridge" / "cadcraft" / src.parent.name)
    dst = dst_dir / src.name
    if not (dst.is_file() and dst.stat().st_size == src.stat().st_size):
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log.info("cadcraft-cli 已从 _MEI 固化到 %s", dst)
    return str(dst)


def find_cli() -> str:
    """定位 cadcraft-cli.exe；找不到抛 FileNotFoundError。"""
    global _cli_cache
    if _cli_cache and Path(_cli_cache).is_file():
        return _cli_cache
    _cli_cache = None  # 缓存路径已失效（如被终端清理）→ 重新探测
    env = os.environ.get("CADCRAFT_CLI", "").strip()
    if env and Path(env).is_file():
        _cli_cache = env
        return env
    import glob
    all_hits: list[str] = []
    for pat in _vendor_globs():
        all_hits.extend(glob.glob(pat))
    if all_hits:
        # 按 portable 目录名里的版本号取最大（字典序会把 0.17.0 排在 0.6.2 前面）
        best = max(all_hits, key=_version_tuple_of)
        _cli_cache = _stabilize_mei(Path(best))  # _MEI 内嵌先固化
        return _cli_cache
    which = shutil.which("cadcraft-cli")
    if which:
        _cli_cache = which
        return which
    raise FileNotFoundError(
        "cadcraft-cli 未找到：请解压 cadcraft-*-windows-*-portable.zip 到 "
        "exe 同级 vendor/cadcraft/（或设置环境变量 CADCRAFT_CLI 指向 cadcraft-cli.exe）")


def _run_cli(args: list[str]) -> tuple[int, str]:
    """同步跑 CLI（在线程池里等，不卡事件循环）。返回 (returncode, stdout)。"""
    exe = find_cli()
    cmd = [exe] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_S,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return 124, f"cadcraft-cli 超时（>{CLI_TIMEOUT_S}s）"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


async def run_cli(args: list[str]) -> tuple[int, str]:
    return await asyncio.to_thread(_run_cli, args)


def _parse_window(window: Any) -> Optional[str]:
    """校验 --window 参数：4 个有限正数 → "x0,y0,x1,y1"；None/空 → None。"""
    if window in (None, "", []):
        return None
    if not isinstance(window, (list, tuple)) or len(window) != 4:
        raise ValueError("window 必须是 [x0, y0, x1, y1] 四个数字")
    nums = []
    for v in window:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("window 含非法数值")
        nums.append(f)
    return ",".join(repr(n) for n in nums)


def _check_drawing(path: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"图纸文件不存在: {path}")
    return p


async def cad_dump_entities(path: str, limit: int = 0) -> Dict[str, Any]:
    """实体清单 dump（--dump-entities 等价实现）。

    数据源：cadcraft-cli `run <file> --cmd 'drawing.inspect {"entities": true}'`
    原生输出（2026-10-15 实证）：每个实体含 type/layer/handle/geometry（坐标、
    半径、顶点、文本 value/contents 等）。CLI 本体是预编译 Rust 二进制加不了
    子命令，故在桥接层包装同名能力。
    注意：drawing.inspect 的 limit 参数默认 200 且 limit=0 表示 0 条（不是不限，
    2026-10-15 实测 entityCount=1208 / entities=200），必须显式传大 limit 才能全量。
    本函数 limit 参数为桥接层裁剪（limit>0 只返回前 limit 条，truncated=true），
    CLI 层始终请求全量，全量数据供 pdm_cad_diff 内部用。
    """
    p = _check_drawing(path)
    rc, out = await run_cli(
        ["run", str(p), "--cmd", 'drawing.inspect {"entities": true, "limit": 1000000}'])
    if rc != 0:
        return {"ok": False, "error": f"cadcraft-cli drawing.inspect 失败(rc={rc}): {out[:500]}"}
    try:
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return {"ok": False, "error": "drawing.inspect 输出非 JSON", "raw": out[:2000]}
    ents = j.get("entities") or []
    total = len(ents)
    truncated = bool(limit) and total > limit
    return {"ok": True, "path": str(p),
            "entityCount": j.get("entityCount", total),
            "layers": j.get("layers"), "extents": j.get("extents"),
            "total": total, "truncated": truncated,
            "entities": ents[:limit] if truncated else ents}


async def cad_info(path: str) -> Dict[str, Any]:
    """cadcraft-cli info <file>：实体统计/图层/extents 等 JSON。"""
    p = _check_drawing(path)
    rc, out = await run_cli(["info", str(p)])
    if rc != 0:
        return {"ok": False, "error": f"cadcraft-cli info 失败(rc={rc}): {out[:500]}"}
    try:
        return {"ok": True, "info": json.loads(out[out.index("{"):out.rindex("}") + 1])}
    except Exception:
        return {"ok": True, "info_raw": out[:4000]}  # 版本差异兜底：原样给文本


async def cad_render(path: str, out_path: str, out_format: str = "png",
                     window: Any = None) -> Dict[str, Any]:
    """cadcraft-cli convert <file> <out> [--window]：渲染为 PNG/SVG/PDF 落盘。"""
    p = _check_drawing(path)
    fmt = (out_format or "png").lower().lstrip(".")
    if fmt not in ("png", "svg", "pdf"):
        return {"ok": False, "error": f"不支持的导出格式 {fmt!r}（仅 png/svg/pdf）"}
    outp = Path(out_path)
    outp.parent.mkdir(parents=True, exist_ok=True)
    args = ["convert", str(p), str(outp)]
    try:
        w = _parse_window(window)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if w:
        # 0.17.0 换 clap 后，负值开头的独立参数被当 flag 拒收（exit 2），
        # 必须等号写法 `--window=-1,..`；旧版（≤0.6.2 实测）只认分开写
        if cli_version() >= (0, 17, 0):
            args += [f"--window={w}"]
        else:
            args += ["--window", w]
    rc, out = await run_cli(args)
    if rc != 0 or not outp.is_file():
        return {"ok": False, "error": f"cadcraft-cli convert 失败(rc={rc}): {out[:500]}"}
    return {"ok": True, "out_path": str(outp), "size": outp.stat().st_size,
            "format": fmt, "cli": " ".join(args[1:])}


async def cad_can_read(path: str) -> bool:
    """轻量探测：info 返回 0 即认为该文件可被 cadcraft 读取。"""
    try:
        r = await cad_info(path)
        return bool(r.get("ok"))
    except Exception:
        return False
