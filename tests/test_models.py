"""Tests for profile validation, TOML round-tripping and time parsing."""

import time

import pytest

from subserver import tomlio
from subserver.models import (
    DEFAULT_PALETTE, ValidationError, normalise_color, validate_profile,
)
from subserver.timeutil import TimeError, format_span, is_relative, parse_datetime


def live_plot(**overrides):
    plot = {"title": "T", "mode": "live", "start": "-1h", "series": [{"variable": "a.b"}]}
    plot.update(overrides)
    return {"name": "p", "plots": [plot]}


def test_defaults_are_filled_in():
    profile = validate_profile(live_plot(), owner="alice")
    plot = profile["plots"][0]
    assert profile["layout"] == "1x1"
    assert plot["series"][0]["color"] == DEFAULT_PALETTE[0]
    assert plot["series"][0]["label"] == "a.b"
    assert plot["series"][0]["kind"] == "line"


def test_static_plot_requires_both_bounds():
    with pytest.raises(ValidationError):
        validate_profile(live_plot(mode="static"), owner="alice")
    with pytest.raises(ValidationError):
        validate_profile(
            live_plot(mode="static", start="2026-01-02T00:00", end="2026-01-01T00:00"), owner="alice"
        )
    profile = validate_profile(
        live_plot(mode="static", start="2026-01-01T00:00", end="2026-01-02T00:00"), owner="alice"
    )
    assert profile["plots"][0]["end"] == "2026-01-02T00:00"


def test_plot_needs_a_variable_or_a_function():
    with pytest.raises(ValidationError):
        validate_profile(live_plot(series=[]), owner="alice")
    profile = validate_profile(
        live_plot(series=[], function={"module": "statistics", "name": "summary"}), owner="alice"
    )
    assert profile["plots"][0]["function"]["name"] == "summary"


@pytest.mark.parametrize("module,name", [("../evil", "f"), ("m", "import os"), ("m-x", "f")])
def test_function_names_must_be_identifiers(module, name):
    with pytest.raises(ValidationError):
        validate_profile(live_plot(function={"module": module, "name": name}), owner="alice")


def test_function_arguments_must_be_simple_values():
    with pytest.raises(ValidationError):
        validate_profile(
            live_plot(function={"module": "m", "name": "f", "args": {"bad": {"nested": 1}}}),
            owner="alice",
        )
    profile = validate_profile(
        live_plot(function={"module": "m", "name": "f", "args": {"window": 10, "flag": True}}),
        owner="alice",
    )
    assert profile["plots"][0]["function"]["args"] == {"window": 10, "flag": True}


def test_y_bounds_must_be_ordered():
    with pytest.raises(ValidationError):
        validate_profile(live_plot(y_min=10, y_max=1), owner="alice")


def test_series_count_is_capped():
    with pytest.raises(ValidationError):
        validate_profile(live_plot(series=[{"variable": f"v{i}"} for i in range(30)]), owner="alice")


@pytest.mark.parametrize("value,expected", [
    ("#abc", "#aabbcc"), ("#4FC3F7", "#4fc3f7"), ("red", "#fallback"),
    ("", "#fallback"), (None, "#fallback"), ("#12345", "#fallback"),
])
def test_colour_normalisation(value, expected):
    assert normalise_color(value, "#fallback") == expected


def test_unknown_plot_kind_is_rejected():
    with pytest.raises(ValidationError):
        validate_profile(live_plot(series=[{"variable": "a", "kind": "pie"}]), owner="alice")


# -- TOML ---------------------------------------------------------------------
def test_toml_round_trip_preserves_structure():
    document = {
        "name": 'quotes " and \n newlines \t tabs',
        "version": 1,
        "ratio": 0.5,
        "live": True,
        "tags": ["a", "b"],
        "empty": [],
        "meta": {"author": "alice", "nested": {"deep": 3}},
        "plots": [
            {"title": "P1", "series": [{"variable": "a.b"}], "fn": {"args": {"w": 10}}},
            {"title": "P2", "series": []},
        ],
    }
    assert tomlio.loads(tomlio.dumps(document)) == document


def test_toml_quotes_keys_that_are_not_bare():
    text = tomlio.dumps({"a key": 1, "ok_key": 2})
    assert '"a key" = 1' in text and "ok_key = 2" in text


def test_toml_write_is_atomic_and_adds_a_header(tmp_path):
    target = tmp_path / "sub" / "x.toml"
    tomlio.write_file(target, {"a": 1}, header="written by a test")
    assert target.read_text().startswith("# written by a test")
    assert tomlio.read_file(target) == {"a": 1}
    assert not list(tmp_path.rglob("*.tmp"))


def test_toml_rejects_types_it_cannot_represent():
    with pytest.raises(TypeError):
        tomlio.dumps({"when": object()})


# -- time ---------------------------------------------------------------------
@pytest.mark.parametrize("expression,offset", [
    ("now", 0), ("-1h", -3600), ("now-90m", -5400), ("-7d", -604800), ("+30m", 1800),
])
def test_relative_times(expression, offset):
    assert abs(parse_datetime(expression) - (time.time() + offset)) < 2
    assert is_relative(expression)


@pytest.mark.parametrize("text", ["2026-08-23T00:00:00", "2026-08-23 00:00", "2026-08-23"])
def test_absolute_times_parse(text):
    assert parse_datetime(text) > 0
    assert not is_relative(text)


def test_explicit_offsets_are_honoured():
    assert parse_datetime("2026-08-23T00:00:00Z") == parse_datetime("2026-08-23T00:00:00+00:00")


@pytest.mark.parametrize("text", ["nonsense", "-1y", "now-", "2026-13-45"])
def test_unparseable_times_raise(text):
    with pytest.raises(TimeError):
        parse_datetime(text)


def test_empty_time_is_none():
    assert parse_datetime("") is None and parse_datetime(None) is None


def test_format_span_is_compact_within_a_day():
    now = time.time()
    assert "→" in format_span(now - 3600, now)
    assert format_span(None, None) == ""
