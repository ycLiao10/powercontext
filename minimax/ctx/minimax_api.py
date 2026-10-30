"""MiniMax 接入：用检索到的上下文驱动模型生成代码修改。

API Key **只从环境变量** ``MINIMAX_API_KEY`` 读取，缺失即抛
:class:`~ctx.config.MissingApiKeyError` —— 本项目不内置任何默认 key，
也不把 key 写进代码、配置或日志。

服务端点用 OpenAI 兼容的 ``POST {base_url}/chat/completions``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests

from .config import MiniMaxSettings

__all__ = ["MiniMaxError", "MiniMaxAPI", "ChatTurn"]


class MiniMaxError(RuntimeError):
    """MiniMax 调用失败。"""


@dataclass
class ChatTurn:
    """一次模型调用（用于留证）。"""

    model: str
    system: str
    user: str
    response_text: str
    raw: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "system_prompt": self.system,
            "user_prompt": self.user,
            "response": self.response_text,
            "usage": self.raw.get("usage"),
            "response_id": self.raw.get("id"),
        }


class MiniMaxAPI:
    """MiniMax chat/completions 客户端。"""

    def __init__(self, settings: MiniMaxSettings | None = None, *, timeout: float = 300.0):
        self.settings = settings or MiniMaxSettings.require_api_key()
        if not self.settings.api_key:
            raise MiniMaxError("MINIMAX_API_KEY 未设置")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self.settings.api_key}"
        self._session.headers["Content-Type"] = "application/json"

    @property
    def endpoint(self) -> str:
        return f"{self.settings.base_url}/chat/completions"

    def chat(
        self,
        user_prompt: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> ChatTurn:
        """发起一次对话补全，返回模型输出与原始响应。"""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            response = self._session.post(self.endpoint, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MiniMaxError(f"调用 MiniMax 失败：{exc}") from exc

        if response.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax 返回 HTTP {response.status_code}：{response.text[:500]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise MiniMaxError(f"MiniMax 返回的不是 JSON：{response.text[:300]}") from exc

        if "error" in data:
            raise MiniMaxError(f"MiniMax 返回错误：{json.dumps(data['error'], ensure_ascii=False)}")

        choices = data.get("choices") or []
        if not choices:
            raise MiniMaxError(f"MiniMax 未返回任何 choice：{json.dumps(data, ensure_ascii=False)[:300]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        return ChatTurn(
            model=self.settings.model,
            system=system_prompt,
            user=user_prompt,
            response_text=content,
            raw=data,
        )
