# pyplRcd

最简单的原始 API 录像程序。

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
