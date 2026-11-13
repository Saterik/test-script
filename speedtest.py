#!/usr/bin/env python3
"""honest-speedtest - sequential HTTP download benchmark with honest metrics.

Downloads a URL N times sequentially, waiting for the full response body each
time, and reports aggregate throughput. Throughput is defined as total bytes
received divided by total elapsed time, not as the mean of per-request speeds.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TextIO

__version__ = "1.0.0"

CHUNK_SIZE = 64 * 1024
USER_AGENT = f"honest-speedtest/{__version__}"

# ANSI escape codes (disabled unless stdout is a TTY).
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"


@dataclass(frozen=True)
class Sample:
    """Metrics for a single completed request."""

    bytes_received: int
    elapsed: float
    ttfb: float


@dataclass
class Report:
    """Aggregated metrics across all measured requests."""

    url: str
    requests: int
    warmup: int
    samples: list[Sample] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(sample.bytes_received for sample in self.samples)

    @property
    def total_time(self) -> float:
        return sum(sample.elapsed for sample in self.samples)

    @property
    def avg_time(self) -> float:
        if not self.samples:
            return 0.0
        return statistics.fmean(sample.elapsed for sample in self.samples)

    @property
    def bytes_per_second(self) -> float:
        # Throughput by definition: every byte divided by all elapsed time.
        if not self.total_time:
            return 0.0
        return self.total_bytes / self.total_time

    @property
    def bits_per_second(self) -> float:
        return self.bytes_per_second * 8

    @property
    def times(self) -> list[float]:
        return [sample.elapsed for sample in self.samples]

    @property
    def ttfb_values(self) -> list[float]:
        return [sample.ttfb for sample in self.samples]

    @property
    def jitter(self) -> float:
        return statistics.stdev(self.times) if len(self.times) > 1 else 0.0

    def percentile(self, p: float, values: list[float] | None = None) -> float:
        """Linear-interpolated percentile, p in [0, 1]."""
        vals = sorted(values if values is not None else self.times)
        if not vals:
            return 0.0
        if len(vals) == 1:
            return vals[0]
        rank = p * (len(vals) - 1)
        low, high = math.floor(rank), math.ceil(rank)
        if low == high:
            return vals[int(rank)]
        weight = rank - low
        return vals[low] * (1 - weight) + vals[high] * weight


def fetch_once(url: str, timeout: float, chunk_size: int = CHUNK_SIZE) -> Sample:
    """Perform one request, read the whole body, and return its metrics."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",  # measure real bytes, no gzip
        },
    )
    start = time.perf_counter()
    first_byte_at: float | None = None
    received = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            if first_byte_at is None:
                first_byte_at = time.perf_counter()
            received += len(chunk)
    elapsed = time.perf_counter() - start
    ttfb = first_byte_at - start if first_byte_at is not None else elapsed
    return Sample(bytes_received=received, elapsed=elapsed, ttfb=ttfb)


def is_transient(exc: Exception) -> bool:
    """Whether an error is worth retrying (server-side or network hiccup)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in (408, 429)
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def fetch_with_retry(
    url: str, timeout: float, retries: int, backoff: float = 0.5
) -> Sample:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fetch_once(url, timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised when not retryable
            last = exc
            if not is_transient(exc) or attempt == retries:
                raise
            time.sleep(backoff * (2**attempt))
    raise RuntimeError("unreachable") from last


def human_bits(bits_per_second: float) -> str:
    units = ("bit/s", "Kbit/s", "Mbit/s", "Gbit/s", "Tbit/s")
    value = bits_per_second
    for unit in units[:-1]:
        if value < 1000:
            return f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} {units[-1]}"


def human_bytes(byte_count: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = byte_count
    for unit in units[:-1]:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} {units[-1]}"


def _paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


def render_human(report: Report, color: bool) -> str:
    times = report.times
    ttfb = report.ttfb_values
    speed = f"{human_bits(report.bits_per_second)} ({human_bytes(report.bytes_per_second)}/s)"

    def row(label: str, value: str) -> str:
        return f"  {label:<20}{value}"

    lines = [
        _paint("Итог", _BOLD, color),
        row("Цель:", report.url),
        row("Запросов:", f"{report.requests} (прогрев: {report.warmup}, вне статистики)"),
        "",
        row("Всего скачано:", human_bytes(report.total_bytes)),
        row("Суммарное время:", f"{report.total_time:.3f} s"),
        row("Среднее время:", f"{report.avg_time:.3f} s"),
        row("Скорость:", _paint(speed, _GREEN + _BOLD, color)),
        "",
        "  Время запроса:",
        f"    min {min(times):.3f}s  p50 {report.percentile(0.50):.3f}s  "
        f"p95 {report.percentile(0.95):.3f}s  max {max(times):.3f}s  "
        f"jitter {report.jitter:.3f}s",
        "  TTFB (до первого байта):",
        f"    min {min(ttfb):.3f}s  p50 {report.percentile(0.50, ttfb):.3f}s  "
        f"max {max(ttfb):.3f}s",
    ]
    return "\n".join(lines)


def report_to_dict(report: Report) -> dict[str, object]:
    times = report.times
    ttfb = report.ttfb_values
    summary: dict[str, object] = {
        "total_bytes": report.total_bytes,
        "total_time": report.total_time,
        "avg_time": report.avg_time,
        "bytes_per_second": report.bytes_per_second,
        "bits_per_second": report.bits_per_second,
        "jitter": report.jitter,
    }
    if times:
        summary.update(
            {
                "min_time": min(times),
                "p50_time": report.percentile(0.50),
                "p95_time": report.percentile(0.95),
                "max_time": max(times),
            }
        )
    if ttfb:
        summary.update(
            {
                "ttfb_min": min(ttfb),
                "ttfb_p50": report.percentile(0.50, ttfb),
                "ttfb_max": max(ttfb),
            }
        )
    return {
        "url": report.url,
        "requests": report.requests,
        "warmup": report.warmup,
        "summary": summary,
        "samples": [
            {"bytes": s.bytes_received, "elapsed": s.elapsed, "ttfb": s.ttfb}
            for s in report.samples
        ],
    }


def run_benchmark(
    url: str,
    requests: int,
    warmup: int,
    timeout: float,
    retries: int,
    progress: TextIO | None,
) -> Report:
    for index in range(1, warmup + 1):
        if progress is not None:
            print(f"Прогрев {index}/{warmup} ...", file=progress)
        fetch_with_retry(url, timeout, retries)

    report = Report(url=url, requests=requests, warmup=warmup)
    for index in range(1, requests + 1):
        sample = fetch_with_retry(url, timeout, retries)
        report.samples.append(sample)
        if progress is not None:
            speed = (
                human_bits(sample.bytes_received * 8 / sample.elapsed)
                if sample.elapsed
                else "n/a"
            )
            print(
                f"[{index:>2}/{requests}] {human_bytes(sample.bytes_received):>10}  "
                f"{sample.elapsed:6.3f}s  TTFB {sample.ttfb:6.3f}s  {speed}",
                file=progress,
            )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speedtest.py",
        description=(
            "Последовательный HTTP-бенчмарк. Скачивает адрес N раз, "
            "дожидается полного ответа и считает честную пропускную способность."
        ),
        epilog="Скорость = суммарные байты / суммарное время (throughput).",
    )
    parser.add_argument("url", help="адрес для скачивания (например, тяжёлая картинка)")
    parser.add_argument(
        "-n", "--requests", type=int, default=10, help="число замеров (по умолчанию: 10)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="прогревных запросов вне статистики (по умолчанию: 1)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="таймаут запроса, сек (по умолчанию: 30)"
    )
    parser.add_argument(
        "--retries", type=int, default=2, help="повторы на транзиентных ошибках (по умолчанию: 2)"
    )
    parser.add_argument(
        "--json", action="store_true", help="машинный JSON в stdout (прогресс идёт в stderr)"
    )
    parser.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.requests < 1:
        parser.error("--requests должен быть >= 1")
    if args.warmup < 0:
        parser.error("--warmup не может быть отрицательным")
    if args.timeout <= 0:
        parser.error("--timeout должен быть > 0")

    color = not args.no_color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    # JSON owns stdout, so progress goes to stderr there. In human mode progress
    # stays on stdout to avoid Windows PowerShell painting stderr red.
    progress = sys.stderr if args.json else sys.stdout

    try:
        report = run_benchmark(
            url=args.url,
            requests=args.requests,
            warmup=args.warmup,
            timeout=args.timeout,
            retries=args.retries,
            progress=progress,
        )
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"Ошибка запроса: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(render_human(report, color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
