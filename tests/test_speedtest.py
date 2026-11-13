from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import speedtest as st

PAYLOAD = b"x" * (256 * 1024)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/data")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/error":
            body = b"boom"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *args: object) -> None:  # silence test output
        return


@pytest.fixture()
def server() -> str:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fetch_once_reads_whole_body(server: str) -> None:
    sample = st.fetch_once(f"{server}/data", timeout=5)
    assert sample.bytes_received == len(PAYLOAD)
    assert sample.elapsed > 0
    assert 0 <= sample.ttfb <= sample.elapsed


def test_redirect_is_followed(server: str) -> None:
    sample = st.fetch_once(f"{server}/redirect", timeout=5)
    assert sample.bytes_received == len(PAYLOAD)


def test_http_error_is_raised(server: str) -> None:
    with pytest.raises(urllib.error.HTTPError):
        st.fetch_once(f"{server}/error", timeout=5)


def test_throughput_is_total_bytes_over_total_time() -> None:
    report = st.Report(
        url="x",
        requests=2,
        warmup=0,
        samples=[
            st.Sample(bytes_received=1000, elapsed=1.0, ttfb=0.05),
            st.Sample(bytes_received=1000, elapsed=9.0, ttfb=0.05),
        ],
    )
    # 2000 bytes / 10 s = 200 B/s. Mean of per-request speeds would be 555 B/s.
    assert report.total_bytes == 2000
    assert report.total_time == pytest.approx(10.0)
    assert report.bytes_per_second == pytest.approx(200.0)
    assert report.avg_time == pytest.approx(5.0)


def test_percentile_interpolates() -> None:
    report = st.Report(
        url="x",
        requests=4,
        warmup=0,
        samples=[st.Sample(bytes_received=1, elapsed=t, ttfb=0.01) for t in (1.0, 2.0, 3.0, 4.0)],
    )
    assert report.percentile(0.0) == pytest.approx(1.0)
    assert report.percentile(0.5) == pytest.approx(2.5)
    assert report.percentile(1.0) == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        (0.0, "0.00 bit/s"),
        (999.0, "999.00 bit/s"),
        (1000.0, "1.00 Kbit/s"),
        (1_000_000.0, "1.00 Mbit/s"),
        (1_000_000_000.0, "1.00 Gbit/s"),
    ],
)
def test_human_bits(bits: float, expected: str) -> None:
    assert st.human_bits(bits) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (512.0, "512.00 B"),
        (1024.0, "1.00 KB"),
        (1024.0**2, "1.00 MB"),
    ],
)
def test_human_bytes(size: float, expected: str) -> None:
    assert st.human_bytes(size) == expected


def test_cli_json_output_is_valid(server: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = st.main([f"{server}/data", "-n", "2", "--warmup", "0", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == 2
    assert payload["summary"]["total_bytes"] == 2 * len(PAYLOAD)
    assert len(payload["samples"]) == 2


def test_cli_rejects_zero_requests(server: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        st.main([f"{server}/data", "-n", "0"])
    assert excinfo.value.code == 2
