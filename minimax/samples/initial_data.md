# 初始数据

流程起点的全部状态。

## 1. 业务工程起点

`sample_project/src/pricing.py`（改前，**故意是错的**）：

```python
def total(items, is_member):
    amount = sum(float(item["price"]) for item in items)
    discount = 0.0
    if amount >= 200:
        discount = 30.0
    if is_member:
        discount += amount * 0.1      # ← 叠加；规格要求取优惠大的
    return round(amount - discount, 2)
```

改前的实测行为（`logs/before.txt`）：

| 输入 | 期望 | 实际 | 差 |
| --- | --- | --- | --- |
| `total(_items(250), is_member=True)` | 220 | 195.0 | 少减了 30 |
| `total(_items(1000), is_member=True)` | 900 | 870.0 | 少减了 100 |

两条都是 `AssertionError`（断言失败），不是 import error，也不是环境错误。

## 2. seekdb 初始状态

```bash
python init_db.py --reset
```

```
已导入 14 条文档，库内统计：{'code': 2, 'spec': 4, 'test': 8}
```

| kind | 条数 | 来源 | symbol 含义 |
| --- | --- | --- | --- |
| `spec` | 4 | `specs/pricing.md` | Markdown 小节标题 |
| `code` | 1 | `src/pricing.py` | 函数名 `total` |
| `code` | 1 | `src/pricing.py` | `<file>` —— 整份文件原文（模型改写的基准） |
| `test` | 8 | `tests/test_pricing.py` | 测试函数名 |

> `conftest.py` 是测试脚手架，**不进索引**（见 `ctx/ingest.py::classify`）。

表结构：

```sql
CREATE TABLE `eng_docs` (
  `kind`    varchar(32)  NOT NULL,
  `path`    varchar(512) NOT NULL,
  `symbol`  varchar(255) NOT NULL DEFAULT '',
  `content` text DEFAULT NULL,
  PRIMARY KEY (`kind`, `path`, `symbol`),
  KEY `idx_path` (`path`),
  FULLTEXT KEY `ft_content` (`content`) WITH PARSER ngram
);
```

复合主键让「同一路径 + 同一符号」天然唯一，重复导入走
`ON DUPLICATE KEY UPDATE content = VALUES(content)`；同时 `ctx index` 会**裁剪**
本工程前缀下已不存在的记录（文件被删、符号被改名都不会留下残留），
因此 **`ctx index` 幂等且是真正的全量刷新**（连跑三次都是 14 条）。

## 3. PowerContext 初始状态

**空**。

```bash
python -m ctx status
```

```
PowerContext: http://127.0.0.1:8000
scope 键: powercontext-minimax
scope id: scp_4xsxyvsh1rbj2tg6cbt61v51yq
completed (0):
todo (0):
verification (0):
  （暂无记录）
```

（`scope id` 由服务端生成，每次全新部署都会不同，属于正常现象。）

## 4. 环境变量初始状态

```bash
export SEEKDB_HOST=127.0.0.1        # 或留空走默认值
export SEEKDB_PORT=2881
export SEEKDB_USER=root
export SEEKDB_PASSWORD=             # 空密码
export POWERCONTEXT_SERVER_URL=http://127.0.0.1:8000
export POWERCONTEXT_SCOPE_ID=powercontext-minimax
export MINIMAX_API_KEY=<你的 key>    # 第四阶段才需要
```
