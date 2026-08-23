#!/usr/bin/env python3
"""A stand-in data access node, for developing and demoing Sub_Server.

It speaks the NDJSON protocol documented in ``subserver/protocol.py``: it
accepts ``subscribe`` and ``history`` requests and answers both from the same
deterministic synthetic signals, so a live plot and a static plot of the same
window agree with each other.

Run it in one terminal and ``run_server.py`` in another::

    python3 mock_data_node.py --port 980
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import socketserver
import threading
import time

log = logging.getLogger("mock_data_node")

# name -> (base, amplitude, period_seconds, noise)
SIGNALS: dict[str, tuple[float, float, float, float]] = {
    "reactor.core.temp_a": (540.0, 25.0, 300.0, 0.8),
    "reactor.core.temp_b": (536.0, 22.0, 300.0, 0.8),
    "reactor.core.pressure": (14.7, 1.4, 173.0, 0.05),
    "reactor.coolant.flow": (820.0, 60.0, 97.0, 4.0),
    "reactor.coolant.inlet_temp": (295.0, 6.0, 611.0, 0.3),
    "grid.bus.voltage": (13800.0, 90.0, 41.0, 12.0),
    "grid.bus.current": (410.0, 35.0, 61.0, 3.0),
    "grid.frequency": (60.0, 0.03, 27.0, 0.004),
    "turbine.rpm": (3600.0, 12.0, 53.0, 1.5),
    "turbine.vibration": (0.42, 0.18, 19.0, 0.03),
    "aux.ambient_temp": (72.0, 8.0, 86400.0, 0.2),
    "aux.humidity": (45.0, 12.0, 86400.0, 0.5),
}


def value_at(name: str, ts: float) -> float:
    """Deterministic-plus-noise value for a signal at a point in time."""
    base, amplitude, period, noise = SIGNALS[name]
    phase = (hash(name) % 1000) / 1000.0 * math.tau
    wave = math.sin(ts / period * math.tau + phase)
    # A slow second harmonic keeps the traces from looking like pure sine waves.
    wave += 0.25 * math.sin(ts / (period / 3.0) * math.tau + phase)
    jitter = random.Random(f"{name}:{int(ts * 10)}").gauss(0.0, noise)
    return base + amplitude * wave + jitter


class NodeHandler(socketserver.BaseRequestHandler):
    """One connected client: either a subscriber or a history requester."""

    def handle(self) -> None:  # noqa: D102
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("client connected: %s", peer)
        self.request.settimeout(None)
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
                        self._send({"op": "error", "message": "malformed JSON"})
                        continue
                    if not self._dispatch(message):
                        return
        except (ConnectionError, OSError) as exc:
            log.info("client %s dropped: %s", peer, exc)
        finally:
            log.info("client disconnected: %s", peer)

    def _dispatch(self, message: dict) -> bool:
        """Handle one request.  Returns False when the connection should close."""
        op = message.get("op")
        if op == "subscribe":
            self._stream(message.get("variables") or ["*"])
            return False
        if op == "history":
            self._history(message)
            return True
        self._send({"op": "error", "message": f"unknown op {op!r}"})
        return True

    def _send(self, message: dict) -> None:
        self.request.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())

    @staticmethod
    def _select(patterns: list[str]) -> list[str]:
        if not patterns or "*" in patterns:
            return list(SIGNALS)
        return [name for name in SIGNALS if name in patterns]

    def _stream(self, patterns: list[str]) -> None:
        names = self._select(patterns)
        self._send({"op": "welcome", "variables": names})
        interval = float(getattr(self.server, "interval", 1.0))
        while not getattr(self.server, "shutting_down", False):
            ts = time.time()
            self._send({"op": "data", "t": ts, "values": {n: round(value_at(n, ts), 4) for n in names}})
            time.sleep(interval)

    def _history(self, message: dict) -> None:
        names = self._select(message.get("variables") or ["*"])
        try:
            start = float(message["start"])
            end = float(message["end"])
        except (KeyError, TypeError, ValueError):
            self._send({"op": "error", "message": "history needs numeric start and end"})
            return
        end = min(end, time.time())
        if end <= start:
            self._send({"op": "history", "id": message.get("id"), "series": {}, "done": True})
            return
        step = float(getattr(self.server, "interval", 1.0))
        # Coarsen very long requests so a reply stays a sane size.
        count = int((end - start) / step)
        if count > 200_000:
            step = (end - start) / 200_000
            count = 200_000
        series = {}
        for name in names:
            times = [start + i * step for i in range(count)]
            series[name] = {
                "t": [round(t, 3) for t in times],
                "v": [round(value_at(name, t), 4) for t in times],
            }
        self._send({"op": "history", "id": message.get("id"), "series": series, "done": True})
        log.info("served history: %d vars x %d samples", len(names), count)


class ThreadedNode(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    shutting_down = False
    interval = 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=980)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    server = ThreadedNode((args.host, args.port), NodeHandler)
    server.interval = args.interval
    log.info("mock data node listening on %s:%d (%d signals)", args.host, args.port, len(SIGNALS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutting_down = True
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
