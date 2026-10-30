"""SDD 上下文管理系统（Context Management for Software-driven Development）。

三个子命令::

    python -m ctx index --root sample_project
    python -m ctx build --task "..." --out ctx.json
    python -m ctx status

三个外部组件都真正参与流程：

* seekdb —— 存规格/代码/测试并做全文检索（见 :mod:`ctx.seekdb`、:mod:`ctx.retrieval`）
* PowerContext —— 存任务进展与验证结果、重启后读回（见 :mod:`ctx.state`）
* MiniMax —— 生成代码修改（见 :mod:`ctx.minimax_api`，第四阶段接入）
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
