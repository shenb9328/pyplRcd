#!/usr/bin/env python3
"""
pypl Offline Market Data Server & Fast AI Query Gateway.
Host: nl.hugehot.com
Provides high-performance, stream-decompressed, agent-friendly HTTP endpoints
for LLMs, AI agents, and remote scripts to query 6-hour high-density raw market data.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "/home/shenb/pypl/pyplRcd/data"))
PORT = int(os.getenv("PORT", "8899"))
DOMAIN = os.getenv("SERVER_DOMAIN", "nl.hugehot.com")


# ---------------------------------------------------------------------------
# Data Helper & Search
# ---------------------------------------------------------------------------

def list_slices() -> list[dict[str, Any]]:
    """List all available 5-minute zip slices sorted chronologically."""
    if not DATA_DIR.exists():
        return []
    
    slices = []
    for p in sorted(DATA_DIR.glob("*.zip")):
        stat = p.stat()
        m = re.match(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.zip", p.name)
        iso_time = ""
        if m:
            y, mo, d, h, mi, s = m.groups()
            iso_time = f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
            
        slices.append({
            "filename": p.name,
            "iso_time": iso_time,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "mtime": stat.st_mtime,
        })
    return slices


def extract_compact_summary(exchange: str, payload_str: str, query: str = "") -> dict[str, Any]:
    """
    Extract a concise token-friendly summary from raw payload.
    Avoids filling up LLM context window while preserving essential market data.
    """
    try:
        data = json.loads(payload_str)
    except Exception:
        return {"raw_snippet": payload_str[:300]}

    q_lower = query.lower() if query else ""

    if exchange == "polymarket":
        markets = data.get("data", [])
        matched = []
        for m in markets:
            q_text = str(m.get("question", "")).lower()
            slug = str(m.get("market_slug", "")).lower()
            if not q_lower or q_lower in q_text or q_lower in slug:
                tokens = []
                for tok in m.get("tokens", []):
                    tokens.append({
                        "outcome": tok.get("outcome"),
                        "price": tok.get("price"),
                        "token_id": tok.get("token_id"),
                    })
                matched.append({
                    "question": m.get("question"),
                    "slug": m.get("market_slug"),
                    "active": m.get("active"),
                    "tokens": tokens,
                })
                if len(matched) >= 20:
                    break
        return {
            "total_markets_in_batch": len(markets),
            "matched_count": len(matched),
            "markets": matched,
        }

    elif exchange == "kalshi":
        events = data.get("events", [])
        matched = []
        for ev in events:
            ev_title = str(ev.get("title", "")).lower()
            series = str(ev.get("series_ticker", "")).lower()
            if not q_lower or q_lower in ev_title or q_lower in series:
                mkts = []
                for m in ev.get("markets", []):
                    mkts.append({
                        "ticker": m.get("ticker"),
                        "title": m.get("title"),
                        "yes_bid": m.get("yes_bid"),
                        "yes_ask": m.get("yes_ask"),
                        "last_price": m.get("last_price"),
                    })
                matched.append({
                    "event_title": ev.get("title"),
                    "series_ticker": ev.get("series_ticker"),
                    "markets": mkts,
                })
                if len(matched) >= 20:
                    break
        return {
            "total_events_in_batch": len(events),
            "matched_count": len(matched),
            "events": matched,
        }

    elif exchange == "kraken":
        pairs = data.get("result", {})
        matched = {}
        for p_name, p_data in pairs.items():
            if not q_lower or q_lower in p_name.lower():
                matched[p_name] = {
                    "ask": p_data.get("a", [""])[0],
                    "bid": p_data.get("b", [""])[0],
                    "last": p_data.get("c", [""])[0],
                    "vol_24h": p_data.get("v", [""])[0],
                }
                if len(matched) >= 30:
                    break
        return {
            "total_pairs_in_batch": len(pairs),
            "matched_count": len(matched),
            "tickers": matched,
        }

    return {"raw_snippet": payload_str[:300]}


def query_records(
    exchange: str | None = None,
    query: str = "",
    slice_name: str | None = None,
    limit: int = 10,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Stream-query records across slices matching exchange and query keywords."""
    all_slices = sorted(DATA_DIR.glob("*.zip"), reverse=True)
    if not all_slices:
        return []

    target_slices = all_slices
    if slice_name and slice_name != "all":
        if slice_name == "latest":
            target_slices = [all_slices[0]]
        else:
            if not slice_name.endswith(".zip"):
                slice_name += ".zip"
            found = [s for s in all_slices if s.name == slice_name]
            if found:
                target_slices = found

    results = []
    q_lower = query.lower().strip() if query else ""

    for zip_path in target_slices:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for inner_name in zf.namelist():
                    with zf.open(inner_name) as f:
                        for line in f:
                            rec = json.loads(line.decode("utf-8"))
                            rec_exchange = rec.get("exchange", "")
                            if exchange and rec_exchange.lower() != exchange.lower():
                                continue

                            payload_str = rec.get("payload", "")
                            if q_lower and q_lower not in payload_str.lower():
                                continue

                            entry: dict[str, Any] = {
                                "slice": zip_path.name,
                                "receive_ts": rec.get("receive_ts"),
                                "exchange": rec_exchange,
                                "url": rec.get("url"),
                                "fetch_latency_ms": rec.get("fetch_latency_ms"),
                                "http_status": rec.get("http_status"),
                            }
                            if compact:
                                entry["summary"] = extract_compact_summary(rec_exchange, payload_str, q_lower)
                            else:
                                entry["payload"] = payload_str

                            results.append(entry)
                            if len(results) >= limit:
                                return results
        except Exception as exc:
            print(f"Error reading {zip_path}: {exc}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class DataServerHandler(BaseHTTPRequestHandler):
    server_version = "pypl-DataGateway/1.0"

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # 1. Root / Help Documentation
        if path in ("", "/", "/api", "/help"):
            self._handle_docs()
            return

        # 2. Health check
        if path == "/health":
            self._send_json(200, {"status": "ok", "timestamp": datetime.utcnow().isoformat() + "Z"})
            return

        # 3. Slices List
        if path == "/api/slices":
            slices = list_slices()
            total_mb = sum(s["size_mb"] for s in slices)
            self._send_json(200, {
                "total_slices": len(slices),
                "total_size_mb": round(total_mb, 2),
                "time_span": f"{slices[0]['iso_time']} -> {slices[-1]['iso_time']}" if slices else "",
                "slices": slices,
            })
            return

        # 4. Search & Query API
        if path == "/api/query":
            exchange = params.get("exchange", [None])[0]
            query_str = params.get("q", params.get("query", [""]))[0]
            slice_name = params.get("slice", ["latest"])[0]
            try:
                limit = min(int(params.get("limit", [10])[0]), 100)
            except ValueError:
                limit = 10
            compact = params.get("compact", ["1"])[0].lower() not in ("0", "false", "no")

            results = query_records(
                exchange=exchange,
                query=query_str,
                slice_name=slice_name,
                limit=limit,
                compact=compact,
            )
            self._send_json(200, {
                "matched_count": len(results),
                "params": {
                    "exchange": exchange,
                    "query": query_str,
                    "slice": slice_name,
                    "limit": limit,
                    "compact": compact,
                },
                "records": results,
            })
            return

        # 5. Latest Snapshot
        if path == "/api/latest":
            results = query_records(slice_name="latest", limit=12, compact=True)
            self._send_json(200, {
                "description": "Latest snapshot across all configured exchanges",
                "count": len(results),
                "records": results,
            })
            return

        # 6. Direct Slice Download
        if path.startswith("/api/download/"):
            fname = path[len("/api/download/"):]
            if not fname.endswith(".zip"):
                fname += ".zip"
            target_file = DATA_DIR / fname
            if not target_file.exists() or not target_file.is_file():
                self._send_json(404, {"error": "File not found", "requested": fname})
                return

            stat = target_file.stat()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{target_file.name}"')
            self.send_header("Content-Length", str(stat.st_size))
            self.end_headers()
            with open(target_file, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    self.wfile.write(chunk)
            return

        self._send_json(404, {"error": "Endpoint not found", "available": ["/", "/api/slices", "/api/query", "/api/latest", "/api/download/<filename>"]})

    def _handle_docs(self) -> None:
        slices = list_slices()
        time_span = f"{slices[0]['iso_time']} ~ {slices[-1]['iso_time']}" if slices else "2026-09-26 13:26 ~ 19:26 UTC"
        total_mb = sum(s["size_mb"] for s in slices)

        base_url = f"http://{DOMAIN}/data"
        direct_url = f"http://{DOMAIN}:{PORT}"

        doc = f"""# 🚀 pypl Offline Market Data & AI Query Gateway

Welcome! This service allows LLMs, AI agents, and quant researchers to seamlessly query
**6 hours of continuous, uncompressed-equivalent (67.5 GB) high-density market snapshots**
without downloading huge archives or exceeding token limits.

- **Base Gateway URL**: `{base_url}` (or `{direct_url}`)
- **Time Span**: `{time_span}` (73 slices @ 5-min intervals)
- **Total Compressed Size**: `{round(total_mb, 2)} MB` (~7.1 GB)
- **Covered Markets**:
  - **Polymarket**: 3,000 prediction markets (all outcome tokens, estimates & rewards)
  - **Kalshi**: 2,724 prediction contracts (nested markets, open events)
  - **Kraken**: 1,480 crypto spot pairs (live orderbook bid/ask/last/vwap)

---

## 📡 API Endpoints for AI Agents

### 1. Fast Keyword Search (`/api/query`)
Search across markets with token-optimized JSON responses.

```http
GET /api/query?q=<keyword>&exchange=<polymarket|kalshi|kraken>&limit=10&compact=1
```

**Parameters:**
- `q`: Search keyword (e.g. `trump`, `harris`, `btc`, `rate`, `fed`)
- `exchange`: Filter by `polymarket`, `kalshi`, or `kraken` (optional)
- `slice`: `latest` (default), `all` (search entire 6h history), or specific `YYYYMMDD_HHMMSS`
- `limit`: Max records to return (default: `10`, max: `100`)
- `compact`: `1` (default, token-saving summary) or `0` (full raw verbatim payload)

**Examples:**
```bash
# Search Trump-related markets on Polymarket
curl -s "{base_url}/api/query?exchange=polymarket&q=trump&limit=3"

# Search Bitcoin spot and prediction contracts
curl -s "{base_url}/api/query?q=btc&limit=5"

# Query Kraken real-time crypto prices
curl -s "{base_url}/api/query?exchange=kraken&limit=5"
```

---

### 2. Latest Full Snapshot (`/api/latest`)
Fetch the most recent multi-exchange snapshot in one call.

```bash
curl -s "{base_url}/api/latest"
```

---

### 3. List All Available Time Slices (`/api/slices`)
List all 73 five-minute slice zip files.

```bash
curl -s "{base_url}/api/slices"
```

---

### 4. Direct Slice Download (`/api/download/<filename>`)
Download any raw 5-minute zip archive directly.

```bash
curl -O "{base_url}/api/download/20260926_132500.zip"
```

---

## 🐍 Python 1-Line Client for other AI Agents

```python
import requests

BASE = "{base_url}"

# 1. Search any market by keyword
res = requests.get(f"{{BASE}}/api/query", params={{"q": "trump", "limit": 5}}).json()
for r in res["records"]:
    print(r["exchange"], r["receive_ts"], r.get("summary"))

# 2. Get latest Kraken spot prices
kraken = requests.get(f"{{BASE}}/api/query", params={{"exchange": "kraken", "limit": 1}}).json()
tickers = kraken["records"][0]["summary"]["tickers"]
print("BTC/USD:", tickers.get("XXBTZUSD"))
```
"""
        self._send_text(200, doc)


# ---------------------------------------------------------------------------
# Server Launcher
# ---------------------------------------------------------------------------

def run_server(port: int = PORT) -> None:
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, DataServerHandler)
    print(f"Data Server running on http://0.0.0.0:{port}")
    print(f"Public Domain Gateway: http://{DOMAIN}/data (via Nginx) or http://{DOMAIN}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping data server...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server(PORT)
