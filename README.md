# COMP 4620SEF / COMP S362W Project

## Files

- `s12345678_server.py`: Flask project server.
- `s12345678_test.py`: automated integration and system tests.
- `legacy_pi_server.py`: instructor-provided server, reproduced without changes.
- `stats.txt`: persisted request statistics.
- `requirements.txt`: Python dependency versions.

Replace `12345678` in the server and test file names with your eight-digit
student number. The test program discovers the server from its own file name,
so both files must be renamed together and their contents do not need editing.

## Setup and execution

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python legacy_pi_server.py
```

In a second terminal:

```sh
.venv/bin/python s12345678_server.py
```

The project server uses `127.0.0.1:5000` and the legacy server uses
`127.0.0.1:31416` by default. macOS may reserve port 5000 for AirPlay
Receiver. In that case disable AirPlay Receiver, or use:

```sh
PROJECT_SERVER_PORT=5050 .venv/bin/python s12345678_server.py
```

The integration test starts both servers and uses port 5500 to avoid a
conflict with the normal development instance:

```sh
.venv/bin/python s12345678_test.py
```

The test suite creates a temporary statistics file. The submitted
`stats.txt` remains available for normal program execution.

## Statistics file format

Each non-empty line contains a four-digit username and a positive integer:

```text
0123 15
3721 4
```
