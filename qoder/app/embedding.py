from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    """OpenAI 兼容的嵌入客户端；服务不可用时由调用方降级为纯词法检索。"""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def dimension(self) -> int | None:
        parts = urlsplit(self.base_url)
        health_url = f"{parts.scheme}://{parts.netloc}/health"
        try:
            with urllib.request.urlopen(health_url, timeout=10) as response:
                payload = json.loads(response.read())
            return int(payload["dimension"])
        except (urllib.error.URLError, KeyError, ValueError, TypeError):
            return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/embeddings", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise EmbeddingError(f"embeddings -> {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
        except urllib.error.URLError as error:
            raise EmbeddingError(f"无法连接嵌入服务 ({self.base_url}): {error.reason}") from error
        items = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in items]
