"""Integration and system tests for the final project server."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional


BASE_DIR = Path(__file__).resolve().parent
SERVER_PROGRAM = BASE_DIR / f"{Path(__file__).stem.removesuffix('_test')}_server.py"
LEGACY_PROGRAM = BASE_DIR / "legacy_pi_server.py"
PROJECT_HOST = "127.0.0.1"
USERNAME = "0123"
PASSWORD = "0123-pw"


def _find_free_port(require_udp: bool = False) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_socket:
        tcp_socket.bind((PROJECT_HOST, 0))
        port = tcp_socket.getsockname()[1]
        if not require_udp:
            return port
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.bind((PROJECT_HOST, port))
    return port


def _wait_for_tcp_server(host: str, port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise RuntimeError(
                f"Program exited before listening on {host}:{port}.\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


def _terminate_process(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=2)


class ProjectServerSystemTest(unittest.TestCase):
    legacy_process = None
    server_process = None
    temporary_directory = None
    statistics_path = None
    project_port = None
    legacy_port = None

    @classmethod
    def setUpClass(cls):
        cls.project_port = _find_free_port()
        cls.legacy_port = _find_free_port(require_udp=True)
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.statistics_path = (
            Path(cls.temporary_directory.name) / "statistics.txt"
        )
        cls._start_legacy()
        cls._start_project_server()

    @classmethod
    def tearDownClass(cls):
        _terminate_process(cls.server_process)
        _terminate_process(cls.legacy_process)
        if cls.temporary_directory is not None:
            cls.temporary_directory.cleanup()

    @classmethod
    def _start_legacy(cls):
        cls.legacy_process = subprocess.Popen(
            [
                sys.executable,
                str(LEGACY_PROGRAM),
                "127.0.0.1",
                str(cls.legacy_port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_tcp_server(
            "127.0.0.1", cls.legacy_port, cls.legacy_process
        )

    @classmethod
    def _start_project_server(cls):
        environment = os.environ.copy()
        environment.update(
            {
                "PROJECT_SERVER_HOST": PROJECT_HOST,
                "PROJECT_SERVER_PORT": str(cls.project_port),
                "PROJECT_STATS_PATH": str(cls.statistics_path),
                "LEGACY_PI_HOST": "127.0.0.1",
                "LEGACY_PI_PORT": str(cls.legacy_port),
                "PYTHONUNBUFFERED": "1",
            }
        )
        cls.server_process = subprocess.Popen(
            [sys.executable, str(SERVER_PROGRAM)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_tcp_server(
            PROJECT_HOST, cls.project_port, cls.server_process
        )

    @classmethod
    def _restart_project_server(cls):
        _terminate_process(cls.server_process)
        cls._start_project_server()

    def post(self, path: str, payload: object, timeout: float = 30):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://{PROJECT_HOST}:{self.project_port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def valid_payload(self, **changes):
        payload = {
            "username": USERNAME,
            "password": PASSWORD,
        }
        payload.update(changes)
        return payload

    def test_01_authentication_is_checked_by_all_services(self):
        invalid_requests = [
            {},
            {"username": 123, "password": "0123-pw"},
            {"username": "a123", "password": "a123-pw"},
            {"username": "0123", "password": "wrong"},
            {"username": "0123"},
        ]
        for path in ("/pi", "/legacy_pi", "/statistics"):
            for payload in invalid_requests:
                with self.subTest(path=path, payload=payload):
                    status, response = self.post(path, payload)
                    self.assertEqual(status, 401)
                    self.assertEqual(response, {"error": "user info error"})

    def test_02_pi_returns_valid_result_and_response_schema(self):
        payload = self.valid_payload(simulations=200_000, concurrency=2)
        status, response = self.post("/pi", payload)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(response),
            {"simulations", "concurrency", "pi", "execution_time"},
        )
        self.assertEqual(response["simulations"], 200_000)
        self.assertEqual(response["concurrency"], 2)
        self.assertAlmostEqual(response["pi"], 3.1415926535, delta=0.02)
        self.assertIsInstance(response["execution_time"], float)
        self.assertGreaterEqual(response["execution_time"], 0)

    def test_03_pi_field_errors(self):
        cases = [
            ({"concurrency": 1}, "missing field simulations"),
            ({"simulations": 99}, "invalid field simulations"),
            ({"simulations": 100_000_001}, "invalid field simulations"),
            ({"simulations": "100"}, "invalid field simulations"),
            ({"simulations": True}, "invalid field simulations"),
            (
                {"simulations": 100, "concurrency": 0},
                "invalid field concurrency",
            ),
            (
                {"simulations": 100, "concurrency": 9},
                "invalid field concurrency",
            ),
            (
                {"simulations": 100, "concurrency": True},
                "invalid field concurrency",
            ),
        ]
        for fields, expected_error in cases:
            with self.subTest(fields=fields):
                status, response = self.post(
                    "/pi", self.valid_payload(**fields)
                )
                self.assertEqual(status, 400)
                self.assertEqual(response, {"error": expected_error})

    def test_04_pi_higher_concurrency_reduces_processing_time(self):
        simulations = 4_000_000
        _status, sequential = self.post(
            "/pi",
            self.valid_payload(
                simulations=simulations,
                concurrency=1,
            ),
            timeout=60,
        )
        _status, parallel = self.post(
            "/pi",
            self.valid_payload(
                simulations=simulations,
                concurrency=4,
            ),
            timeout=60,
        )
        self.assertLess(
            parallel["execution_time"],
            sequential["execution_time"],
            (
                "Four processes should be faster than one process for this "
                f"CPU-bound workload: sequential={sequential}, "
                f"parallel={parallel}"
            ),
        )

    def test_05_legacy_tcp_returns_consistent_result(self):
        payload = self.valid_payload(protocol="tcp", concurrency=3)
        status, response = self.post("/legacy_pi", payload, timeout=30)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(response),
            {
                "protocol",
                "concurrency",
                "num_valid_results",
                "pi",
                "execution_time",
            },
        )
        self.assertEqual(response["protocol"], "tcp")
        self.assertEqual(response["concurrency"], 3)
        self.assertIn(response["num_valid_results"], range(4))
        if response["num_valid_results"] == 0:
            self.assertEqual(response["pi"], 0)
        else:
            self.assertAlmostEqual(response["pi"], 3.1415926535, delta=0.06)

    def test_06_legacy_udp_returns_consistent_result(self):
        payload = self.valid_payload(protocol="udp", concurrency=3)
        status, response = self.post("/legacy_pi", payload, timeout=30)
        self.assertEqual(status, 200)
        self.assertEqual(response["protocol"], "udp")
        self.assertEqual(response["concurrency"], 3)
        self.assertIn(response["num_valid_results"], range(4))
        if response["num_valid_results"] == 0:
            self.assertEqual(response["pi"], 0)
        else:
            self.assertAlmostEqual(response["pi"], 3.1415926535, delta=0.06)

    def test_07_legacy_pi_field_errors(self):
        cases = [
            ({}, "missing field protocol"),
            ({"protocol": "http"}, "invalid field protocol"),
            ({"protocol": 1}, "invalid field protocol"),
            (
                {"protocol": "tcp", "concurrency": 0},
                "invalid field concurrency",
            ),
            (
                {"protocol": "udp", "concurrency": 9},
                "invalid field concurrency",
            ),
        ]
        for fields, expected_error in cases:
            with self.subTest(fields=fields):
                status, response = self.post(
                    "/legacy_pi", self.valid_payload(**fields)
                )
                self.assertEqual(status, 400)
                self.assertEqual(response, {"error": expected_error})

    def test_08_statistics_returns_current_server_state(self):
        self.post("/statistics", self.valid_payload())
        status, response = self.post("/statistics", self.valid_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            set(response), {"statistics", "total_users", "total_requests"}
        )
        self.assertEqual(response["total_users"], len(response["statistics"]))
        self.assertEqual(
            response["total_requests"],
            sum(row["requests"] for row in response["statistics"]),
        )
        user_rows = [
            row
            for row in response["statistics"]
            if row["username"] == USERNAME
        ]
        self.assertEqual(len(user_rows), 1)
        self.assertGreaterEqual(user_rows[0]["requests"], 2)

    def test_09_statistics_updates_are_atomic_under_concurrency(self):
        _status, before = self.post("/statistics", self.valid_payload())
        before_count = next(
            row["requests"]
            for row in before["statistics"]
            if row["username"] == USERNAME
        )
        request_count = 20
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    self.post, "/statistics", self.valid_payload()
                )
                for _ in range(request_count)
            ]
            statuses = [future.result()[0] for future in futures]
        self.assertTrue(all(status == 200 for status in statuses))

        _status, after = self.post("/statistics", self.valid_payload())
        after_count = next(
            row["requests"]
            for row in after["statistics"]
            if row["username"] == USERNAME
        )
        self.assertEqual(after_count, before_count + request_count + 1)

    def test_99_statistics_survive_server_restart(self):
        _status, before_restart = self.post(
            "/statistics", self.valid_payload()
        )
        before_count = next(
            row["requests"]
            for row in before_restart["statistics"]
            if row["username"] == USERNAME
        )

        self._restart_project_server()
        _status, after_restart = self.post(
            "/statistics", self.valid_payload()
        )
        after_count = next(
            row["requests"]
            for row in after_restart["statistics"]
            if row["username"] == USERNAME
        )
        self.assertEqual(after_count, before_count + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
