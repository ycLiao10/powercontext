from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

SCOPE_TITLE = "文献阅读工作区"
SCOPE_SUMMARY = "文献导入、检索与阅读偏好的上下文空间"


class PowerContextError(RuntimeError):
    pass


class PowerContext:
    """PowerContext Server 的最小 HTTP 客户端：Scope、来源、记忆。"""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._scope_id: str | None = None

    def scope_id(self) -> str:
        if self._scope_id is None:
            self._scope_id = self._ensure_scope()
        return self._scope_id

    def _ensure_scope(self) -> str:
        for scope in self._call("GET", "/v1/scopes").get("items", []):
            if scope.get("title") == SCOPE_TITLE:
                return scope["scope_id"]
        created = self._call(
            "POST",
            "/v1/scopes",
            {"title": SCOPE_TITLE, "summary": SCOPE_SUMMARY, "idempotency_key": uuid.uuid4().hex},
        )
        return created["scope_id"]

    def capture_source(self, source_id: str, content: str, metadata: dict) -> None:
        self._call(
            "POST",
            "/v1/sources/content",
            {"scope_id": self.scope_id(), "source_id": source_id, "content": content, "metadata": metadata},
        )

    def remember(self, kind: str, text: str, reason: str | None = None) -> dict:
        return self._call(
            "POST",
            "/v1/memory/remember",
            {"scope_id": self.scope_id(), "kind": kind, "text": text, "reason": reason},
        )

    def search_memory(self, query: str, limit: int = 5) -> list[dict]:
        response = self._call(
            "POST", "/v1/memory/search", {"scope_id": self.scope_id(), "query": query, "limit": limit}
        )
        return [
            {
                "entry_id": hit.get("citation", {}).get("entry_id", ""),
                "text": hit.get("text", ""),
                "score": hit.get("score"),
            }
            for hit in response.get("hits", [])
        ]

    def capabilities(self) -> dict:
        return self._call("GET", "/v1/capabilities")

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, headers={"Content-Type": "application/json"}, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise PowerContextError(f"{method} {path} -> {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise PowerContextError(f"无法连接 PowerContext Server ({self.base_url}): {error.reason}") from error
        return json.loads(body) if body else {}
