#!/usr/bin/env python3
"""Local HTTP server with a heavy file, for testing speedtest.py on loopback.

Generates testdata/heavy.bin of the requested size (once) and serves ONLY the
testdata/ directory on 127.0.0.1. Other project files are never exposed.

Also answers GET/HEAD /health with "ok" and ignores client aborts (WinError
10053 / broken pipe), which are normal when a download is interrupted.
"""
from __future__ import annotations

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_SIZE_MB = 20
DATA_DIR = Path(__file__).resolve().parent / "testdata"
TARGET = DATA_DIR / "heavy.bin"

_ABORT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class TestFileHandler(SimpleHTTPRequestHandler):
    """Static handler with a health endpoint and quiet client aborts."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/health":
            self._send_health(write_body=True)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/health":
            self._send_health(write_body=False)
            return
        super().do_HEAD()

    def _send_health(self, write_body: bool) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib API
        if args and "/health" in str(args[0]):
            return
        super().log_message(format, *args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except _ABORT_ERRORS:
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except _ABORT_ERRORS:
            pass


def ensure_file(size_mb: int) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    size = size_mb * 1024 * 1024
    if TARGET.exists() and TARGET.stat().st_size == size:
        return TARGET
    block = os.urandom(1024 * 1024)
    remaining = size
    with TARGET.open("wb") as handle:
        while remaining > 0:
            handle.write(block[: min(remaining, len(block))])
            remaining -= len(block)
    return TARGET


def main() -> int:
    parser = argparse.ArgumentParser(description="Раздать тяжёлый файл по localhost.")
    parser.add_argument("-p", "--port", type=int, default=8000, help="порт (по умолчанию: 8000)")
    parser.add_argument(
        "--size-mb", type=int, default=DEFAULT_SIZE_MB, help="размер файла в MB (по умолчанию: 20)"
    )
    args = parser.parse_args()

    target = ensure_file(args.size_mb)
    handler = partial(TestFileHandler, directory=str(DATA_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler)

    print(f"Файл: {target} ({target.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"URL:  http://127.0.0.1:{args.port}/{target.name}")
    print(f"Health: http://127.0.0.1:{args.port}/health")
    print("Ctrl+C для остановки")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
