"""Tests for turning plot specifications into browser payloads."""

import time

import numpy as np
import pytest

from subserver.models import validate_profile
from subserver.query import QueryEngine
from subserver.ringstore import RamStore
from subserver.userfuncs import FunctionRegistry


@pytest.fixture
def engine(tmp_path):
    store = RamStore()
    now = time.time()
    for i in range(600):
        store.add_sample("a.b", now - 600 + i, float(i))
        store.add_sample("c.d", now - 600 + i, float(i) * 2)
    functions = FunctionRegistry("Functions", enabled=True)
    return QueryEngine(store, None, functions, max_points=200), store


def plot_spec(**overrides):
    spec = {
        "title": "T", "y_label": "K", "mode": "live", "kind": "line", "start": "-5m",
        "series": [{"variable": "a.b", "label": "A", "color": "#4fc3f7",
                    "kind": "line", "width": 1.8, "point_size": 2.5, "y_axis": "left"}],
        "show_legend": True, "show_grid": True,
    }
    spec.update(overrides)
    return spec


def test_live_payload_covers_the_window(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(), 0, owner="alice")
    assert payload["incremental"] is False
    assert payload["series"][0]["x"]
    assert payload["end_ts"] >= payload["start_ts"]
    assert payload["cursor"] == pytest.approx(payload["series"][0]["x"][-1], abs=1)


def test_incremental_returns_only_new_points(engine):
    query, store = engine
    first = query.plot_payload(plot_spec(), 0, owner="alice")
    store.add_sample("a.b", first["cursor"] + 1, 12345.0)
    second = query.plot_payload(plot_spec(), 0, owner="alice", since=first["cursor"])
    assert second["incremental"] is True
    assert second["series"][0]["y"] == [12345.0]


def test_a_cursor_before_the_window_forces_a_full_reload(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(), 0, owner="alice", since=time.time() - 100_000)
    assert payload["incremental"] is False


def test_static_payload_is_bounded_and_never_incremental(engine):
    query, _ = engine
    spec = plot_spec(mode="static", start="-8m", end="-4m")
    payload = query.plot_payload(spec, 0, owner="alice", since=time.time())
    assert payload["incremental"] is False
    assert payload["end_ts"] - payload["start_ts"] == pytest.approx(240, abs=2)


def test_static_plot_missing_a_bound_reports_an_error(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(mode="static", end=""), 0, owner="alice")
    assert "start or end" in payload["error"]


def test_an_unparseable_start_is_reported_not_raised(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(start="last tuesday"), 0, owner="alice")
    assert payload["error"] and payload["series"] == []


def test_series_are_decimated_to_the_limit(engine):
    query, store = engine
    now = time.time()
    for i in range(20_000):
        store.add_sample("big", now - 20_000 + i, float(i))
    spec = plot_spec(start="-6h", series=[{"variable": "big", "label": "big", "color": "#fff",
                                           "kind": "line", "width": 1, "point_size": 0, "y_axis": "left"}])
    payload = query.plot_payload(spec, 0, owner="alice")
    assert 0 < len(payload["series"][0]["x"]) <= 200


def test_function_output_replaces_the_raw_series_by_default(engine):
    query, _ = engine
    spec = plot_spec(function={"module": "statistics", "name": "rolling_mean", "scope": "examples",
                               "args": {"window": 5}, "replace_series": True, "owner": "examples"})
    payload = query.plot_payload(spec, 0, owner="examples")
    assert [s["label"] for s in payload["series"]] == ["a.b (mean 5)"]


def test_kept_raw_series_never_shares_a_colour_with_the_function_output(engine):
    query, _ = engine
    spec = plot_spec(function={"module": "statistics", "name": "rolling_mean", "scope": "examples",
                               "args": {"window": 5}, "replace_series": False, "owner": "examples"})
    payload = query.plot_payload(spec, 0, owner="examples")
    colours = [s["color"] for s in payload["series"]]
    assert len(colours) == 2 and len(set(colours)) == 2
    assert "#4fc3f7" in colours          # the raw series keeps the colour it was given


def test_scalar_function_results_become_reference_lines(engine):
    query, _ = engine
    spec = plot_spec(function={"module": "statistics", "name": "summary", "scope": "examples",
                               "args": {}, "replace_series": False, "owner": "examples"})
    payload = query.plot_payload(spec, 0, owner="examples")
    assert {line["label"] for line in payload["lines"]} == {"mean", "min", "max"}


def test_a_failing_function_reports_on_the_plot(engine):
    query, _ = engine
    spec = plot_spec(function={"module": "statistics", "name": "difference", "scope": "examples",
                               "args": {}, "replace_series": True, "owner": "examples"})
    payload = query.plot_payload(spec, 0, owner="examples")
    assert "at least two variables" in payload["error"]


def test_function_plots_are_always_recomputed_in_full(engine):
    query, _ = engine
    spec = plot_spec(function={"module": "statistics", "name": "normalise", "scope": "examples",
                               "args": {}, "replace_series": True, "owner": "examples"})
    payload = query.plot_payload(spec, 0, owner="examples", since=time.time() - 10)
    assert payload["incremental"] is False


def test_missing_variable_is_empty_with_a_coverage_notice(engine):
    query, _ = engine
    spec = plot_spec(series=[{"variable": "absent", "label": "absent", "color": "#fff",
                              "kind": "line", "width": 1, "point_size": 0, "y_axis": "left"}])
    payload = query.plot_payload(spec, 0, owner="alice")
    assert payload["series"][0]["x"] == []
    assert payload["notice"]


def test_an_over_long_live_window_is_clamped_with_a_notice(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(start="-200d"), 0, owner="alice")
    assert payload["end_ts"] - payload["start_ts"] <= 90 * 24 * 3600 + 1
    assert "90 days" in payload["notice"]


def test_profile_payload_can_be_restricted_to_some_plots(engine):
    query, _ = engine
    profile = validate_profile(
        {"name": "p", "layout": "2x1", "plots": [plot_spec(), plot_spec(title="Second")]},
        owner="alice",
    )
    payloads = query.profile_payload(profile, owner="alice", only={1})
    assert [p["index"] for p in payloads] == [1]


def test_payload_values_are_json_safe_numbers(engine):
    query, _ = engine
    payload = query.plot_payload(plot_spec(), 0, owner="alice")
    values = payload["series"][0]["y"]
    assert all(isinstance(v, float) and np.isfinite(v) for v in values)
