"""``python -m ctx`` 的入口。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
