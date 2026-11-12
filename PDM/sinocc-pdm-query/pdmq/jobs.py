"""任务与进度（长查询的进度条数据源）。

为什么需要它：流程查询 / 确认单查询都要几十秒到两分钟（几百个物料 → 上千次 PDM 请求），
前端必须有**确定性进度**而不是转圈。做法：

  POST /api/query/xxx      → 立刻返回 job_id，查询在后台 asyncio 任务里跑
  GET  /api/jobs/{id}      → 轮询进度 {stage, done, total, pct, elapsed_ms, ...}
  GET  /api/jobs/{id}/result → 跑完后再取结果（避免把几百 KB 结果塞进轮询响应）

进度口径（三段式，与真实工作单元一致）：
  1) 读 BOM 多层明细            → total=1（一次请求，快）
  2) 逐物料扫关联对象           → total=去重物料数，每完成一个 tick
  3) 逐对象取流程状态/确认单明细 → total=去重对象数，每完成一个 tick
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

# 完成后保留多久（秒）与最多保留多少个 job（防内存膨胀）
JOB_KEEP_SEC = 1800.0
JOB_MAX = 30


class Job:
    """一次后台查询任务。字段都是前端进度条/日志要展示的。"""

    def __init__(self, kind: str, params: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                       # rels | samconfirms
        self.params = params or {}
        self.status = "running"                # running | done | error | cancelled
        self.stage = "准备中"
        self.stage_index = 0                   # 第几段（1..3），前端画分段进度
        self.stage_total = 3
        self.done = 0                          # 当前段已完成
        self.total = 0                         # 当前段总量（0 = 该段不确定/一次性）
        self.message = ""                      # 人类可读的细粒度说明（最近一条）
        self.log: list[str] = []               # 最近若干条阶段日志（前端滚动展示）
        self.result: Any = None
        self.error = ""
        self.t0 = time.time()
        self.t1: Optional[float] = None
        self.cancel_requested = False
        self.task = None               # asyncio.Task（停止时直接 cancel，立即生效）
        # 各段的重心权重（用于把三段合成一个 0~100 的总进度）。
        # 实测（814042400004 两层 284 行）：流程查询 段2（逐物料 selectrel）占大头；
        # 确认单查询 段3（逐张取确认单明细）最重 —— 权重偏了会让「预计剩余」明显偏低。
        self._weights = {"rels": [0.05, 0.70, 0.25],
                         "samconfirms": [0.05, 0.37, 0.58]}.get(kind, [0.12, 0.45, 0.43])

    # ---- 更新 ----
    def set_stage(self, index: int, name: str, total: int = 0, message: str = "") -> None:
        self.stage_index = index
        self.stage = name
        self.done = 0
        self.total = max(0, total)
        # 换段必须重置 message：否则新段会一直挂着上一段最后一条细粒度说明（如「xxx：2 张确认单」）
        self.message = message
        self._log(f"[{index}/{self.stage_total}] {name}" + (f"（共 {total} 项）" if total else ""))

    def tick(self, n: int = 1, message: str = "") -> None:
        self.done += n
        if message:
            self.message = message

    def set_total(self, total: int) -> None:
        self.total = max(0, total)

    def finish(self, result: Any) -> None:
        self.result = result
        self.status = "done"
        self.done = self.total or 1
        self.stage = "完成"
        self.t1 = time.time()
        self._log(f"完成，用时 {int((self.t1 - self.t0) * 1000)} ms")

    def fail(self, err: str) -> None:
        self.status = "error"
        self.error = err
        self.t1 = time.time()
        self._log(f"失败：{err}")

    def cancelled(self) -> None:
        self.status = "cancelled"
        self.error = "用户已停止"
        self.t1 = time.time()
        self._log("用户已停止")

    def _log(self, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {line}")
        if len(self.log) > 40:
            del self.log[: len(self.log) - 40]

    def note(self, line: str) -> None:
        """给前端阶段日志追加一条（不影响进度计数）。"""
        self._log(line)

    # ---- 对外快照 ----
    def _pct(self) -> float:
        """把分段进度折算成 0~100：已完成段全权重 + 当前段内插值。"""
        if self.status == "done":
            return 100.0
        w = self._weights
        if self.stage_index <= 0 or self.stage_index > len(w):
            return 0.0
        base = sum(w[: self.stage_index - 1])
        cur = w[self.stage_index - 1]
        if self.total > 0:
            frac = min(1.0, self.done / self.total)
        else:
            frac = 0.15 if self.status == "running" else 1.0
        return round((base + cur * frac) * 100, 1)

    def snap(self) -> dict:
        now = self.t1 or time.time()
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "stage_total": self.stage_total,
            "done": self.done,
            "total": self.total,
            "pct": self._pct(),
            "message": self.message,
            "elapsed_ms": int((now - self.t0) * 1000),
            "error": self.error,
            "log": self.log[-8:],
            "params": self.params,
        }


_JOBS: dict[str, Job] = {}


def create(kind: str, params: dict | None = None) -> Job:
    _gc()
    job = Job(kind, params)
    _JOBS[job.id] = job
    return job


def get(job_id: str) -> Optional[Job]:
    return _JOBS.get(job_id)


def _gc() -> None:
    """清理过期/超量 job（查询结果含整棵 BOM，留着不放会吃内存）。"""
    now = time.time()
    for jid in [j for j, v in _JOBS.items()
                if (v.t1 and now - v.t1 > JOB_KEEP_SEC)]:
        _JOBS.pop(jid, None)
    if len(_JOBS) > JOB_MAX:
        # 按结束时间/创建时间从旧到新删
        order = sorted(_JOBS.values(), key=lambda v: (v.t1 or v.t0))
        for v in order[: len(_JOBS) - JOB_MAX]:
            _JOBS.pop(v.id, None)
