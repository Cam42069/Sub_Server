"""Tests for the in-RAM sample store."""


import numpy as np
import pytest

from subserver.ringstore import BYTES_PER_SAMPLE, RamStore, VarBuffer, decimate


def test_append_and_query_in_order():
    store = RamStore()
    for i in range(100):
        store.add_sample("a", 1000.0 + i, float(i))
    times, values = store.query("a", 1010, 1020)
    assert times[0] == 1010 and times[-1] == 1020
    assert values.tolist() == list(range(10, 21))


def test_query_unknown_variable_is_empty():
    store = RamStore()
    times, values = store.query("nope")
    assert times.size == 0 and values.size == 0


def test_non_numeric_and_non_finite_values_are_rejected():
    store = RamStore()
    assert store.add_sample("a", 1.0, 5) is True
    assert store.add_sample("a", 2.0, "text") is False
    assert store.add_sample("a", 3.0, None) is False
    assert store.add_sample("a", 4.0, float("nan")) is False
    assert store.add_sample("a", float("inf"), 1.0) is False
    assert len(store.query("a")[0]) == 1
    assert store.stats()["rejected"] == 4


def test_memory_ceiling_is_enforced():
    store = RamStore(max_bytes=100_000)
    for i in range(200_000):
        store.add_sample("a", float(i), float(i))
    stats = store.stats()
    assert stats["bytes"] <= store.max_bytes
    assert stats["dropped"] > 0
    # The data that survives is the most recent.
    assert store.latest("a") == 199_999.0


def test_eviction_keeps_variables_time_aligned():
    store = RamStore(max_bytes=60_000)
    for i in range(20_000):
        store.add_sample("a", float(i), 1.0)
        store.add_sample("b", float(i), 2.0)
    first_a, first_b = store.earliest("a"), store.earliest("b")
    # A common cut-off means the two buffers start within one sample of each other.
    assert abs(first_a - first_b) <= 1.0


def test_max_age_drops_old_samples():
    store = RamStore(max_bytes=10**9, max_age_seconds=100)
    for i in range(0, 500):
        store.add_sample("a", float(i), float(i))
    assert store.earliest("a") >= 399.0


def test_out_of_order_sample_is_merged_in_place():
    store = RamStore()
    store.add_block("a", [10.0, 11.0, 12.0], [1.0, 2.0, 3.0])
    store.add_sample("a", 10.5, 99.0)
    times, values = store.query("a")
    assert times.tolist() == [10.0, 10.5, 11.0, 12.0]
    assert values.tolist() == [1.0, 99.0, 2.0, 3.0]


def test_merge_backfill_deduplicates_timestamps():
    store = RamStore()
    store.add_block("a", [3.0, 4.0], [3.0, 4.0])
    store.add_block("a", [1.0, 2.0, 3.0], [1.0, 2.0, 30.0], merge=True)
    times, values = store.query("a")
    assert times.tolist() == [1.0, 2.0, 3.0, 4.0]
    assert values.size == 4


def test_add_block_rejects_mismatched_lengths():
    store = RamStore()
    with pytest.raises(ValueError):
        store.add_block("a", [1.0, 2.0], [1.0])


def test_trim_reclaims_memory():
    buffer = VarBuffer("a")
    for i in range(10_000):
        buffer.append(float(i), 1.0)
    before = buffer.nbytes
    buffer.trim_before(9_900)
    assert buffer.nbytes < before
    assert len(buffer) == 100


def test_decimate_preserves_extremes_and_order():
    times = np.arange(10_000, dtype=np.float64)
    values = np.sin(times / 100)
    values[5_000] = 42.0      # a spike that plain striding would skip
    values[7_000] = -42.0
    out_t, out_v = decimate(times, values, 500)
    assert out_t.size <= 500
    assert 42.0 in out_v and -42.0 in out_v
    assert np.all(np.diff(out_t) >= 0)


def test_decimate_leaves_short_series_alone():
    times = np.arange(10, dtype=np.float64)
    out_t, out_v = decimate(times, times, 500)
    assert out_t.size == 10


def test_stats_reports_span_and_usage():
    store = RamStore(max_bytes=1_000_000)
    store.add_block("a", [1.0, 2.0], [1.0, 2.0])
    stats = store.stats()
    assert stats["variables"] == 1
    assert stats["oldest"] == 1.0 and stats["newest"] == 2.0
    assert 0 < stats["usage_fraction"] < 1
    assert stats["bytes"] >= BYTES_PER_SAMPLE * 2
