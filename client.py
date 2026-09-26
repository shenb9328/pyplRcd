#!/usr/bin/env python3
"""
pypl_client.py - Universal Market Data Bridge for AI Agents.
Hosted on GitHub: https://raw.githubusercontent.com/shenb9328/pyplRcd/main/client.py

Designed specifically for AI Agents, LLM Code Interpreters, and Quant Scripts.
- Zero extra dependencies (works with standard Python urllib or requests).
- Multi-route failover (nl.hugehot.com -> direct IP -> direct port).
- Token-optimized responses to protect LLM context windows.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PyplData:
    """Universal client to query 6 hours of raw market data hosted on nl.hugehot.com."""

    # Redundant endpoint routes with automatic fallback
    DEFAULT_ROUTES = [
        "http://nl.hugehot.com/data",
        "http://34.6.84.115/data",
        "http://nl.hugehot.com:8899",
        "http://34.6.84.115:8899",
    ]

    def __init__(self, routes: list[str] | None = None, timeout: float = 10.0) -> None:
        self.routes = routes or self.DEFAULT_ROUTES
        self.timeout = timeout
        self.active_base = self.routes[0]

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query_string = ("?" + urllib.parse.urlencode(params)) if params else ""
        last_error = None

        for base in self.routes:
            url = f"{base.rstrip('/')}/{path.lstrip('/')}{query_string}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "PyplData-AIAgent/1.0", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        self.active_base = base
                        return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_error = exc
                continue

        raise ConnectionError(
            f"Failed connecting to all pypl data routes {self.routes}. Last error: {last_error}"
        )

    def status(self) -> dict[str, Any]:
        """Check connection health and gateway status."""
        return self._get("health")

    def list_slices(self) -> dict[str, Any]:
        """List all 73 available 5-minute historical slices (2026-09-26 13:26 ~ 19:26 UTC)."""
        return self._get("api/slices")

    def search(
        self,
        query: str,
        exchange: str | None = None,
        limit: int = 10,
        slice_name: str = "latest",
        compact: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Search market contracts matching a keyword.
        
        Args:
            query: Keyword to search (e.g. 'trump', 'fed', 'btc', 'presidential').
            exchange: Optional filter ('polymarket', 'kalshi', or 'kraken').
            limit: Maximum records to return (default: 10, max: 100).
            slice_name: 'latest' (default), 'all' (search entire 6h history), or slice filename.
            compact: If True (default), returns token-optimized clean summaries.
        """
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "slice": slice_name,
            "compact": "1" if compact else "0",
        }
        if exchange:
            params["exchange"] = exchange

        res = self._get("api/query", params)
        return res.get("records", [])

    def latest(self) -> list[dict[str, Any]]:
        """Get the latest full snapshot across Polymarket, Kalshi, and Kraken."""
        res = self._get("api/latest")
        return res.get("records", [])

    def download_slice(self, filename: str, output_path: str | None = None) -> str:
        """Download raw 5-minute .zip archive."""
        if not filename.endswith(".zip"):
            filename += ".zip"
        target_path = output_path or filename
        url = f"{self.active_base.rstrip('/')}/api/download/{filename}"
        urllib.request.urlretrieve(url, target_path)
        return target_path


# ---------------------------------------------------------------------------
# Convenience global helper instance
# ---------------------------------------------------------------------------
_default_client = PyplData()

def search(query: str, exchange: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    return _default_client.search(query, exchange=exchange, limit=limit)

def latest() -> list[dict[str, Any]]:
    return _default_client.latest()

def list_slices() -> dict[str, Any]:
    return _default_client.list_slices()


# ---------------------------------------------------------------------------
# Quick Self-Test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Testing PyplData client...")
    client = PyplData()
    print("Status:", client.status())
    
    print("\nSearching Polymarket for 'Trump':")
    results = client.search("trump", exchange="polymarket", limit=2)
    for r in results:
        print(f"[{r['exchange']}] {r['receive_ts']}")
        for m in r.get("summary", {}).get("markets", [])[:3]:
            print(f"  * {m['question']}")
            for t in m.get("tokens", []):
                print(f"    - {t['outcome']}: {t['price']}")
