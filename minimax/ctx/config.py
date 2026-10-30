"""运行时配置。

约定：**所有连接参数与密钥一律来自环境变量**，代码里只保留「本地开发默认值」，
不包含任何真实 API key / 密码。

===========================  ==========================================
环境变量                     含义与默认值
===========================  ==========================================
``SEEKDB_HOST``              seekdb 主机，默认 ``127.0.0.1``
``SEEKDB_PORT``              seekdb 端口，默认 ``2881``
``SEEKDB_USER``              seekdb 用户，默认 ``root``
``SEEKDB_PASSWORD``          seekdb 密码，默认空字符串
``SEEKDB_DATABASE``          库名，默认 ``powercontext_minimax``
``POWERCONTEXT_SERVER_URL``  PowerContext 服务地址，默认 ``http://127.0.0.1:8000``
``POWERCONTEXT_SCOPE_ID``    存放任务状态的 scope，默认 ``powercontext-minimax``
``POWERCONTEXT_CLIENT_API_TOKEN``  可选，Bearer token，默认空
``MINIMAX_API_KEY``          MiniMax API Key（**必填，缺失即报错，绝不写死**）
``MINIMAX_BASE_URL``         MiniMax 接口地址，默认官方兼容地址
``MINIMAX_MODEL``            使用的模型名
===========================  ==========================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "SeekDBSettings",
    "PowerContextSettings",
    "MiniMaxSettings",
    "DEFAULT_SEEKDB_DATABASE",
    "DEFAULT_POWERCONTEXT_SCOPE_ID",
    "DEFAULT_MINIMAX_BASE_URL",
    "DEFAULT_MINIMAX_MODEL",
]

DEFAULT_SEEKDB_DATABASE = "powercontext_minimax"
DEFAULT_POWERCONTEXT_SCOPE_ID = "powercontext-minimax"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"


def _get(name: str, default: str) -> str:
    """读取环境变量；未设置或为空串时返回默认值。"""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


@dataclass(frozen=True)
class SeekDBSettings:
    """seekdb 连接参数。"""

    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> "SeekDBSettings":
        try:
            port = int(_get("SEEKDB_PORT", "2881"))
        except ValueError as exc:  # pragma: no cover - 配置错误
            raise ValueError(f"SEEKDB_PORT 必须是整数，收到 {os.environ.get('SEEKDB_PORT')!r}") from exc
        return cls(
            host=_get("SEEKDB_HOST", "127.0.0.1"),
            port=port,
            user=_get("SEEKDB_USER", "root"),
            password=os.environ.get("SEEKDB_PASSWORD", ""),
            database=_get("SEEKDB_DATABASE", DEFAULT_SEEKDB_DATABASE),
        )

    @property
    def dsn(self) -> str:
        """不含密码的可读描述，用于日志与证据文件。"""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class PowerContextSettings:
    """PowerContext 服务与 scope 配置。"""

    base_url: str
    scope_id: str
    timeout: float
    api_token: str

    @classmethod
    def from_env(cls) -> "PowerContextSettings":
        try:
            timeout = float(_get("POWERCONTEXT_CLIENT_TIMEOUT", "30"))
        except ValueError as exc:  # pragma: no cover - 配置错误
            raise ValueError("POWERCONTEXT_CLIENT_TIMEOUT 必须是数字") from exc
        return cls(
            base_url=_get("POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8000").rstrip("/"),
            scope_id=_get("POWERCONTEXT_SCOPE_ID", DEFAULT_POWERCONTEXT_SCOPE_ID),
            timeout=timeout,
            api_token=os.environ.get("POWERCONTEXT_CLIENT_API_TOKEN", ""),
        )


@dataclass(frozen=True)
class MiniMaxSettings:
    """MiniMax 接入参数。

    ``api_key`` 只从 ``MINIMAX_API_KEY`` 读取；缺失时 :meth:`require_api_key`
    直接抛错，调用方必须停下来向使用者索取，**不允许内置任何默认 key**。
    """

    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_env(cls) -> "MiniMaxSettings":
        return cls(
            api_key=os.environ.get("MINIMAX_API_KEY", ""),
            base_url=_get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL).rstrip("/"),
            model=_get("MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL),
        )

    @classmethod
    def require_api_key(cls) -> "MiniMaxSettings":
        """取得配置；没有 API Key 时抛 :class:`MissingApiKeyError`。"""
        settings = cls.from_env()
        if not settings.api_key:
            raise MissingApiKeyError(
                "环境变量 MINIMAX_API_KEY 未设置。请先导出 MiniMax API Key 再重试，"
                "本项目不会把 key 写死在代码或配置文件中。"
            )
        return settings


class MissingApiKeyError(RuntimeError):
    """MINIMAX_API_KEY 缺失。"""
