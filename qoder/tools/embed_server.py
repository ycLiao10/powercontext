#!/usr/bin/env python
"""本地嵌入服务：OpenAI 兼容的 /v1/embeddings，供 PowerContext 与工作台调用。

默认模型 BAAI/bge-small-zh-v1.5（512 维，中文友好，CPU 可跑）。
启动：python tools/embed_server.py --port 8081
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 部分网络环境下 certifi 根证书链不完整，模型下载会 SSL 校验失败；优先使用操作系统信任库
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "models"


class Embedder:
    def __init__(self, model_name: str, cache_dir: Path):
        from fastembed import TextEmbedding

        cache_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        self._lock = threading.Lock()
        probe = next(iter(self._model.embed(["维度探测"])))
        self.dimension = len(probe)

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [[float(value) for value in vector] for vector in self._model.embed(texts)]


def build_handler(embedder: Embedder):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalEmbed/0.1"

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                self._json({"status": "ok", "model": embedder.model_name, "dimension": embedder.dimension})
            elif self.path.startswith("/v1/models"):
                self._json({"object": "list", "data": [{"id": embedder.model_name, "object": "model"}]})
            else:
                self._json({"error": {"message": "not found"}}, status=404)

        def do_POST(self) -> None:
            if not self.path.startswith("/v1/embeddings"):
                self._json({"error": {"message": "not found"}}, status=404)
                return
            try:
                payload = self._body()
                raw_input = payload.get("input")
                texts = [raw_input] if isinstance(raw_input, str) else list(raw_input or [])
                if not texts:
                    raise ValueError("input 不能为空")
                vectors = embedder.embed(texts)
                self._json(
                    {
                        "object": "list",
                        "model": payload.get("model") or embedder.model_name,
                        "data": [
                            {"object": "embedding", "index": index, "embedding": vector}
                            for index, vector in enumerate(vectors)
                        ],
                        "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": sum(len(text) for text in texts)},
                    }
                )
            except (ValueError, TypeError, KeyError) as error:
                self._json({"error": {"message": str(error)}}, status=400)
            except Exception as error:  # 模型侧异常统一按 500 返回，避免连接被直接关闭
                self._json({"error": {"message": repr(error)}}, status=500)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="本地嵌入服务（OpenAI 兼容）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    args = parser.parse_args()

    embedder = Embedder(args.model, Path(args.cache_dir))
    server = ThreadingHTTPServer((args.host, args.port), build_handler(embedder))
    print(f"embedding service -> http://{args.host}:{args.port}  model={args.model} dim={embedder.dimension}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
