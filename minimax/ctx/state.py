"""PowerContext 集成：保存任务进展与验证结果，重启后读回。

接入方式：**HTTP API**（``powercontext`` 1.2.0 自带的 REST 服务），
服务进程用 ``powercontext server run`` 启动，底层是 SQLite（默认后端），
数据落在磁盘上，所以 **进程重启后状态依然在**。

用到的端点（均为 powercontext 1.2.0 实际提供的接口）：

===========================  ==========================================
``GET  /v1/capabilities``    探测服务是否可用
``POST /v1/scopes``          按幂等键取得（或创建）scope
``POST /v1/memory/remember`` 写入一条记忆（任务进展 / 待办 / 验证结果）
``POST /v1/memory/search``   按语义检索记忆
``POST /v1/memory/entries/list`` 列出 scope 里的全部记忆条目
===========================  ==========================================

关于 scope id：PowerContext 的 scope id 由**服务端生成**（形如 ``scp_xxx``），
创建请求里不能指定 id，但带 ``idempotency_key`` 且参数一致时是幂等的
（重复调用返回同一个 scope）。因此本模块用一组**固定参数**
（固定的 title + 由配置项派生的 summary + 由配置项派生的 idempotency_key）
去取得 scope，并缓存服务端返回的真实 id。

记忆条目的 ``kind`` 字段用来区分用途：

* ``task``        —— 任务描述本身
* ``completed``   —— 已完成的步骤
* ``todo``        —— 待办
* ``verification``—— 验证结果（测试命令、退出码、结论）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from .config import PowerContextSettings

__all__ = [
    "PowerContextError",
    "PowerContextUnavailable",
    "PowerContextClient",
    "STATE_KINDS",
    "TASK_KIND",
    "SCOPE_TITLE",
    "load_state",
    "save_progress",
]

TASK_KIND = "task"
STATE_KINDS = ("completed", "todo", "verification")
SCOPE_TITLE = "SDD 上下文管理"


class PowerContextError(RuntimeError):
    """PowerContext 返回错误。"""


class PowerContextUnavailable(PowerContextError):
    """连不上 PowerContext 服务。"""


@dataclass
class PowerContextClient:
    """PowerContext HTTP 客户端。"""

    settings: PowerContextSettings
    _session: requests.Session = field(default_factory=requests.Session, repr=False)
    _resolved_scope_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.settings.api_token:
            self._session.headers["Authorization"] = f"Bearer {self.settings.api_token}"

    # ------------------------------------------------------------------ 基础
    def _url(self, path: str) -> str:
        return f"{self.settings.base_url}{path}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = self._url(path)
        try:
            response = self._session.request(
                method, url, json=payload, timeout=self.settings.timeout
            )
        except requests.RequestException as exc:
            raise PowerContextUnavailable(
                f"无法连接 PowerContext 服务 {url}：{exc}。"
                f"请先启动：powercontext server run --host 127.0.0.1 --port 8000"
            ) from exc
        if response.status_code >= 400:
            raise PowerContextError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:400]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return response.text

    # ------------------------------------------------------------------ scope
    def resolve_scope(self) -> str:
        """取得可用的 scope_id（幂等创建 + 缓存）。

        配置项 ``POWERCONTEXT_SCOPE_ID`` 在这里扮演「scope 键」的角色：
        它决定 summary 与幂等键，从而决定拿到哪个 scope。
        返回的是**服务端生成的真实 scope id**。
        """
        if self._resolved_scope_id:
            return self._resolved_scope_id
        configured = self.settings.scope_id
        created = self._request(
            "POST",
            "/v1/scopes",
            {
                "title": SCOPE_TITLE,
                "summary": f"scope for {configured}",
                "idempotency_key": f"ctx-{configured}",
            },
        )
        scope_id = str(created.get("scope_id") or "")
        if not scope_id:
            raise PowerContextError(f"创建 scope 失败，服务端未返回 scope_id：{created}")
        self._resolved_scope_id = scope_id
        return scope_id

    # ------------------------------------------------------------------ 接口
    def capabilities(self) -> Any:
        """读取服务能力，用于确认服务真的在跑。"""
        return self._request("GET", "/v1/capabilities")

    def remember(self, kind: str, text: str, *, reason: str | None = None) -> Any:
        """写入一条记忆。"""
        payload: dict[str, Any] = {
            "scope_id": self.resolve_scope(),
            "kind": kind,
            "text": text,
        }
        if reason:
            payload["reason"] = reason
        return self._request("POST", "/v1/memory/remember", payload)

    def search(self, query: str, limit: int = 10) -> Any:
        """按语义检索记忆。"""
        return self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": self.resolve_scope(), "query": query, "limit": limit},
        )

    def list_entries(self, *, include_inactive: bool = False) -> list[dict]:
        """列出 scope 内全部记忆条目。"""
        payload: dict[str, Any] = {"scope_id": self.resolve_scope()}
        if include_inactive:
            payload["include_inactive"] = True
        data = self._request("POST", "/v1/memory/entries/list", payload)
        if isinstance(data, dict):
            return list(data.get("entries") or [])
        return list(data or [])


def load_state(client: PowerContextClient | None = None) -> dict[str, Any]:
    """读回任务状态，按 ``kind`` 归类。

    返回 ``{"completed": [...], "todo": [...], "verification": [...]}``，
    另附 ``available``（服务是否可用）、``scope_id``（真实 scope id）。
    服务不可用时返回空状态并把 ``available`` 置 False，调用方据此提示。
    """
    empty: dict[str, Any] = {
        "completed": [],
        "todo": [],
        "verification": [],
        "available": False,
    }
    client = client or PowerContextClient(PowerContextSettings.from_env())
    try:
        scope_id = client.resolve_scope()
        entries = client.list_entries()
    except PowerContextError:
        return empty
    state: dict[str, Any] = {kind: [] for kind in STATE_KINDS}
    tasks: list[str] = []
    for entry in entries:
        kind = str(entry.get("kind") or "")
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        if kind == TASK_KIND:
            tasks.append(text)
        elif kind in state:
            if text not in state[kind]:
                state[kind].append(text)
    state["available"] = True
    state["scope_id"] = scope_id
    if tasks:
        state["task"] = tasks[-1]
    return state


def save_progress(kind: str, text: str, *, reason: str | None = None) -> Any:
    """把一条任务进展写进 PowerContext。"""
    if kind not in STATE_KINDS and kind != TASK_KIND:
        raise ValueError(f"未知的记忆类型 {kind!r}，应为 {STATE_KINDS} 或 {TASK_KIND!r}")
    client = PowerContextClient(PowerContextSettings.from_env())
    return client.remember(kind, text, reason=reason)
