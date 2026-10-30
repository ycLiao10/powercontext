from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .embedding import EmbeddingClient, EmbeddingError
from .ingest import backfill_vectors, ingest_document
from .pc_client import PowerContext, PowerContextError
from .search import search
from .store import Store

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR.parent / "data" / "literature.db"
DEFAULT_EMBED_URL = "http://127.0.0.1:8081/v1"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"


def build_handler(store: Store, pc: PowerContext, embedder: EmbeddingClient):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LiteratureDesk/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send_file(BASE_DIR / "index.html", "text/html; charset=utf-8")
                elif parsed.path == "/api/health":
                    self._handle_health()
                elif parsed.path == "/api/documents":
                    self._json({"documents": store.documents()})
                elif parsed.path == "/api/search":
                    self._handle_search(parse_qs(parsed.query).get("q", [""])[0])
                else:
                    self._json({"error": "not found"}, status=404)
            except (ValueError, PowerContextError) as error:
                self._fail(error)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/documents":
                    self._handle_ingest()
                elif parsed.path == "/api/preferences":
                    self._handle_preference()
                else:
                    self._json({"error": "not found"}, status=404)
            except (ValueError, PowerContextError) as error:
                self._fail(error)

        def _fail(self, error: Exception) -> None:
            status = 400 if isinstance(error, ValueError) else 503
            self._json({"error": str(error)}, status=status)

        def _handle_health(self) -> None:
            try:
                capabilities = pc.capabilities()
                pc_state = {"available": True, "scope_id": pc.scope_id(),
                            "search_modes": capabilities.get("search_modes", [])}
            except PowerContextError as error:
                pc_state = {"available": False, "error": str(error)}
            dimension = embedder.dimension()
            embedding_state = {"available": dimension is not None, "model": embedder.model,
                               "dimension": dimension, "base_url": embedder.base_url}
            self._json({"powercontext": pc_state, "embedding": embedding_state,
                        "documents": len(store.documents()), "chunks": store.chunk_count()})

        def _handle_ingest(self) -> None:
            payload = self._body()
            result = ingest_document(store, pc, embedder, str(payload.get("title", "")), str(payload.get("text", "")))
            self._json(result, status=200)

        def _handle_search(self, query: str) -> None:
            self._json(search(store, pc, embedder, query))

        def _handle_preference(self) -> None:
            payload = self._body()
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("偏好内容不能为空")
            entry = pc.remember("preference", text, "用户手动记录")
            self._json({"entry": entry["entry"]["citation"]})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    parser = argparse.ArgumentParser(description="文献阅读工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.environ.get("LITERATURE_DB", str(DEFAULT_DB)))
    parser.add_argument("--pc-url", default=os.environ.get("POWERCONTEXT_CLIENT_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--embed-url", default=os.environ.get("LITERATURE_EMBED_URL", DEFAULT_EMBED_URL))
    parser.add_argument("--embed-model", default=os.environ.get("LITERATURE_EMBED_MODEL", DEFAULT_EMBED_MODEL))
    args = parser.parse_args()

    store = Store(Path(args.db))
    pc = PowerContext(args.pc_url)
    embedder = EmbeddingClient(args.embed_url, args.embed_model)
    dimension = embedder.dimension()
    embed_state = f"{args.embed_model} dim={dimension}" if dimension else f"不可用 ({args.embed_url})"
    if dimension:
        try:
            backfilled = backfill_vectors(store, embedder)
            if backfilled:
                print(f"已为 {backfilled} 个历史分块补齐向量")
        except EmbeddingError as error:
            print(f"向量回填失败，检索降级为词法: {error}", file=sys.stderr)
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, pc, embedder))
    print(f"文献阅读工作台 -> http://{args.host}:{args.port}  (PowerContext: {args.pc_url}, DB: {args.db}, Embedding: {embed_state})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
