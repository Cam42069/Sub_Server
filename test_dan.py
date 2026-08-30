#!/usr/bin/env python3
"""A tiny stand-in data access node that publishes random data.

Speaks the same NDJSON socket protocol as the real node (see
``subserver/protocol.py``), so it can stand in for one while testing the rest
of Sub_Server.  Standard library only -- no dependencies.

    python3 test_dan.py                      # 127.0.0.1:980, one sample/second
    python3 test_dan.py --port 9000 --interval 0.25
    python3 test_dan.py --host 0.0.0.0       # reachable from other machines

Then point the server at it:

    python3 run_server.py --node-port 9000

Values are random walks rather than white noise: a walk drifts and wanders, so
the plots have shape to look at, while white noise just fills a band.  Every
sample that goes out is also kept in memory, so a ``history`` request returns
exactly what was streamed earlier and a static plot lines up with the live one.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import random
import socketserver
import threading
import time
from collections import deque

log = logging.getLogger("test_dan")

# name -> (start value, step size, low, high)
# The walk reflects off the low/high bounds, so a variable stays in a sensible
# range instead of drifting away over a long run.
SIGNALS: dict[str, tuple[float, float, float, float]] = {
    "dan.temperature": (72.0, 0.30, 55.0, 95.0),
    "dan.pressure": (14.7, 0.05, 12.0, 18.0),
    "dan.flow": (500.0, 6.00, 300.0, 700.0),
    "dan.voltage": (120.0, 0.60, 100.0, 140.0),
    "dan.noise": (0.0, 4.00, -30.0, 30.0),
}
# Two more with a different character, handled separately below.
COUNTER = "dan.counter"        # climbs steadily -- makes ordering problems obvious
STATE = "dan.state"            # flips between 0 and 1 -- good for step plots

ALL_NAMES = list(SIGNALS) + [COUNTER, STATE]

MAX_HISTORY_SAMPLES = 200_000  # ~2 days at one per second
MAX_PREFILL_TICKS = 20_000


class DataFeed:
    """Generates samples on a timer and remembers them for history requests."""

    def __init__(self, interval: float, backlog_seconds: float):
        self.interval = max(0.01, float(interval))
        self._lock = threading.Lock()
        self._new_sample = threading.Condition(self._lock)
        self._samples: deque[tuple[float, dict[str, float]]] = deque(maxlen=MAX_HISTORY_SAMPLES)
        self._dropped = 0          # samples aged out of the deque, for indexing
        self._values = {name: spec[0] for name, spec in SIGNALS.items()}
        self._counter = 0.0
        self._state = 0.0
        self.stopping = threading.Event()
        self._prefill(backlog_seconds)

    # -- generation -------------------------------------------------------------
    def _step(self, ts: float) -> dict[str, float]:
        """Advance every signal one tick and return the new values."""
        values: dict[str, float] = {}
        for name, (_, step, low, high) in SIGNALS.items():
            value = self._values[name] + random.gauss(0.0, step)
            # Reflect at the bounds rather than clamping, which would otherwise
            # leave the trace stuck flat against a limit.
            if value < low:
                value = low + (low - value)
            elif value > high:
                value = high - (value - high)
            self._values[name] = min(high, max(low, value))
            values[name] = round(self._values[name], 4)

        self._counter += 1.0
        values[COUNTER] = self._counter
        if random.random() < 0.05:          # flip roughly every 20 ticks
            self._state = 0.0 if self._state else 1.0
        values[STATE] = self._state
        return values

    def _prefill(self, backlog_seconds: float) -> None:
        """Generate a backlog ending now, so history works from the first second."""
        ticks = min(MAX_PREFILL_TICKS, int(max(0.0, backlog_seconds) / self.interval))
        if ticks <= 0:
            return
        start = time.time() - ticks * self.interval
        for i in range(ticks):
            ts = start + i * self.interval
            self._samples.append((ts, self._step(ts)))
        log.info("pre-filled %d samples covering the last %.0f s", ticks, ticks * self.interval)

    def run(self) -> None:
        """Produce one sample set per interval until stopped."""
        next_tick = time.time()
        while not self.stopping.is_set():
            ts = time.time()
            sample = (ts, self._step(ts))
            with self._new_sample:
                if len(self._samples) == self._samples.maxlen:
                    self._dropped += 1
                self._samples.append(sample)
                self._new_sample.notify_all()
            next_tick += self.interval
            # Sleep to the next tick, but never negative if we fell behind.
            self.stopping.wait(max(0.0, next_tick - time.time()))

    # -- readers ----------------------------------------------------------------
    def cursor(self) -> int:
        """An index a subscriber can use to ask for "everything after this"."""
        with self._lock:
            return self._dropped + len(self._samples)

    def wait_for(self, cursor: int, timeout: float = 1.0):
        """Block until samples after `cursor` exist; return them and a new cursor."""
        with self._new_sample:
            end = self._dropped + len(self._samples)
            if cursor >= end:
                self._new_sample.wait(timeout)
                end = self._dropped + len(self._samples)
            start = max(cursor, self._dropped)
            if start >= end:
                return [], end
            offset = start - self._dropped
            return list(self._samples)[offset:], end

    def history(self, start: float, end: float) -> list[tuple[float, dict[str, float]]]:
        with self._lock:
            return [(ts, values) for ts, values in self._samples if start <= ts <= end]


def select(patterns: list[str]) -> list[str]:
    """Resolve requested variable patterns against what this node publishes."""
    if not patterns or "*" in patterns:
        return list(ALL_NAMES)
    return [name for name in ALL_NAMES
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)]


class Handler(socketserver.BaseRequestHandler):
    """One connected client: either a subscriber or a history request."""

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("client connected: %s", peer)
        buffer = bytearray()
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = bytes(buffer).partition(b"\n")
                    buffer = bytearray(rest)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        self.send({"op": "error", "message": "malformed JSON"})
                        continue
                    if not self.dispatch(message):
                        return
        except (ConnectionError, OSError) as exc:
            log.info("client %s dropped: %s", peer, exc)
        finally:
            log.info("client disconnected: %s", peer)

    def send(self, message: dict) -> None:
        self.request.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())

    def dispatch(self, message: dict) -> bool:
        """Handle one request; return False when the connection should close."""
        op = message.get("op")
        if op == "subscribe":
            self.stream(select(message.get("variables") or ["*"]))
            return False
        if op == "history":
            self.history(message)
            return True
        self.send({"op": "error", "message": f"unknown op {op!r}"})
        return True

    def stream(self, names: list[str]) -> None:
        feed: DataFeed = self.server.feed
        self.send({"op": "welcome", "variables": names})
        cursor = feed.cursor()
        while not feed.stopping.is_set():
            samples, cursor = feed.wait_for(cursor)
            for ts, values in samples:
                self.send({"op": "data", "t": round(ts, 3),
                           "values": {n: values[n] for n in names if n in values}})

    def history(self, message: dict) -> None:
        feed: DataFeed = self.server.feed
        names = select(message.get("variables") or ["*"])
        try:
            start = float(message["start"])
            end = float(message["end"])
        except (KeyError, TypeError, ValueError):
            self.send({"op": "error", "message": "history needs numeric start and end"})
            return
        rows = feed.history(start, end)
        series = {name: {"t": [], "v": []} for name in names}
        for ts, values in rows:
            for name in names:
                if name in values:
                    series[name]["t"].append(round(ts, 3))
                    series[name]["v"].append(values[name])
        self.send({"op": "history", "id": message.get("id"), "series": series, "done": True})
        log.info("served history: %d variables x %d samples", len(names), len(rows))


class Node(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    feed: DataFeed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=980, help="port to listen on")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--backlog", type=float, default=3600.0,
                        help="seconds of history to generate at start-up (0 to disable)")
    parser.add_argument("--seed", type=int, help="seed the generator for repeatable runs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    if args.seed is not None:
        random.seed(args.seed)

    feed = DataFeed(args.interval, args.backlog)
    server = Node((args.host, args.port), Handler)
    server.feed = feed

    threading.Thread(target=feed.run, name="feed", daemon=True).start()
    threading.Thread(target=server.serve_forever, name="server", daemon=True).start()

    print(f"\n  test_dan listening on {args.host}:{args.port}")
    print(f"  {len(ALL_NAMES)} variables, one sample every {args.interval}s:")
    for name in ALL_NAMES:
        print(f"    {name}")
    print(f"\n  Point the server at it:  python3 run_server.py --node-port {args.port}")
    print("  Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
        feed.stopping.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
