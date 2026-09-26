#!/usr/bin/env python3
"""
V0 raw API recorder.

Only does:
1. request configured public APIs
2. save the response exactly as returned
3. rotate the local JSONL file every 5 minutes
4. upload the completed file to GitHub

No filtering, deduplication, edge calculation, market selection, or strategy logic.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO = os.getenv("GITHUB_REPO", "shenb9328/pyplRcd")
BRANCH = os.getenv("GITHUB_BRANCH", "main")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

# One local file covers exactly this many seconds.
FILE_SECONDS = 5 * 60

# How often each configured API is requested.
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))

# HTTP timeout for exchange APIs.
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))

# GitHub API timeout.
GITHUB_TIMEOUT = float(os.getenv("GITHUB_TIMEOUT", "30"))

# Public read-only APIs.  These are deliberately just URLs + parameters.
# The recorder does not inspect or transform their response payloads.
#
# Add/remove URLs here when the raw collection target changes.
SOURCES = [
    {
        "name": "polymarket",
        "url": "https://clob.polymarket.com/sampling-markets",
        "params": {"next_cursor": "MA=="},
    },
    {
        "name": "kalshi",
        "url": "https://api.elections.kalshi.com/trade-api/v2/events",
        "params": {
            "limit": 100,
            "status": "open",
            "with_nested_markets": "true",
        },
    },
    {
        "name": "kraken",
        "url": "https://api.kraken.com/0/public/AssetPairs",
        "params": {},
    },
]


# ---------------------------------------------------------------------------
# Time / JSON
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def file_name_for(ts: float) -> str:
    # Natural UTC 5-minute window.
    bucket = int(ts // FILE_SECONDS) * FILE_SECONDS
    dt = datetime.fromtimestamp(bucket, timezone.utc)
    return dt.strftime("%Y%m%d_%H%M%S.jsonl")


def json_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Local writer
# ---------------------------------------------------------------------------

class RawWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path: Path | None = None
        self.fp = None
        self.bucket: str | None = None

    def _open_for(self, ts: float) -> None:
        bucket = file_name_for(ts)
        if self.path is not None and self.bucket == bucket:
            return

        self.close()

        self.bucket = bucket
        self.path = self.root / bucket
        self.fp = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, record: dict[str, Any], ts: float) -> None:
        self._open_for(ts)
        assert self.fp is not None
        self.fp.write(json_line(record))
        self.fp.flush()

    def close(self) -> Path | None:
        if self.fp is not None:
            self.fp.flush()
            self.fp.close()
            self.fp = None

        old = self.path
        self.path = None
        self.bucket = None
        return old


# ---------------------------------------------------------------------------
# GitHub uploader
# ---------------------------------------------------------------------------

class GitHubUploader:
    def __init__(self, repo: str, branch: str, token: str) -> None:
        self.repo = repo
        self.branch = branch
        self.token = token
        self.base = f"https://api.github.com/repos/{repo}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "pyplRcd-raw-recorder",
            }
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        r = self.session.request(method, url, timeout=GITHUB_TIMEOUT, **kwargs)
        if not r.ok:
            raise RuntimeError(f"GitHub {method} {url} -> {r.status_code}: {r.text[:1000]}")
        return r.json()

    def upload(self, local_path: Path) -> str:
        content = local_path.read_bytes()

        # GitHub's Git blob API supports blobs up to 100 MB.
        if len(content) >= 100 * 1024 * 1024:
            raise RuntimeError(
                f"{local_path} is {len(content) / 1024 / 1024:.1f} MB; "
                "split the 5-minute window before uploading."
            )

        # 1. Current branch head.
        ref = self._request("GET", f"{self.base}/git/ref/heads/{self.branch}")
        parent_sha = ref["object"]["sha"]

        # 2. Current commit/tree.
        parent = self._request("GET", f"{self.base}/git/commits/{parent_sha}")
        base_tree = parent["tree"]["sha"]

        # 3. Create blob.
        blob = self._request(
            "POST",
            f"{self.base}/git/blobs",
            json={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )

        # 4. Add the new file to the existing tree.
        github_path = f"data/{local_path.name}"
        tree = self._request(
            "POST",
            f"{self.base}/git/trees",
            json={
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": github_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob["sha"],
                    }
                ],
            },
        )

        # 5. Commit.
        commit = self._request(
            "POST",
            f"{self.base}/git/commits",
            json={
                "message": f"raw data: {local_path.name}",
                "tree": tree["sha"],
                "parents": [parent_sha],
            },
        )

        # 6. Move main to the new commit.
        self._request(
            "PATCH",
            f"{self.base}/git/refs/heads/{self.branch}",
            json={"sha": commit["sha"], "force": False},
        )

        return commit["sha"]


def upload_pending(github: GitHubUploader, data_dir: Path, keep: Path | None = None) -> None:
    """Upload every completed local JSONL file that is not the active file."""
    for path in sorted(data_dir.glob("*.jsonl")):
        if keep is not None and path == keep:
            continue
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            sha = github.upload(path)
            print(f"uploaded {path.name} -> {sha[:12]}")
            path.unlink()
        except Exception as exc:
            # Keep the file. The next 5-minute rotation will retry it.
            print(f"UPLOAD FAILED {path}: {exc}", file=sys.stderr)



# ---------------------------------------------------------------------------
# Raw HTTP collection
# ---------------------------------------------------------------------------

def collect_one(session: requests.Session, source: dict[str, Any]) -> dict[str, Any]:
    request_ts = now_iso()
    start = time.monotonic_ns()

    try:
        r = session.get(
            source["url"],
            params=source.get("params") or {},
            timeout=HTTP_TIMEOUT,
        )
        latency_ms = (time.monotonic_ns() - start) / 1_000_000

        # Preserve the response body itself. Do not parse and re-serialize it.
        # This keeps the API response as close to the received bytes as possible.
        payload = r.text

        record: dict[str, Any] = {
            "receive_ts": now_iso(),
            "source_ts": None,
            "exchange": source["name"],
            "url": r.url,
            "fetch_latency_ms": round(latency_ms, 3),
            "http_status": r.status_code,
            "payload": payload,
        }

        return record

    except Exception as exc:
        latency_ms = (time.monotonic_ns() - start) / 1_000_000
        return {
            "receive_ts": now_iso(),
            "source_ts": None,
            "exchange": source["name"],
            "url": build_url(source),
            "fetch_latency_ms": round(latency_ms, 3),
            "http_status": None,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


def build_url(source: dict[str, Any]) -> str:
    params = source.get("params") or {}
    if not params:
        return source["url"]
    return source["url"] + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record raw API responses into 5-minute JSONL files.")
    p.add_argument(
        "--hours",
        type=float,
        default=0,
        help="Run for this many hours. 0 means run until Ctrl-C.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN (or GH_TOKEN) with Contents: write permission.", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    http = requests.Session()
    http.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "pyplRcd-raw-recorder/0.1",
        }
    )

    github = GitHubUploader(REPO, BRANCH, token)
    writer = RawWriter(DATA_DIR)

    started = time.monotonic()
    next_poll = time.monotonic()

    print(f"recording -> {REPO}")
    print(f"local data -> {DATA_DIR}")
    print(f"sources -> {len(SOURCES)}")
    print(f"poll -> every {POLL_SECONDS:g}s")
    print("upload -> every completed 5-minute file")

    try:
        while True:
            if args.hours > 0 and time.monotonic() - started >= args.hours * 3600:
                break

            now = time.time()

            # Rotate at the natural 5-minute boundary.
            if writer.path is not None:
                current_bucket = file_name_for(now)
                if writer.bucket != current_bucket:
                    finished = writer.close()
                    upload_pending(github, DATA_DIR)

            # One raw request per configured source. Nothing is parsed for decisions.
            for source in SOURCES:
                record = collect_one(http, source)
                writer.write(record, time.time())
                print(
                    f"{record['exchange']} "
                    f"{record.get('http_status')} "
                    f"{record['fetch_latency_ms']}ms"
                )

            next_poll += POLL_SECONDS
            sleep_for = next_poll - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_poll = time.monotonic()

    except KeyboardInterrupt:
        print("\nstopped by user")

    finally:
        writer.close()
        upload_pending(github, DATA_DIR)

        http.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
