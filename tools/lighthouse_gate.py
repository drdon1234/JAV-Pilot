from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 4174
RUN_COUNT = 3


class LighthouseGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class LighthouseMetrics:
    performance: float
    lcp_ms: float
    cls: float
    tbt_ms: float

    def public_dict(self) -> dict[str, float | bool | int]:
        return {
            "ok": True,
            "runs": RUN_COUNT,
            "performance": round(self.performance, 2),
            "lcp_ms": round(self.lcp_ms, 2),
            "cls": round(self.cls, 4),
            "tbt_ms": round(self.tbt_ms, 2),
        }


def evaluate_lighthouse_reports(reports: list[dict[str, object]]) -> LighthouseMetrics:
    if len(reports) != RUN_COUNT:
        raise LighthouseGateError("exactly three Lighthouse reports are required")
    rows = [_extract_metrics(report) for report in reports]
    metrics = LighthouseMetrics(
        performance=statistics.median(row.performance for row in rows),
        lcp_ms=statistics.median(row.lcp_ms for row in rows),
        cls=statistics.median(row.cls for row in rows),
        tbt_ms=statistics.median(row.tbt_ms for row in rows),
    )
    violations: list[str] = []
    if metrics.performance < 90:
        violations.append("performance score is below 90")
    if metrics.lcp_ms > 2500:
        violations.append("LCP exceeds 2500 ms")
    if metrics.cls > 0.1:
        violations.append("CLS exceeds 0.1")
    if metrics.tbt_ms > 200:
        violations.append("TBT exceeds 200 ms")
    if violations:
        raise LighthouseGateError("; ".join(violations))
    return metrics


def run_lighthouse_gate(*, port: int = PORT) -> LighthouseMetrics:
    fixture = PROJECT_ROOT / "test" / "support" / "e2e_fixture_server.py"
    if not fixture.is_file() or fixture.is_symlink():
        raise LighthouseGateError(
            "local E2E resources are unavailable: test/support/e2e_fixture_server.py"
        )
    npm = shutil.which("npm")
    if not npm:
        raise LighthouseGateError("npm is unavailable")
    chrome_path = _chromium_path()
    base_url = f"http://{HOST}:{port}"
    environment = os.environ.copy()
    environment["CHROME_PATH"] = chrome_path
    server = subprocess.Popen(
        [
            sys.executable,
            str(fixture),
            "--port",
            str(port),
            "--static-root",
            str(PROJECT_ROOT / "frontend" / "dist"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(server, port=port)
        with tempfile.TemporaryDirectory(prefix="jav-pilot-lighthouse-") as temporary:
            temporary_root = Path(temporary)
            reports: list[dict[str, object]] = []
            for index in range(RUN_COUNT):
                output_path = temporary_root / f"report-{index}.json"
                completed = subprocess.run(
                    [
                        npm,
                        "--prefix",
                        "frontend",
                        "exec",
                        "--",
                        "lighthouse",
                        f"{base_url}/search",
                        "--quiet",
                        "--output=json",
                        f"--output-path={output_path}",
                        "--only-categories=performance",
                        "--preset=desktop",
                        "--throttling-method=provided",
                        "--disable-full-page-screenshot",
                        "--no-enable-error-reporting",
                        "--chrome-flags=--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage",
                    ],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                    check=False,
                )
                if completed.returncode != 0 or not output_path.is_file():
                    raise LighthouseGateError("Lighthouse execution failed")
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise LighthouseGateError("Lighthouse report is invalid")
                reports.append(payload)
            return evaluate_lighthouse_reports(reports)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _extract_metrics(report: dict[str, object]) -> LighthouseMetrics:
    try:
        categories = report["categories"]
        audits = report["audits"]
        if not isinstance(categories, dict) or not isinstance(audits, dict):
            raise TypeError
        performance = categories["performance"]
        lcp = audits["largest-contentful-paint"]
        cls = audits["cumulative-layout-shift"]
        tbt = audits["total-blocking-time"]
        if not all(isinstance(value, dict) for value in (performance, lcp, cls, tbt)):
            raise TypeError
        return LighthouseMetrics(
            performance=float(performance["score"]) * 100,
            lcp_ms=float(lcp["numericValue"]),
            cls=float(cls["numericValue"]),
            tbt_ms=float(tbt["numericValue"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise LighthouseGateError("Lighthouse report is missing required metrics") from exc


def _chromium_path() -> str:
    playwright = sync_playwright().start()
    try:
        path = Path(playwright.chromium.executable_path)
    except Error as exc:
        raise LighthouseGateError("independent Chromium is unavailable") from exc
    finally:
        playwright.stop()
    if not path.is_file():
        raise LighthouseGateError("independent Chromium is unavailable")
    return str(path)


def _wait_for_server(
    process: subprocess.Popen[bytes], *, port: int, timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LighthouseGateError("fixture server exited before Lighthouse")
        try:
            with socket.create_connection((HOST, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise LighthouseGateError("fixture server did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the three-sample Lighthouse release performance gate."
    )
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        metrics = run_lighthouse_gate(port=args.port)
    except (LighthouseGateError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        print(
            json.dumps(
                {"ok": False, "error_code": "lighthouse_gate_failed"},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(metrics.public_dict(), ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
