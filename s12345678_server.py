"""COMP 4620SEF / COMP S362W final individual project server."""

import atexit
import json
import logging
import multiprocessing
import os
import re
import secrets
import signal
import socket
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, jsonify, request


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_LEGACY_HOST = "127.0.0.1"
DEFAULT_LEGACY_PORT = 31416
DEFAULT_STATS_PATH = BASE_DIR / "stats.txt"
USERNAME_PATTERN = re.compile(r"[0-9]{4}")
VALID_PI_TEXT_PATTERN = re.compile(r"[0-9.]+")

app = Flask(__name__)


class StatisticsStore:
    """Maintain request counts consistently in memory and in a text file."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._counts = self._load()

    def _load(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        if not self.path.exists():
            return counts

        with self.path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                text = line.strip()
                if not text:
                    continue
                parts = text.split()
                if len(parts) != 2:
                    logging.warning(
                        "Ignoring malformed statistics line %s", line_number
                    )
                    continue
                username, count_text = parts
                try:
                    count = int(count_text)
                except ValueError:
                    logging.warning(
                        "Ignoring invalid count on statistics line %s",
                        line_number,
                    )
                    continue
                if USERNAME_PATTERN.fullmatch(username) and count > 0:
                    counts[username] = count
        return counts

    def increment(self, username: str) -> None:
        """Increment and persist one user's count as a single critical section."""
        with self._lock:
            self._counts[username] = self._counts.get(username, 0) + 1
            self._save_locked()

    def snapshot(self) -> dict[str, object]:
        """Return an internally consistent point-in-time statistics snapshot."""
        with self._lock:
            rows = [
                {"username": username, "requests": self._counts[username]}
                for username in sorted(self._counts)
            ]
            return {
                "statistics": rows,
                "total_users": len(rows),
                "total_requests": sum(self._counts.values()),
            }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            for username in sorted(self._counts):
                file.write(f"{username} {self._counts[username]}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, self.path)


STATS_PATH = Path(os.getenv("PROJECT_STATS_PATH", str(DEFAULT_STATS_PATH)))
STATS = StatisticsStore(STATS_PATH)

_process_executor: Optional[ProcessPoolExecutor] = None
_process_executor_lock = threading.Lock()


def monte_carlo_batch(simulations: int, seed: int) -> int:
    """Return the number of random points that fall inside a unit circle."""
    import random

    generator = random.Random(seed)
    inside = 0
    for _ in range(simulations):
        x = generator.random()
        y = generator.random()
        if x * x + y * y <= 1.0:
            inside += 1
    return inside


def _get_process_executor() -> ProcessPoolExecutor:
    global _process_executor
    with _process_executor_lock:
        if _process_executor is None:
            _process_executor = ProcessPoolExecutor(
                max_workers=8,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _process_executor


def _shutdown_process_executor() -> None:
    global _process_executor
    with _process_executor_lock:
        if _process_executor is not None:
            _process_executor.shutdown(wait=True, cancel_futures=True)
            _process_executor = None


atexit.register(_shutdown_process_executor)


def _terminate_process(signum, _frame) -> None:
    _shutdown_process_executor()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _terminate_process)


def calculate_pi(simulations: int, concurrency: int) -> float:
    """Calculate pi by partitioning equal Monte Carlo workloads."""
    if concurrency == 1:
        inside = monte_carlo_batch(simulations, secrets.randbits(63))
    else:
        base_size, remainder = divmod(simulations, concurrency)
        batch_sizes = [
            base_size + (1 if index < remainder else 0)
            for index in range(concurrency)
        ]
        executor = _get_process_executor()
        futures = [
            executor.submit(
                monte_carlo_batch,
                batch_size,
                secrets.randbits(63),
            )
            for batch_size in batch_sizes
        ]
        inside = sum(future.result() for future in futures)
    return 4.0 * inside / simulations


def _error(message: str, status_code: int):
    return jsonify({"error": message}), status_code


def _authenticate(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        return None
    if password != f"{username}-pw":
        return None
    return username


def _get_concurrency(
    payload: dict[str, object],
) -> Tuple[Optional[int], Optional[object]]:
    concurrency = payload.get("concurrency", 1)
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= 8
    ):
        return None, _error("invalid field concurrency", 400)
    return concurrency, None


def _request_context():
    payload = request.get_json(silent=True)
    username = _authenticate(payload)
    if username is None:
        return None, None, _error("user info error", 401)

    # R4 requires statistics to be updated after authentication and before
    # endpoint-specific field validation.
    STATS.increment(username)
    return payload, username, None


def _valid_legacy_result(text: str) -> Optional[float]:
    text = text.strip()
    if not text or not VALID_PI_TEXT_PATTERN.fullmatch(text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _request_legacy_tcp(host: str, port: int, timeout: float) -> str:
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        chunks = []
        while True:
            chunk = connection.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _request_legacy_udp(host: str, port: int, timeout: float) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(timeout)
        connection.sendto(b"", (host, port))
        data, _ = connection.recvfrom(1024)
    return data.decode("utf-8", errors="replace")


def obtain_legacy_values(
    protocol: str,
    concurrency: int,
    host: str,
    port: int,
) -> List[float]:
    """Obtain legacy values concurrently, treating corrupt responses as invalid."""
    timeout = float(os.getenv("LEGACY_PI_TIMEOUT", "5"))

    def request_one() -> str:
        if protocol == "tcp":
            return _request_legacy_tcp(host, port, timeout)
        return _request_legacy_udp(host, port, timeout)

    values = []
    if concurrency == 1:
        raw_results = [request_one()]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(request_one) for _ in range(concurrency)]
            raw_results = [future.result() for future in futures]

    for raw_result in raw_results:
        value = _valid_legacy_result(raw_result)
        if value is not None:
            values.append(value)
    return values


@app.post("/pi")
def pi_service():
    payload, _username, error_response = _request_context()
    if error_response is not None:
        return error_response

    if "simulations" not in payload:
        return _error("missing field simulations", 400)
    simulations = payload["simulations"]
    if (
        not isinstance(simulations, int)
        or isinstance(simulations, bool)
        or not 100 <= simulations <= 100_000_000
    ):
        return _error("invalid field simulations", 400)

    concurrency, error_response = _get_concurrency(payload)
    if error_response is not None:
        return error_response

    started = time.perf_counter()
    pi_value = calculate_pi(simulations, concurrency)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return jsonify(
        {
            "simulations": simulations,
            "concurrency": concurrency,
            "pi": pi_value,
            "execution_time": round(elapsed_ms, 3),
        }
    )


@app.post("/legacy_pi")
def legacy_pi_service():
    payload, _username, error_response = _request_context()
    if error_response is not None:
        return error_response

    if "protocol" not in payload:
        return _error("missing field protocol", 400)
    protocol = payload["protocol"]
    if not isinstance(protocol, str) or protocol not in {"tcp", "udp"}:
        return _error("invalid field protocol", 400)

    concurrency, error_response = _get_concurrency(payload)
    if error_response is not None:
        return error_response

    host = os.getenv("LEGACY_PI_HOST", DEFAULT_LEGACY_HOST)
    port = int(os.getenv("LEGACY_PI_PORT", str(DEFAULT_LEGACY_PORT)))
    started = time.perf_counter()
    try:
        values = obtain_legacy_values(protocol, concurrency, host, port)
    except (OSError, TimeoutError) as error:
        logging.warning("Legacy server request failed: %s", error)
        values = []
    elapsed_ms = (time.perf_counter() - started) * 1000

    return jsonify(
        {
            "protocol": protocol,
            "concurrency": concurrency,
            "num_valid_results": len(values),
            "pi": sum(values) / len(values) if values else 0,
            "execution_time": round(elapsed_ms, 3),
        }
    )


@app.post("/statistics")
def statistics_service():
    _payload, _username, error_response = _request_context()
    if error_response is not None:
        return error_response
    return jsonify(STATS.snapshot())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    host = os.getenv("PROJECT_SERVER_HOST", DEFAULT_HOST)
    port = int(os.getenv("PROJECT_SERVER_PORT", str(DEFAULT_PORT)))
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
