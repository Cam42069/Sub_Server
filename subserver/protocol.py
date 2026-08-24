"""Wire protocol spoken to the data access node.

The node is contacted over a plain TCP socket and exchanges newline-delimited
JSON ("NDJSON") documents -- one complete JSON object per line, UTF-8 encoded.

Client -> node
--------------
``{"op": "subscribe", "variables": ["*"]}``
    Begin streaming.  ``["*"]`` requests every variable the node publishes.
``{"op": "history", "id": "<token>", "variables": [...], "start": <epoch>, "end": <epoch>}``
    Ask for stored samples in a closed time range.

Node -> client
--------------
``{"op": "welcome", "variables": [...]}``
    Optional greeting listing what the node publishes.
``{"op": "data", "t": <epoch>, "values": {"<name>": <number>, ...}}``
    One timestamped sample set.  ``op`` may be omitted; ``values`` may instead
    be spelled ``v`` or ``data``, and ``t`` may be ``time`` or ``timestamp``.
``{"op": "history", "id": "<token>", "series": {"<name>": {"t": [...], "v": [...]}}, "done": true}``
    Response to a history request, optionally split over several messages with
    ``done`` set only on the last one.
``{"op": "error", "message": "..."}``
    The node rejected the previous request.

Timestamps are epoch seconds.  Values above 1e11 are interpreted as epoch
milliseconds, since no plausible "seconds" timestamp reaches the year 5138.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Iterator

MAX_LINE_BYTES = 8 * 1024 * 1024  # refuse absurd lines rather than buffering forever
_MS_THRESHOLD = 1e11


def normalise_timestamp(value: Any) -> float:
    """Coerce a wire timestamp to epoch seconds."""
    ts = float(value)
    if abs(ts) >= _MS_THRESHOLD:
        ts /= 1000.0
    return ts


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def extract_sample(message: dict[str, Any]) -> tuple[float, dict[str, Any]] | None:
    """Pull ``(timestamp, {name: value})`` out of a data message.

    Returns ``None`` when the message is not a sample.  Accepts the spelling
    variations documented in the module docstring so the server can talk to
    nodes that were not written against this exact schema.
    """
    op = message.get("op")
    if op not in (None, "data", "sample", "update"):
        return None
    for key in ("t", "time", "timestamp", "ts"):
        if key in message:
            raw_ts = message[key]
            break
    else:
        return None
    for key in ("values", "v", "data", "vars"):
        candidate = message.get(key)
        if isinstance(candidate, dict):
            values = candidate
            break
    else:
        # Flat form: every key that is not metadata is treated as a variable.
        reserved = {"op", "t", "time", "timestamp", "ts", "id", "seq"}
        values = {k: v for k, v in message.items() if k not in reserved}
        if not values:
            return None
    try:
        ts = normalise_timestamp(raw_ts)
    except (TypeError, ValueError):
        return None
    return ts, values


def read_lines(sock: socket.socket, chunk_size: int = 65536) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from a socket until it closes.

    Malformed lines are skipped rather than killing the connection; a line that
    grows past :data:`MAX_LINE_BYTES` raises, since that means the peer is not
    speaking NDJSON at all.
    """
    buffer = bytearray()
    while True:
        chunk = sock.recv(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            nl = buffer.find(b"\n")
            if nl < 0:
                break
            raw = bytes(buffer[:nl])
            del buffer[: nl + 1]
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(message, dict):
                yield message
        if len(buffer) > MAX_LINE_BYTES:
            raise ValueError("data node sent an oversized line; is it speaking NDJSON?")
