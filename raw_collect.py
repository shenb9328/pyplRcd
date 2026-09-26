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
import queue
import sys
import threading
import time
import zipfile
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
        "pages": 3,
        "cursor_param": "next_cursor",
        "response_cursor_key": "next_cursor",
        "terminal_cursors": ["LTE=", ""],
    },
    {
        "name": "kalshi",
        "url": "https://api.elections.kalshi.com/trade-api/v2/events",
        "params": {
            "limit": 200,
            "status": "open",
            "with_nested_markets": "true",
        },
        "pages": 2,
        "cursor_param": "cursor",
        "response_cursor_key": "cursor",
        "terminal_cursors": ["", None],
    },
    {
        "name": "kraken",
        "url": "https://api.kraken.com/0/public/Ticker",
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
# Background Async Zip Compressor
# ---------------------------------------------------------------------------

class AsyncCompressor:
    def __init__(self, compress_level: int = 6) -> None:
        self.compress_level = compress_level
        self.queue: queue.Queue[Path | None] = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def submit(self, path: Path) -> None:
        if path.exists() and path.stat().st_size > 0:
            self.queue.put(path)

    def _worker(self) -> None:
        while True:
            path = self.queue.get()
            if path is None:
                self.queue.task_done()
                break
            try:
                self._compress_file(path)
            except Exception as exc:
                print(f"[compress FAILED] {path}: {exc}", file=sys.stderr)
            finally:
                self.queue.task_done()

    def _compress_file(self, path: Path) -> None:
        if not path.exists():
            return
        orig_size = path.stat().st_size
        if orig_size == 0:
            return

        zip_path = path.with_suffix(".zip")
        tmp_zip = path.with_suffix(".zip.tmp")

        t0 = time.monotonic()
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=self.compress_level) as zf:
            zf.write(path, arcname=path.name)

        tmp_zip.replace(zip_path)
        t_taken = time.monotonic() - t0
        zip_size = zip_path.stat().st_size
        ratio = (zip_size / orig_size) * 100 if orig_size else 100.0

        path.unlink(missing_ok=True)
        print(
            f"[compressed] {path.name} -> {zip_path.name} "
            f"({orig_size / 1024 / 1024:.1f}MB -> {zip_size / 1024 / 1024:.1f}MB, "
            f"{ratio:.1f}%) in {t_taken:.2f}s, original removed."
        )

    def close(self) -> None:
        self.queue.join()
        self.queue.put(None)
        self.thread.join(timeout=60)


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

def collect_source(session: requests.Session, source: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    max_pages = source.get("pages", 1)
    cursor_param = source.get("cursor_param")
    resp_cursor_key = source.get("response_cursor_key")
    terminal_cursors = set(source.get("terminal_cursors") or ["", None])

    current_params = dict(source.get("params") or {})

    for page_idx in range(max_pages):
        start = time.monotonic_ns()
        try:
            r = session.get(
                source["url"],
                params=current_params,
                timeout=HTTP_TIMEOUT,
            )
            latency_ms = (time.monotonic_ns() - start) / 1_000_000
            payload = r.text

            record: dict[str, Any] = {
                "receive_ts": now_iso(),
                "source_ts": None,
                "exchange": source["name"],
                "url": r.url,
                "page": page_idx + 1 if max_pages > 1 else None,
                "fetch_latency_ms": round(latency_ms, 3),
                "http_status": r.status_code,
                "payload": payload,
            }
            records.append(record)

            if page_idx + 1 < max_pages and cursor_param and resp_cursor_key:
                try:
                    data = r.json()
                    next_cur = data.get(resp_cursor_key)
                    if not next_cur or next_cur in terminal_cursors:
                        break
                    current_params[cursor_param] = next_cur
                except Exception:
                    break

        except Exception as exc:
            latency_ms = (time.monotonic_ns() - start) / 1_000_000
            records.append({
                "receive_ts": now_iso(),
                "source_ts": None,
                "exchange": source["name"],
                "url": build_url(source, current_params),
                "page": page_idx + 1 if max_pages > 1 else None,
                "fetch_latency_ms": round(latency_ms, 3),
                "http_status": None,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            })
            break

    return records


def build_url(source: dict[str, Any], params_override: dict[str, Any] | None = None) -> str:
    params = params_override if params_override is not None else (source.get("params") or {})
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
    p.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="Run for this many seconds (for short test runs).",
    )
    p.add_argument(
        "--local-only",
        action="store_true",
        default=True,
        help="Save data locally only, do not upload to GitHub or delete files (default: True).",
    )
    p.add_argument(
        "--upload",
        action="store_true",
        default=False,
        help="Enable uploading completed 5-minute files to GitHub (requires GITHUB_TOKEN).",
    )
    p.add_argument(
        "--zip",
        dest="do_zip",
        action="store_true",
        default=True,
        help="Compress completed 5-minute jsonl files into .zip and delete the original (default: True).",
    )
    p.add_argument(
        "--no-zip",
        dest="do_zip",
        action="store_false",
        help="Disable automatic zip compression.",
    )
    p.add_argument(
        "--compress-level",
        type=int,
        default=6,
        help="Zip compression level (1-9, default: 6).",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=POLL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_SECONDS:g}s).",
    )
    p.add_argument(
        "--data-dir",
        type=str,
        default=str(DATA_DIR),
        help=f"Directory to save raw jsonl/zip files (default: {DATA_DIR}).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    poll_interval = max(0.5, args.poll)
    should_upload = args.upload and not args.local_only

    github: GitHubUploader | None = None
    if should_upload:
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if not token:
            print("ERROR: --upload requested but GITHUB_TOKEN (or GH_TOKEN) is not set.", file=sys.stderr)
            return 2
        github = GitHubUploader(REPO, BRANCH, token)

    compressor = AsyncCompressor(compress_level=args.compress_level) if args.do_zip else None

    # Enqueue any leftover uncompressed jsonl files from previous runs
    if compressor is not None:
        for old_file in data_dir.glob("*.jsonl"):
            compressor.submit(old_file)

    http = requests.Session()
    http.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "pyplRcd-raw-recorder/0.1",
        }
    )

    writer = RawWriter(data_dir)

    started = time.monotonic()
    next_poll = time.monotonic()

    run_duration_desc = ""
    if args.seconds > 0:
        run_duration_desc = f"{args.seconds:g}s"
    elif args.hours > 0:
        run_duration_desc = f"{args.hours:g}h"
    else:
        run_duration_desc = "continuous"

    print(f"mode -> {'upload to GitHub' if should_upload else 'local storage only (data preserved)'}")
    print(f"local data -> {data_dir.resolve()}")
    print(f"sources -> {len(SOURCES)}")
    print(f"poll -> every {poll_interval:g}s")
    print(f"duration -> {run_duration_desc}")
    print(f"compression -> {'enabled (.zip async, level %d)' % args.compress_level if args.do_zip else 'disabled'}")
    if should_upload:
        print(f"upload target -> {REPO}:{BRANCH}")
    else:
        print("upload -> disabled (preserving all files locally)")

    try:
        while True:
            elapsed = time.monotonic() - started
            if args.seconds > 0 and elapsed >= args.seconds:
                break
            if args.hours > 0 and elapsed >= args.hours * 3600:
                break

            now = time.time()

            # Rotate at the natural 5-minute boundary.
            if writer.path is not None:
                current_bucket = file_name_for(now)
                if writer.bucket != current_bucket:
                    finished = writer.close()
                    if finished:
                        print(f"[rotate] closed {finished.name}")
                        if compressor is not None:
                            compressor.submit(finished)
                    if should_upload and github is not None:
                        upload_pending(github, data_dir)

            # Raw requests per configured source (with pagination support).
            for source in SOURCES:
                records = collect_source(http, source)
                for record in records:
                    writer.write(record, time.time())
                    status = record.get("http_status")
                    err = record.get("error", {}).get("type") if "error" in record else None
                    status_str = str(status) if status is not None else f"ERR:{err}"
                    name_with_page = record["exchange"]
                    if record.get("page"):
                        name_with_page += f"(p{record['page']})"
                    print(
                        f"[{record['receive_ts'][:19]}] "
                        f"{name_with_page:<18} "
                        f"{status_str:<6} "
                        f"{record['fetch_latency_ms']}ms"
                    )

            next_poll += poll_interval
            sleep_for = next_poll - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_poll = time.monotonic()

    except KeyboardInterrupt:
        print("\nstopped by user")

    finally:
        finished = writer.close()
        if finished:
            print(f"[final] closed {finished.name}")
            if compressor is not None:
                compressor.submit(finished)

        if compressor is not None:
            print("[shutdown] waiting for background compression to complete...")
            compressor.close()
            print("[shutdown] compression complete.")

        if should_upload and github is not None:
            upload_pending(github, data_dir)

        http.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
