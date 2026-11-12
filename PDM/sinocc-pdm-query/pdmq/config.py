"""Settings for SinoCC bridge."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_prefix="SINOCC_",
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "http://newpdm.carcgl.com"
    timeout: float = 60.0   # cpcwf.search stat=2 (全部进行中) 实测 ~16s+，必须放宽

    session_backend: str = "local"           # local | redis
    redis_url: str = "redis://localhost:6379/0"

    default_entcode: str = "CHIGOCAC"
    default_invorgid: str = ""

    dump_soap: bool = False

    # ---- 网络出口（2026-09-07 新增）----
    # httpx 默认 trust_env=True，会同时读「环境变量代理」和「Windows 系统/IE 代理」。
    # 打包 exe（run.py 清掉了环境变量代理）与手动 uvicorn 启动时环境不同，
    # 曾出现「手动开能连、打包后连不上」：exe 侧落到系统代理而系统代理不通。
    # 排障/固化手段：
    #   SINOCC_TRUST_ENV=false  → 完全忽略系统代理，直连
    #   SINOCC_PROXY=http://host:port → 显式指定代理（优先于系统代理）
    trust_env: bool = True
    proxy: str = ""


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()


# Re-export for typing
__all__ = ["Settings", "load_settings"]
