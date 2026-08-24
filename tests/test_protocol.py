"""Tests for the data-node wire protocol and the subscriber."""

import json
import socket
import threading
import time

import pytest

from subserver import protocol
from subserver.ringstore import RamStore
from subserver.subscriber import Subscriber


def test_extract_sample_accepts_the_documented_form():
    assert protocol.extract_sample({"op": "data", "t": 1.0, "values": {"a": 1}}) == (1.0, {"a": 1})


@pytest.mark.parametrize("message,expected_time", [
    ({"t": 1.0, "values": {"a": 1}}, 1.0),                       # op omitted
    ({"op": "sample", "time": 2.0, "v": {"a": 1}}, 2.0),         # alternate spellings
    ({"timestamp": 1700000000000, "data": {"a": 1}}, 1.7e9),     # milliseconds
    ({"t": 5.0, "a": 1, "b": 2}, 5.0),                           # flat form
])
def test_extract_sample_tolerates_spelling_variations(message, expected_time):
    result = protocol.extract_sample(message)
    assert result is not None and result[0] == pytest.approx(expected_time)


@pytest.mark.parametrize("message", [
    {"op": "welcome", "variables": []},
    {"op": "error", "message": "no"},
    {"values": {"a": 1}},          # no timestamp
    {"t": "not a time", "values": {"a": 1}},
    {"t": 1.0},                    # no values
])
def test_extract_sample_ignores_non_samples(message):
    assert protocol.extract_sample(message) is None


def test_milliseconds_are_converted_to_seconds():
    assert protocol.normalise_timestamp(1700000000000) == 1700000000.0
    assert protocol.normalise_timestamp(1700000000) == 1700000000.0


def test_encode_is_one_json_object_per_line():
    raw = protocol.encode({"op": "subscribe", "variables": ["*"]})
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw) == {"op": "subscribe", "variables": ["*"]}


def _serve(payload: bytes):
    """Run a one-shot TCP server that sends `payload` then closes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(payload)
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def test_read_lines_skips_malformed_lines_and_splits_partial_ones():
    port, thread = _serve(b'{"a": 1}\nnot json\n\n{"b": 2}\n')
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"hello\n")
    messages = list(protocol.read_lines(sock, chunk_size=3))   # tiny reads split lines
    sock.close()
    thread.join(timeout=2)
    assert messages == [{"a": 1}, {"b": 2}]


def test_subscriber_ingests_a_stream_into_the_store():
    payload = b"".join(
        protocol.encode({"op": "welcome", "variables": ["a.b"]})
        if i == 0 else
        protocol.encode({"op": "data", "t": 1000.0 + i, "values": {"a.b": float(i)}})
        for i in range(5)
    )
    port, thread = _serve(payload)
    store = RamStore()
    subscriber = Subscriber(store, "127.0.0.1", port, ["*"], backfill_seconds=0)
    subscriber.start()
    deadline = time.time() + 5
    while len(store.query("a.b")[0]) < 4 and time.time() < deadline:
        time.sleep(0.05)
    subscriber.stop()
    thread.join(timeout=2)
    assert store.query("a.b")[1].tolist() == [1.0, 2.0, 3.0, 4.0]
    assert subscriber.node_variables == ["a.b"]


def test_subscriber_filters_to_the_requested_variables():
    payload = protocol.encode({"op": "data", "t": 1.0, "values": {"keep.me": 1, "drop.me": 2}})
    port, thread = _serve(payload)
    store = RamStore()
    subscriber = Subscriber(store, "127.0.0.1", port, ["keep.*"], backfill_seconds=0)
    subscriber.start()
    deadline = time.time() + 5
    while not store.has("keep.me") and time.time() < deadline:
        time.sleep(0.05)
    subscriber.stop()
    thread.join(timeout=2)
    assert store.has("keep.me") and not store.has("drop.me")


def test_subscriber_survives_an_unreachable_node():
    store = RamStore()
    # Port 1 is reserved and refuses connections; the thread must keep running.
    subscriber = Subscriber(store, "127.0.0.1", 1, ["*"], reconnect_min_delay=0.05, backfill_seconds=0)
    subscriber.start()
    time.sleep(0.5)
    assert subscriber.connected is False
    assert subscriber.last_error
    assert subscriber.connect_attempts >= 1
    subscriber.stop()
    assert subscriber.status()["connected"] is False
