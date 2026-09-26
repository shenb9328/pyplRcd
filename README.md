# pyplRcd

最简单的原始 API 录像程序与 AI 离线数据服务。

---

## 🤖 外部 AI 极速调用方式 (AI Agent Bridge)

本仓库提供公开透明的只读数据网关与单文件客户端，供 AI 助手、大模型分析脚本按需检索 **2026-09-26 13:26 ~ 19:26 UTC 连续 6 小时的离线历史全景行情录像**。

### 方式 1：标准透明 HTTP REST API（零第三方代码，纯数据交互）
```python
import requests

# 检索特定预测市场与赔率（已做 Token 提炼，防止撑爆上下文）
res = requests.get("http://nl.hugehot.com/data/api/query", params={"q": "Trump", "limit": 3}).json()
for r in res.get("records", []):
    print(r["exchange"], r["receive_ts"], r.get("summary"))
```

### 方式 2：使用审查后的本地客户端 [client.py](client.py)
```python
from client import PyplData

client = PyplData()
# 获取全景快照（Polymarket 3000 + Kalshi 2724 + Kraken 1480）
snapshot = client.latest()
```

---

## 原则

这个程序只做四件事：

1. 请求配置好的 API。
2. API 返回什么就保存什么。
3. 本地每 5 分钟形成一个 JSONL 文件。
4. 5 分钟文件写完后上传到本仓库的 `data/` 目录。

不做：

- 不判断机会
- 不计算 edge
- 不计算 spread
- 不去重
- 不过滤
- 不选择 Focus
- 不做 Universe 分类
- 不修改 API payload
- 不做策略分析

## 运行

需要 Python 3 和 `requests`：

```bash
pip install requests
```

设置 GitHub Token：

```bash
export GITHUB_TOKEN="你的GitHub Token"
```

Token 需要这个仓库的 **Contents: write** 权限。

运行 6 小时：

```bash
python raw_collect.py --hours 6
```

持续运行：

```bash
python raw_collect.py
```

默认每 5 秒请求一次配置的 API。

## 文件

程序运行时：

```text
data/
└── 20260926_200000.jsonl
```

每个文件覆盖一个自然 5 分钟窗口。

文件完成后会上传到：

```text
data/YYYYMMDD_HHMMSS.jsonl
```

上传成功后删除本地临时文件；上传失败则保留，避免数据丢失。

## 单条记录

```json
{
  "collector_ts": "...",
  "request_ts": "...",
  "exchange": "polymarket",
  "url": "...",
  "fetch_latency_ms": 123.456,
  "http_status": 200,
  "payload": {}
}
```

`payload` 是 API 原始 JSON 返回；程序不对其做业务层转换。

## 修改采集对象

直接修改 `raw_collect.py` 顶部的 `SOURCES`。

这里只定义“请求哪个 API”，不定义“这个 API 返回的数据有没有价值”。
