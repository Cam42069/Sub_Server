"""Background subscription client for the data access node.

Runs one daemon thread that keeps a socket open to the node, decodes the NDJSON
stream and feeds every sample into the :class:`~subserver.ringstore.RamStore`.
The connection is re-established with exponential backoff whenever it drops, so
the server survives a node restart without operator action.
"""

from __future__ import annotations

import fnmatch
import logging
import socket
import threading
import time
import uuid
from typing import Any

import numpy as np

from . import protocol
from .ringstore import RamStore

log = logging.getLogger(__name__)

HISTORY_TIMEOUT = 30.0
CONNECT_TIMEOUT = 10.0


class Subscriber:
    """Maintains the live subscription and services history backfills."""

    def __init__(
        self,
        store: RamStore,
        host: str,
        port: int,
        variables: list[str] | None = None,
        *,
        reconnect_min_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        backfill_seconds: float = 0.0,
        allow_history_requests: bool = True,
    ):
        self.store = store
        self.host = host
        self.port = int(port)
        self.variables = list(variables or ["*"])
        self.reconnect_min_delay = float(reconnect_min_delay)
        self.reconnect_max_delay = float(reconnect_max_delay)
        self.backfill_seconds = float(backfill_seconds or 0.0)
        self.allow_history_requests = bool(allow_history_requests)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

        # Status, surfaced on the home page.
        self.connected = False
        self.last_error: str | None = None
        self.last_message_at: float | None = None
        self.connected_since: float | None = None
        self.connect_attempts = 0
        self.node_variables: list[str] = []
        self._history_supported: bool | None = None

    # -- Lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="subscriber", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- Subscription loop ------------------------------------------------------
    def _run(self) -> None:
        delay = self.reconnect_min_delay
        while not self._stop.is_set():
            try:
                self.connect_attempts += 1
                self._session()
                delay = self.reconnect_min_delay  # a clean session resets backoff
            except Exception as exc:  # noqa: BLE001 - a dead node must not kill the thread
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("data node connection failed: %s", self.last_error)
            finally:
                self.connected = False
                self.connected_since = None
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.reconnect_max_delay)

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(None)
        with self._lock:
            self._sock = sock
        try:
            sock.sendall(protocol.encode({"op": "subscribe", "variables": self.variables}))
            self.connected = True
            self.connected_since = time.time()
            self.last_error = None
            log.info("subscribed to data node at %s:%s", self.host, self.port)
            if self.backfill_seconds > 0:
                threading.Thread(target=self._startup_backfill, daemon=True).start()
            for message in protocol.read_lines(sock):
                if self._stop.is_set():
                    break
                self._handle(message)
        finally:
            with self._lock:
                if self._sock is sock:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        if op == "welcome":
            names = message.get("variables")
            if isinstance(names, list):
                self.node_variables = [str(n) for n in names]
            return
        if op == "error":
            self.last_error = str(message.get("message", "node reported an error"))
            log.warning("data node error: %s", self.last_error)
            return
        if op == "history":
            return  # history replies arrive on their own connection
        sample = protocol.extract_sample(message)
        if sample is None:
            return
        ts, values = sample
        for name, value in values.items():
            if self._wanted(name):
                self.store.add_sample(str(name), ts, value)
        self.last_message_at = time.time()

    def _wanted(self, name: str) -> bool:
        if not self.variables or "*" in self.variables:
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.variables)

    # -- History ----------------------------------------------------------------
    def _startup_backfill(self) -> None:
        end = time.time()
        start = end - self.backfill_seconds
        try:
            loaded = self.fetch_history(self.variables, start, end)
            if loaded:
                log.info("backfilled %d samples from the data node", loaded)
        except Exception as exc:  # noqa: BLE001
            log.info("history backfill unavailable: %s", exc)

    @property
    def history_supported(self) -> bool | None:
        """``True``/``False`` once probed, ``None`` while still unknown."""
        return self._history_supported

    def fetch_history(self, variables: list[str], start: float, end: float) -> int:
        """Fetch stored samples from the node and merge them into the store.

        Opens its own short-lived connection so a slow history reply cannot
        stall the live stream.  Returns the number of samples merged.
        """
        if not self.allow_history_requests:
            return 0
        if end <= start:
            return 0
        token = uuid.uuid4().hex
        request = {
            "op": "history",
            "id": token,
            "variables": list(variables),
            "start": float(start),
            "end": float(end),
        }
        merged = 0
        deadline = time.monotonic() + HISTORY_TIMEOUT
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        try:
            sock.settimeout(HISTORY_TIMEOUT)
            sock.sendall(protocol.encode(request))
            for message in protocol.read_lines(sock):
                if time.monotonic() > deadline:
                    break
                if message.get("op") == "error":
                    self._history_supported = False
                    raise RuntimeError(str(message.get("message", "history rejected")))
                if message.get("op") != "history":
                    continue
                if message.get("id") not in (None, token):
                    continue
                merged += self._merge_history(message)
                if message.get("done", True):
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass
        self._history_supported = True
        return merged

    def _merge_history(self, message: dict[str, Any]) -> int:
        series = message.get("series") or message.get("variables") or {}
        if not isinstance(series, dict):
            return 0
        merged = 0
        for name, payload in series.items():
            if not isinstance(payload, dict):
                continue
            times = payload.get("t") or payload.get("time") or []
            values = payload.get("v") or payload.get("values") or []
            if not isinstance(times, list) or not isinstance(values, list):
                continue
            if len(times) != len(values):
                continue
            if not times:
                continue
            t_arr = np.array([protocol.normalise_timestamp(x) for x in times], dtype=np.float64)
            v_arr = np.array(
                [float(x) if isinstance(x, (int, float)) else np.nan for x in values],
                dtype=np.float64,
            )
            merged += self.store.add_block(str(name), t_arr, v_arr, merge=True)
        return merged

    # -- Status -----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "variables": self.variables,
            "last_error": self.last_error,
            "last_message_at": self.last_message_at,
            "connected_since": self.connected_since,
            "connect_attempts": self.connect_attempts,
            "history_supported": self._history_supported,
        }
